# app.py — Paper Trading Sandbox (educational, single file)

import hashlib
import math
import os
import random
import re
import sqlite3
import threading
from datetime import datetime

import pandas as pd
import streamlit as st

# ============================================================
# CONFIG
# ============================================================
DB_PATH = "paper_trading.db"
INITIAL_BALANCE = 1_000_000.0
PRICE_CACHE_SECONDS = 60
LEADERBOARD_CACHE_SECONDS = 15

# Base prices are only used by the simulated fallback (approximate INR values)
TICKERS = {
    "RELIANCE.NS": 1400.0,
    "TCS.NS": 3200.0,
    "HDFCBANK.NS": 1000.0,
    "INFY.NS": 1500.0,
    "ICICIBANK.NS": 1350.0,
}

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


class TradeError(Exception):
    pass


class AuthError(Exception):
    pass


# ============================================================
# DATABASE
# ============================================================
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
                initial_balance REAL NOT NULL DEFAULT 1000000,
                current_balance REAL NOT NULL DEFAULT 1000000,
                salt            TEXT NOT NULL,
                password_hash   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                ticker          TEXT NOT NULL,
                type            TEXT NOT NULL CHECK (type IN ('BUY', 'SELL')),
                quantity        INTEGER NOT NULL CHECK (quantity > 0),
                execution_price REAL NOT NULL,
                timestamp       TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES Users(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_user ON Trades(user_id)")
    finally:
        conn.close()


# ---------- authentication ----------
def _hash_pw(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000).hex()


def register_user(username: str, password: str) -> sqlite3.Row:
    if not USERNAME_RE.match(username):
        raise AuthError("Username must be 3-20 characters: letters, digits, underscore.")
    if len(password) < 4:
        raise AuthError("Password must be at least 4 characters.")
    salt = os.urandom(16)
    conn = get_conn()
    try:
        try:
            conn.execute(
                "INSERT INTO Users (username, initial_balance, current_balance, salt, password_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, INITIAL_BALANCE, INITIAL_BALANCE, salt.hex(), _hash_pw(password, salt)),
            )
        except sqlite3.IntegrityError:
            raise AuthError("That username is already taken.")
        return conn.execute("SELECT * FROM Users WHERE username = ?", (username,)).fetchone()
    finally:
        conn.close()


def login_user(username: str, password: str) -> sqlite3.Row:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM Users WHERE username = ?", (username,)).fetchone()
    finally:
        conn.close()
    if row is None or _hash_pw(password, bytes.fromhex(row["salt"])) != row["password_hash"]:
        raise AuthError("Wrong username or password.")
    return row


def get_user_by_id(user_id: int) -> sqlite3.Row:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM Users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()


def get_trades(user_id: int) -> pd.DataFrame:
    conn = get_conn()
    try:
        return pd.read_sql_query(
            "SELECT id, ticker, type, quantity, execution_price, timestamp "
            "FROM Trades WHERE user_id = ? ORDER BY id",
            conn,
            params=(user_id,),
        )
    finally:
        conn.close()


# ============================================================
# TRADE EXECUTION (atomic; no short selling)
# ============================================================
def execute_trade(user_id: int, ticker: str, side: str, quantity: int, price: float) -> str:
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise TradeError("Invalid order type.")
    if ticker not in TICKERS:
        raise TradeError("Unknown ticker.")
    if quantity <= 0:
        raise TradeError("Quantity must be a positive whole number.")
    if not price or price <= 0:
        raise TradeError("Invalid price.")

    value = round(quantity * price, 2)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cash = conn.execute(
            "SELECT current_balance FROM Users WHERE id = ?", (user_id,)
        ).fetchone()["current_balance"]

        if side == "BUY":
            if value > cash:
                raise TradeError(f"Insufficient cash. Need ₹{value:,.2f}, have ₹{cash:,.2f}.")
            new_cash = cash - value
        else:
            owned = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN type='BUY' THEN quantity ELSE -quantity END), 0) AS q "
                "FROM Trades WHERE user_id = ? AND ticker = ?",
                (user_id, ticker),
            ).fetchone()["q"]
            if quantity > owned:
                raise TradeError(f"Short selling not allowed. You own {owned} share(s) of {ticker}.")
            new_cash = cash + value

        conn.execute("UPDATE Users SET current_balance = ? WHERE id = ?", (new_cash, user_id))
        conn.execute(
            "INSERT INTO Trades (user_id, ticker, type, quantity, execution_price, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, ticker, side, quantity, price, datetime.now().isoformat(timespec="seconds")),
        )
        conn.execute("COMMIT")
        return f"{side} {quantity} × {ticker} @ ₹{price:,.2f} (total ₹{value:,.2f})"
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


