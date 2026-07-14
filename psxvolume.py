import sqlite3
import logging
from typing import List, Optional, Dict, Any
from contextlib import contextmanager
from datetime import datetime

import requests
import pandas as pd
import yfinance as yf
import streamlit as st
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ── Setup ──────────────────────────────────────────────────────────────────
DB_PATH = "volume_spikes.db"
PKT = ZoneInfo("Asia/Karachi")

# KSE-100 constituent symbols tracked
WATCHLIST = [
    "ABL", "ABOT", "AGP", "AICL", "AIRLINK", "AKBL", "ATLH", "ATRL",
    "BAFL", "BAHL", "BOP", "BPL", "BWCL", "CHCC", "CNERGY", "COLG",
    "CPHL", "DCR", "DGKC", "EFERT", "ENGRO", "EPCL", "FATIMA", "FCCL",
    "FFC", "FFL", "FHAM", "GADT", "GAL", "GHGL", "GHNI", "HALEON",
    "HCAR", "HGFA", "HINOON", "HUBC", "HUMNL", "IBFL", "INDU", "INIL",
    "JDWS", "KAPCO", "KEL", "KOHC", "KTML", "LCI", "LUCK", "MARI",
    "MCB", "MEBL", "MEHT", "MUREB", "NBP", "NESTLE", "NPL", "OGDC",
    "PABC", "PAEL", "PAKT", "PGLC", "PIBTL", "PIOC", "PKGS", "PPL",
    "PRL", "PSEL", "PSO", "PSX", "PTC", "RMPL", "SAZEW", "SCBPL",
    "SEARL", "SHFA", "SNGP", "SPWL", "SSGC", "SSOM", "SYS", "TGL",
    "THALL", "TPLP", "TRG", "UBL", "UPFL", "WTL", "YOUW",
]


