# app.py — Paper Trading Sandbox (educational, single file)
# v2: full NSE stock universe + any BSE/NSE symbol, prices fetched on demand

import hashlib
import io
import math
import os
import re
import sqlite3
from datetime import datetime
from urllib.request import Request, urlopen

import pandas as pd
import streamlit as st

# ============================================================
# CONFIG
# ============================================================
DB_PATH = "paper_trading.db"
INITIAL_BALANCE = 1_000_000.0
PRICE_CACHE_SECONDS = 60
LEADERBOARD_CACHE_SECONDS = 60
FETCH_CHUNK = 50

NSE_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
LOCAL_NSE_FILE = "EQUITY_L.csv"   # optional: upload NSE's file to GitHub with this name
LOCAL_EXTRA_FILE = "stocks.csv"   # optional: extra symbols, columns: ticker,name

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
TICKER_RE = re.compile(r"^[A-Z0-9&\-_]{1,20}\.(NS|BO)$")

# Used only if the full list cannot be loaded
FALLBACK_STOCKS = [
    ("RELIANCE.NS", "Reliance Industries"), ("TCS.NS", "Tata Consultancy Services"),
    ("HDFCBANK.NS", "HDFC Bank"), ("INFY.NS", "Infosys"), ("ICICIBANK.NS", "ICICI Bank"),
    ("HINDUNILVR.NS", "Hindustan Unilever"), ("ITC.NS", "ITC"), ("SBIN.NS", "State Bank of India"),
    ("BHARTIARTL.NS", "Bharti Airtel"), ("KOTAKBANK.NS", "Kotak Mahindra Bank"),
    ("LT.NS", "Larsen & Toubro"), ("AXISBANK.NS", "Axis Bank"), ("ASIANPAINT.NS", "Asian Paints"),
    ("MARUTI.NS", "Maruti Suzuki"), ("SUNPHARMA.NS", "Sun Pharma"), ("TITAN.NS", "Titan Company"),
    ("BAJFINANCE.NS", "Bajaj Finance"), ("WIPRO.NS", "Wipro"), ("HCLTECH.NS", "HCL Technologies"),
    ("ULTRACEMCO.NS", "UltraTech Cement"), ("NESTLEIND.NS", "Nestle India"),
    ("TATAMOTORS.NS", "Tata Motors"), ("TATASTEEL.NS", "Tata Steel"), ("NTPC.NS", "NTPC"),
    ("POWERGRID.NS", "Power Grid"), ("ONGC.NS", "ONGC"), ("M&M.NS", "Mahindra & Mahindra"),
    ("ADANIENT.NS", "Adani Enterprises"), ("ADANIPORTS.NS", "Adani Ports"),
    ("COALINDIA.NS", "Coal India"), ("JSWSTEEL.NS", "JSW Steel"), ("TECHM.NS", "Tech Mahindra"),
    ("DRREDDY.NS", "Dr Reddy's"), ("CIPLA.NS", "Cipla"), ("EICHERMOT.NS", "Eicher Motors"),
    ("BAJAJ-AUTO.NS", "Bajaj Auto"), ("HEROMOTOCO.NS", "Hero MotoCorp"), ("ZOMATO.NS", "Zomato"),
    ("IRCTC.NS", "IRCTC"), ("YESBANK.NS", "Yes Bank"),
]


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
        # NEW: last known price per ticker (fallback when Yahoo is unavailable)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Prices (
                ticker     TEXT PRIMARY KEY,
                price      REAL NOT NULL,
                updated_at TEXT NOT NULL
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
# STOCK UNIVERSE (all NSE-listed stocks + any extra symbols)
# ============================================================
def _read_nse_csv(src) -> pd.DataFrame:
    df = pd.read_csv(src)
    df.columns = [c.strip().upper() for c in df.columns]
    if "SERIES" in df.columns:
        df = df[df["SERIES"].astype(str).str.strip().isin(["EQ", "BE"])]
    return pd.DataFrame(
        {
            "ticker": df["SYMBOL"].astype(str).str.strip().str.upper() + ".NS",
            "name": df["NAME OF COMPANY"].astype(str).str.strip(),
        }
    )


def _read_extra_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "name" not in df.columns:
        df["name"] = ""
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    return df[["ticker", "name"]]