# ============================================================
# PORTFOLIO MATH
# ============================================================
def compute_holdings(trades: pd.DataFrame) -> dict:
    """Average-cost method -> {ticker: {'qty', 'avg_cost'}}"""
    holdings = {}
    for _, t in trades.iterrows():
        h = holdings.setdefault(t["ticker"], {"qty": 0, "avg_cost": 0.0})
        if t["type"] == "BUY":
            total = h["avg_cost"] * h["qty"] + t["execution_price"] * t["quantity"]
            h["qty"] += int(t["quantity"])
            h["avg_cost"] = total / h["qty"]
        else:
            h["qty"] -= int(t["quantity"])
            if h["qty"] == 0:
                h["avg_cost"] = 0.0
    return {k: v for k, v in holdings.items() if v["qty"] > 0}


def portfolio_table(holdings: dict, prices: dict) -> pd.DataFrame:
    rows = []
    for ticker, h in holdings.items():
        ltp = prices.get(ticker, 0.0)
        invested = h["avg_cost"] * h["qty"]
        value = ltp * h["qty"]
        rows.append(
            {
                "Ticker": ticker,
                "Qty": h["qty"],
                "Avg Cost (₹)": round(h["avg_cost"], 2),
                "Price (₹)": round(ltp, 2),
                "Invested (₹)": round(invested, 2),
                "Value (₹)": round(value, 2),
                "P&L (₹)": round(value - invested, 2),
                "P&L %": round((value - invested) / invested * 100, 2) if invested else 0.0,
            }
        )
    return pd.DataFrame(rows)


@st.cache_data(ttl=LEADERBOARD_CACHE_SECONDS, show_spinner=False)
def leaderboard_df(prices: dict) -> pd.DataFrame:
    """Efficient: two SQL queries total, regardless of number of users."""
    conn = get_conn()
    try:
        users = conn.execute(
            "SELECT id, username, initial_balance, current_balance FROM Users"
        ).fetchall()
        positions = conn.execute(
            "SELECT user_id, ticker, "
            "SUM(CASE WHEN type='BUY' THEN quantity ELSE -quantity END) AS q "
            "FROM Trades GROUP BY user_id, ticker HAVING q > 0"
        ).fetchall()
    finally:
        conn.close()

    holding_value = {}
    for p in positions:
        holding_value[p["user_id"]] = holding_value.get(p["user_id"], 0.0) + p["q"] * prices.get(p["ticker"], 0.0)

    rows = []
    for u in users:
        hv = holding_value.get(u["id"], 0.0)
        nw = u["current_balance"] + hv
        rows.append(
            {
                "Username": u["username"],
                "Cash (₹)": round(u["current_balance"], 2),
                "Holdings (₹)": round(hv, 2),
                "Net Worth (₹)": round(nw, 2),
                "Return %": round((nw - u["initial_balance"]) / u["initial_balance"] * 100, 2),
            }
        )
    if not rows:
        return pd.DataFrame(columns=["Rank", "Username", "Cash (₹)", "Holdings (₹)", "Net Worth (₹)", "Return %"])
    df = pd.DataFrame(rows).sort_values("Net Worth (₹)", ascending=False).reset_index(drop=True)
    df.insert(0, "Rank", df.index + 1)
    return df


# ============================================================
# PRICE DATA (yfinance, with simulated fallback)
# ============================================================
@st.cache_resource
def _shared_sim() -> dict:
    return {"prices": dict(TICKERS), "lock": threading.Lock()}