# ── Data sources ─────────────────────────────────────────────────────────
class TVScreener:
    """Queries TradingView's public Scanner API — returns all requested
    symbols in a single request (much faster than per-symbol scraping)."""

    DEFAULT_URL = "https://scanner.tradingview.com/pakistan/scan"
    DEFAULT_COLS = ["name", "close", "change", "volume"]

    def __init__(self, api_url: str = DEFAULT_URL):
        self.api_url = api_url
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
        })

    def fetch_data(self, symbols: Optional[List[str]] = None,
                    columns: Optional[List[str]] = None) -> pd.DataFrame:
        cols = columns if columns is not None else self.DEFAULT_COLS
        payload: Dict[str, Any] = {
            "filter": [],
            "options": {"active_symbols_only": True},
            "symbols": {"query": {"types": []}, "tickers": []},
            "columns": cols,
            "sort": {"sortBy": "name", "sortOrder": "asc"},
            "range": [0, 500],
        }
        if symbols:
            payload["symbols"]["tickers"] = [f"PSX:{sym.upper()}" for sym in symbols]
        else:
            payload["filter"].append({"left": "type", "operation": "equal", "right": "stock"})

        response = self.session.post(self.api_url, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        raw_rows = data.get("data", [])
        if not raw_rows:
            return pd.DataFrame()

        parsed = []
        for item in raw_rows:
            ticker = item.get("s", "").split(":")[-1]
            row = {"symbol": ticker}
            for col_name, val in zip(cols, item.get("d", [])):
                row[col_name] = val
            parsed.append(row)
        return pd.DataFrame(parsed)


class YFinanceClient:
    """Batched historical OHLCV download via yfinance, formatted for PSX (.KA)."""

    def __init__(self, suffix: str = ".KA"):
        self.suffix = suffix

    def _format_tickers(self, symbols: List[str]) -> List[str]:
        out = []
        for sym in symbols:
            s = sym.strip().upper()
            out.append(s if s.endswith(self.suffix) else f"{s}{self.suffix}")
        return out

    def fetch_history(self, symbols, period="3mo", interval="1d") -> pd.DataFrame:
        ticker_list = [symbols] if isinstance(symbols, str) else list(symbols)
        formatted = self._format_tickers(ticker_list)

        df_raw = yf.download(
            tickers=formatted, period=period, interval=interval,
            auto_adjust=True, progress=False, threads=True,
        )
        if df_raw.empty:
            return pd.DataFrame()
        return self._flatten(df_raw, ticker_list)

    def _flatten(self, df: pd.DataFrame, original_symbols: List[str]) -> pd.DataFrame:
        if len(original_symbols) == 1:
            clean = original_symbols[0].upper()
            out = df.reset_index()
            out.columns = [c.lower() for c in out.columns]
            out.insert(1, "symbol", clean)
            return out

        if isinstance(df.columns, pd.MultiIndex):
            dfs = []
            for raw_ticker in df.columns.levels[1]:
                if raw_ticker not in df.columns.get_level_values(1):
                    continue
                t_df = df.xs(raw_ticker, axis=1, level=1).dropna()
                if t_df.empty:
                    continue
                t_df = t_df.reset_index()
                t_df.columns = [c.lower() for c in t_df.columns]
                t_df.insert(1, "symbol", raw_ticker.replace(self.suffix, ""))
                dfs.append(t_df)
            return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

        return df.reset_index()


TV = TVScreener()
YF_CLIENT = YFinanceClient(suffix=".KA")


# ── DB ───────────────────────────────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS daily_volume (
                date TEXT, symbol TEXT, volume REAL,
                PRIMARY KEY (date, symbol)
            );
            CREATE TABLE IF NOT EXISTS spike_state (
                symbol TEXT PRIMARY KEY, price REAL, change REAL, volume REAL, updated_at TEXT
            );
        """)
        conn.commit()


# ── Fetchers (thin wrappers around the clients, with Streamlit error UX) ──
def get_live_data(debug: bool = False) -> pd.DataFrame:
    try:
        df = TV.fetch_data(symbols=WATCHLIST)
    except requests.RequestException as e:
        st.error(f"TradingView request failed: {e}")
        return pd.DataFrame()

    if debug:
        st.caption(f"TradingView returned {len(df)} rows")
    if df.empty:
        st.error("TradingView returned no data for the watchlist.")
    return df


def sync_history(period: str = "3mo") -> None:
    try:
        df = YF_CLIENT.fetch_history(WATCHLIST, period=period, interval="1d")
    except Exception as e:
        st.error(f"yfinance request failed: {e}")
        return

    if df.empty:
        st.warning("yfinance returned no historical data.")
        return

    rows = 0
    with get_db() as conn:
        for _, r in df.iterrows():
            if pd.isna(r.get("volume")):
                continue
            date_str = pd.to_datetime(r["date"]).strftime("%Y-%m-%d")
            conn.execute(
                "INSERT OR REPLACE INTO daily_volume (date, symbol, volume) VALUES (?, ?, ?)",
                (date_str, r["symbol"], float(r["volume"])),
            )
            rows += 1
        conn.commit()
    st.success(f"Synced {rows} rows across {df['symbol'].nunique()} symbols.")


# ── App Logic ──────────────────────────────────────────────────────────────
st.set_page_config(layout="centered", page_title="PSX Volume Scanner")
init_db()

st.markdown("<h4 style='margin-bottom:0.2rem'>📈 PSX Volume Scanner</h4>", unsafe_allow_html=True)

col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
scan_clicked = col1.button("🔄 Scan", use_container_width=True)
sync_clicked = col2.button("⬇️ History", use_container_width=True)
period = col3.selectbox("Period", ["1mo", "3mo", "6mo", "1y"], index=1, label_visibility="collapsed")
debug_mode = col4.checkbox("Debug", value=False)

if scan_clicked:
    with st.spinner("Fetching..."):
        live_df = get_live_data(debug=debug_mode)
        if not live_df.empty:
            today = datetime.now(PKT).strftime("%Y-%m-%d")
            now_iso = datetime.now(PKT).isoformat()
            with get_db() as conn:
                for _, r in live_df.iterrows():
                    conn.execute(
                        "INSERT OR REPLACE INTO spike_state (symbol, price, change, volume, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (r["symbol"], r.get("close"), r.get("change"), r.get("volume"), now_iso),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO daily_volume (date, symbol, volume) VALUES (?, ?, ?)",
                        (today, r["symbol"], r.get("volume")),
                    )
                conn.commit()
            st.dataframe(
                live_df.sort_values("volume", ascending=False),
                use_container_width=True, hide_index=True, height=280,
            )

if sync_clicked:
    with st.spinner(f"Syncing {period} of history for {len(WATCHLIST)} symbols..."):
        sync_history(period=period)

# ── Display ────────────────────────────────────────────────────────────────
with get_db() as conn:
    dates = [
        r["date"]
        for r in conn.execute(
            "SELECT DISTINCT date FROM daily_volume ORDER BY date DESC LIMIT 3"
        ).fetchall()
    ]
    if dates:
        tabs = st.tabs(dates)
        for tab, d in zip(tabs, dates):
            with tab:
                df = pd.read_sql(
                    "SELECT symbol, volume FROM daily_volume WHERE date = ? ORDER BY volume DESC",
                    conn, params=(d,),
                )
                st.dataframe(df, use_container_width=True, hide_index=True, height=320)
    else:
        st.caption("No data yet — click Scan or History to populate.")