@st.cache_data(ttl=24 * 3600, show_spinner="Loading stock list...")
def load_universe() -> tuple:
    """Returns (DataFrame[ticker, name], source_label)."""
    frames, label = [], "built-in list only"

    # 1) NSE list: local file first, then download
    try:
        if os.path.exists(LOCAL_NSE_FILE):
            frames.append(_read_nse_csv(LOCAL_NSE_FILE))
            label = f"NSE list ({LOCAL_NSE_FILE})"
        else:
            req = Request(NSE_LIST_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=15) as resp:
                frames.append(_read_nse_csv(io.BytesIO(resp.read())))
            label = "NSE official list (downloaded)"
    except Exception:
        pass

    # 2) optional extra symbols (e.g. BSE stocks)
    try:
        if os.path.exists(LOCAL_EXTRA_FILE):
            frames.append(_read_extra_csv(LOCAL_EXTRA_FILE))
            label += " + stocks.csv"
    except Exception:
        pass

    # 3) always include the built-in large caps
    frames.append(pd.DataFrame(FALLBACK_STOCKS, columns=["ticker", "name"]))

    df = pd.concat(frames, ignore_index=True)
    df = df[df["ticker"].apply(lambda t: bool(TICKER_RE.match(t)))]
    df = df.drop_duplicates(subset="ticker").sort_values("ticker").reset_index(drop=True)
    return df, label


# ============================================================
# PRICES (fetched on demand, cached, with stored fallback)
# ============================================================
def _download_chunk(chunk: list) -> dict:
    import yfinance as yf

    for period, interval in (("5d", "1m"), ("5d", "1d")):
        try:
            df = yf.download(
                chunk, period=period, interval=interval,
                progress=False, auto_adjust=True, threads=True,
            )
            if df is None or df.empty:
                continue
            close = df["Close"]
            if isinstance(close, pd.Series):
                close = close.to_frame(name=chunk[0])
            last = close.ffill().iloc[-1]
            out = {}
            for t in chunk:
                if t in last.index:
                    p = float(last[t])
                    if p > 0 and not math.isnan(p):
                        out[t] = round(p, 2)
            if out:
                return out
        except Exception:
            continue
    return {}


@st.cache_data(ttl=PRICE_CACHE_SECONDS, show_spinner=False)
def fetch_prices(tickers: tuple) -> dict:
    """Live (delayed) prices from Yahoo Finance. Missing tickers are simply absent."""
    result = {}
    tickers = list(tickers)
    for i in range(0, len(tickers), FETCH_CHUNK):
        result.update(_download_chunk(tickers[i : i + FETCH_CHUNK]))
    return result