def _fetch_yfinance() -> dict:
    import yfinance as yf

    tickers = list(TICKERS)
    for period, interval in (("1d", "1m"), ("5d", "1d")):
        try:
            df = yf.download(
                tickers, period=period, interval=interval,
                progress=False, auto_adjust=True, threads=False,
            )
            last = df["Close"].ffill().iloc[-1]
            prices = {t: round(float(last[t]), 2) for t in tickers}
            if all(p > 0 and not math.isnan(p) for p in prices.values()):
                return prices
        except Exception:
            continue
    raise RuntimeError("yfinance returned no usable data")


def _simulated_prices() -> dict:
    sim = _shared_sim()
    with sim["lock"]:
        for t in sim["prices"]:
            sim["prices"][t] *= math.exp(random.gauss(0, 0.002))
        return {t: round(p, 2) for t, p in sim["prices"].items()}


@st.cache_data(ttl=PRICE_CACHE_SECONDS, show_spinner=False)
def get_prices() -> tuple:
    stamp = datetime.now().strftime("%H:%M:%S")
    try:
        prices = _fetch_yfinance()
        sim = _shared_sim()
        with sim["lock"]:
            sim["prices"] = dict(prices)  # seed simulation from last real prices
        return prices, "Yahoo Finance (delayed)", stamp
    except Exception:
        return _simulated_prices(), "SIMULATED random walk (live feed unavailable)", stamp


# ============================================================
# ADMIN: backup / restore (guards against Community Cloud resets)
# ============================================================
def admin_password() -> str:
    try:
        return str(st.secrets["ADMIN_PASSWORD"])
    except Exception:
        return ""


def restore_db(file_bytes: bytes) -> None:
    tmp = DB_PATH + ".upload"
    with open(tmp, "wb") as f:
        f.write(file_bytes)
    c = sqlite3.connect(tmp)
    try:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()
    if not {"Users", "Trades"} <= names:
        os.remove(tmp)
        raise ValueError("This file is not a valid backup of the sandbox database.")
    os.replace(tmp, DB_PATH)


def sidebar_admin() -> None:
    pw_required = admin_password()
    with st.sidebar.expander("🔧 Admin"):
        if not pw_required:
            st.caption("Set ADMIN_PASSWORD in the app's Secrets to enable.")
            return
        if st.text_input("Admin password", type="password", key="admin_pw") != pw_required:
            return
        with open(DB_PATH, "rb") as f:
            st.download_button(
                "⬇️ Download backup", f.read(),
                file_name=f"paper_trading_{datetime.now():%Y%m%d_%H%M}.db",
            )
        up = st.file_uploader("Restore from backup (.db)", type=["db"])
        if up is not None and st.button("Restore now (replaces current data)"):
            try:
                restore_db(up.getvalue())
                st.cache_data.clear()
                st.success("Restored. Refresh the page.")
            except Exception as e:
                st.error(str(e))
        conn = get_conn()
        n = conn.execute("SELECT COUNT(*) FROM Users").fetchone()[0]
        conn.close()
        st.caption(f"Registered users: {n}")


# ============================================================
# UI
# ============================================================
def inr(x: float) -> str:
    return f"₹{x:,.2f}"


def sidebar_login() -> None:
    st.sidebar.title("👤 Account")
    if st.session_state.get("user_id"):
        st.sidebar.success(f"Logged in as **{st.session_state['username']}**")
        if st.sidebar.button("Log out"):
            for k in ("user_id", "username"):
                st.session_state.pop(k, None)
            st.rerun()
        return

    mode = st.sidebar.radio("I want to", ["Login", "Register"], horizontal=True)
    with st.sidebar.form("auth_form"):
        name = st.text_input("Username").strip()
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button(mode)
    if submitted:
        try:
            user = register_user(name, pw) if mode == "Register" else login_user(name, pw)
            st.session_state["user_id"] = user["id"]
            st.session_state["username"] = user["username"]
            st.rerun()
        except AuthError as e:
            st.sidebar.error(str(e))


def tab_dashboard(user_id: int, prices: dict) -> None:
    user = get_user_by_id(user_id)
    trades = get_trades(user_id)
    holdings = compute_holdings(trades)
    hv = sum(prices.get(t, 0.0) * h["qty"] for t, h in holdings.items())
    cash = user["current_balance"]
    nw = cash + hv
    pnl = nw - user["initial_balance"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cash", inr(cash))
    c2.metric("Holdings value", inr(hv))
    c3.metric("Net worth", inr(nw))
    c4.metric("Total P&L", inr(pnl), f"{pnl / user['initial_balance'] * 100:+.2f}%")

    st.subheader("Open portfolio")
    if holdings:
        st.dataframe(portfolio_table(holdings, prices), hide_index=True)
    else:
        st.info("No open positions yet. Go to the Trading Terminal tab.")

    st.subheader("Trade history")
    if trades.empty:
        st.caption("No trades yet.")
    else:
        st.dataframe(trades.iloc[::-1], hide_index=True)


def tab_terminal(user_id: int, prices: dict) -> None:
    user = get_user_by_id(user_id)
    holdings = compute_holdings(get_trades(user_id))

    flash = st.session_state.pop("flash", None)
    if flash:
        (st.success if flash[0] == "ok" else st.error)(flash[1])

    ticker = st.selectbox("Ticker", list(TICKERS.keys()))
    price = prices[ticker]
    owned = holdings.get(ticker, {}).get("qty", 0)

    c1, c2, c3 = st.columns(3)
    c1.metric("Current price (delayed)", inr(price))
    c2.metric("Cash available", inr(user["current_balance"]))
    c3.metric(f"You own ({ticker})", owned)

    qty = st.number_input("Quantity", min_value=1, value=1, step=1)
    st.caption(f"Order value: **{inr(qty * price)}**")

    b1, b2 = st.columns(2)
    side = None
    if b1.button("🟢 BUY"):
        side = "BUY"
    if b2.button("🔴 SELL"):
        side = "SELL"

    if side:
        try:
            msg = execute_trade(user_id, ticker, side, int(qty), price)
            st.session_state["flash"] = ("ok", f"Executed: {msg}")
            leaderboard_df.clear()
        except TradeError as e:
            st.session_state["flash"] = ("err", str(e))
        st.rerun()


def tab_leaderboard(prices: dict) -> None:
    df = leaderboard_df(prices)
    if df.empty:
        st.info("No users yet.")
        return
    me = st.session_state.get("username")
    if me:
        mine = df[df["Username"].str.lower() == me.lower()]
        if not mine.empty:
            r = mine.iloc[0]
            st.success(f"Your rank: **#{int(r['Rank'])}** of {len(df)} · Net worth {inr(r['Net Worth (₹)'])}")
    st.caption(f"Showing top 50 of {len(df)} participants · refreshes every {LEADERBOARD_CACHE_SECONDS}s")
    st.dataframe(df.head(50), hide_index=True)
    st.download_button("Download full leaderboard (CSV)", df.to_csv(index=False), "leaderboard.csv", "text/csv")


def main() -> None:
    st.set_page_config(page_title="Paper Trading Sandbox", page_icon="📈", layout="wide")
    init_db()
    sidebar_login()
    sidebar_admin()

    st.title("📈 Paper Trading Sandbox")
    st.caption("Educational simulation with virtual money. Prices are delayed and not for real trading decisions.")

    prices, source, fetched_at = get_prices()
    left, right = st.columns([4, 1])
    left.caption(f"Price source: **{source}** · fetched {fetched_at}")
    if right.button("🔄 Refresh prices"):
        get_prices.clear()
        st.rerun()

    if not st.session_state.get("user_id"):
        st.info("👈 Register or log in from the sidebar to start trading.")
        st.subheader("Leaderboard")
        tab_leaderboard(prices)
        return

    user_id = st.session_state["user_id"]
    t1, t2, t3 = st.tabs(["🏠 Dashboard", "💹 Trading Terminal", "🏆 Leaderboard"])
    with t1:
        tab_dashboard(user_id, prices)
    with t2:
        tab_terminal(user_id, prices)
    with t3:
        tab_leaderboard(prices)


main()