def _save_prices(prices: dict) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO Prices (ticker, price, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET price=excluded.price, updated_at=excluded.updated_at",
            [(t, p, now) for t, p in prices.items()],
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()


def _fallback_prices(tickers: list) -> dict:
    """Stored price first, then the most recent trade price."""
    out = {}
    if not tickers:
        return out
    marks = ",".join("?" * len(tickers))
    conn = get_conn()
    try:
        for r in conn.execute(f"SELECT ticker, price FROM Prices WHERE ticker IN ({marks})", tickers):
            out[r["ticker"]] = r["price"]
        rest = [t for t in tickers if t not in out]
        if rest:
            marks2 = ",".join("?" * len(rest))
            for r in conn.execute(
                f"SELECT ticker, execution_price FROM Trades WHERE ticker IN ({marks2}) "
                "AND id IN (SELECT MAX(id) FROM Trades GROUP BY ticker)",
                rest,
            ):
                out[r["ticker"]] = r["execution_price"]
    finally:
        conn.close()
    return out


def get_prices_for(tickers) -> tuple:
    """Returns (prices_dict, stale_set). 'stale' = live fetch failed, using an older price."""
    tickers = tuple(sorted(set(tickers)))
    if not tickers:
        return {}, set()
    live = fetch_prices(tickers)
    if live:
        _save_prices(live)
    prices, stale = dict(live), set()
    missing = [t for t in tickers if t not in live]
    if missing:
        for t, p in _fallback_prices(missing).items():
            prices[t] = p
            stale.add(t)
    return prices, stale


# ============================================================
# TRADE EXECUTION (atomic; no short selling)
# ============================================================
def execute_trade(user_id: int, ticker: str, side: str, quantity: int, price: float) -> str:
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise TradeError("Invalid order type.")
    if not TICKER_RE.match(ticker):
        raise TradeError("Invalid ticker format. Use e.g. RELIANCE.NS or 500325.BO")
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


def portfolio_table(holdings: dict, prices: dict, stale: set) -> pd.DataFrame:
    rows = []
    for ticker, h in holdings.items():
        ltp = prices.get(ticker, 0.0)
        invested = h["avg_cost"] * h["qty"]
        value = ltp * h["qty"]
        rows.append(
            {
                "Ticker": ticker + (" *" if ticker in stale else ""),
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
def leaderboard_df() -> pd.DataFrame:
    """Two SQL queries + one batched price fetch for only the tickers people actually hold."""
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

    prices, _ = get_prices_for({p["ticker"] for p in positions})
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
                init_db()
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


def tab_dashboard(user_id: int) -> None:
    user = get_user_by_id(user_id)
    trades = get_trades(user_id)
    holdings = compute_holdings(trades)
    prices, stale = get_prices_for(holdings.keys())
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
        st.dataframe(portfolio_table(holdings, prices, stale), hide_index=True)
        if stale:
            st.caption("* Live price unavailable right now; showing the last known price.")
    else:
        st.info("No open positions yet. Go to the Trading Terminal tab.")

    st.subheader("Trade history")
    if trades.empty:
        st.caption("No trades yet.")
    else:
        st.dataframe(trades.iloc[::-1], hide_index=True)


def tab_terminal(user_id: int, universe: pd.DataFrame) -> None:
    user = get_user_by_id(user_id)
    holdings = compute_holdings(get_trades(user_id))

    flash = st.session_state.pop("flash", None)
    if flash:
        (st.success if flash[0] == "ok" else st.error)(flash[1])

    names = dict(zip(universe["ticker"], universe["name"]))
    options = list(universe["ticker"])
    default_idx = options.index("RELIANCE.NS") if "RELIANCE.NS" in options else 0

    selected = st.selectbox(
        f"Search {len(options):,} stocks (type a symbol or company name)",
        options,
        index=default_idx,
        format_func=lambda t: f"{t} — {names.get(t, '')}",
    )
    custom = st.text_input(
        "Or type any symbol (BSE example: 500325.BO · NSE example: TATAMOTORS.NS)",
        placeholder="Leave empty to use the stock chosen above",
    ).strip().upper()

    ticker = custom if custom else selected
    if not TICKER_RE.match(ticker):
        st.error("Symbol must end with .NS (NSE) or .BO (BSE), e.g. RELIANCE.NS or 500325.BO")
        return

    prices, stale = get_prices_for([ticker])
    price = prices.get(ticker)
    fresh = price is not None and ticker not in stale
    owned = holdings.get(ticker, {}).get("qty", 0)

    if not fresh:
        if price is None:
            st.error(f"No price found for **{ticker}**. Check the symbol and try again.")
        else:
            st.warning(f"Live price for **{ticker}** is unavailable right now. Trading is paused for it.")

    c1, c2, c3 = st.columns(3)
    c1.metric("Current price (delayed)", inr(price) if price else "—")
    c2.metric("Cash available", inr(user["current_balance"]))
    c3.metric(f"You own ({ticker})", owned)

    qty = st.number_input("Quantity", min_value=1, value=1, step=1)
    if price:
        st.caption(f"Order value: **{inr(qty * price)}**")

    b1, b2, b3 = st.columns([1, 1, 1])
    side = None
    if b1.button("🟢 BUY", disabled=not fresh):
        side = "BUY"
    if b2.button("🔴 SELL", disabled=not fresh):
        side = "SELL"
    if b3.button("🔄 Refresh price"):
        fetch_prices.clear()
        st.rerun()

    if side:
        try:
            msg = execute_trade(user_id, ticker, side, int(qty), price)
            st.session_state["flash"] = ("ok", f"Executed: {msg}")
            leaderboard_df.clear()
        except TradeError as e:
            st.session_state["flash"] = ("err", str(e))
        st.rerun()


def tab_leaderboard() -> None:
    df = leaderboard_df()
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

    universe, source = load_universe()

    st.title("📈 Paper Trading Sandbox")
    st.caption(
        "Educational simulation with virtual money. Prices come from Yahoo Finance and are delayed."
    )
    st.caption(f"📚 {len(universe):,} stocks available · source: {source}")

    if not st.session_state.get("user_id"):
        st.info("👈 Register or log in from the sidebar to start trading.")
        st.subheader("Leaderboard")
        tab_leaderboard()
        return

    user_id = st.session_state["user_id"]
    t1, t2, t3 = st.tabs(["🏠 Dashboard", "💹 Trading Terminal", "🏆 Leaderboard"])
    with t1:
        tab_dashboard(user_id)
    with t2:
        tab_terminal(user_id, universe)
    with t3:
        tab_leaderboard()


main()
