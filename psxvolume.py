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

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ── Setup ──────────────────────────────────────────────────────────────────
DB_PATH = "volume_spikes.db"
PKT = ZoneInfo("Asia/Karachi")
SCAN_INTERVAL_MINUTES = 15
BASELINE_LOOKBACK = "1mo"  # ~20 trading days, within yfinance's 60-day 15m limit

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
    """Queries TradingView's public Scanner API — all requested symbols in
    a single request."""

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
        raw_rows = response.json().get("data", [])
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
            CREATE TABLE IF NOT EXISTS intraday_log (
                symbol TEXT, ts TEXT, day_volume REAL, price REAL
            );
            CREATE TABLE IF NOT EXISTS intraday_baseline (
                symbol TEXT, time_bucket TEXT, avg_volume REAL,
                PRIMARY KEY (symbol, time_bucket)
            );
            CREATE TABLE IF NOT EXISTS spike_candidates (
                symbol TEXT PRIMARY KEY,
                date TEXT,
                rvol_daily REAL,
                rvol_15m REAL,
                volume_direction TEXT,
                price_direction TEXT,
                signal TEXT,
                status TEXT,
                flagged_at TEXT,
                last_checked TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT
            );
        """)
        conn.commit()


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


# ── Fetchers ─────────────────────────────────────────────────────────────
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
    """Backfills daily volume history AND the 15-min intraday baseline."""
    try:
        df = YF_CLIENT.fetch_history(WATCHLIST, period=period, interval="1d")
    except Exception as e:
        st.error(f"yfinance daily request failed: {e}")
        return

    rows = 0
    if not df.empty:
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
        st.success(f"Daily history: synced {rows} rows across {df['symbol'].nunique()} symbols.")
    else:
        st.warning("yfinance returned no daily historical data.")

    baseline_rows = build_intraday_baseline(WATCHLIST)
    if baseline_rows:
        st.success(f"Intraday baseline: {baseline_rows} time-bucket averages built.")
    else:
        st.info("No intraday (15m) baseline data available from yfinance for these symbols yet.")


def build_intraday_baseline(symbols) -> int:
    """Builds the average 15-min volume per time-of-day bucket, over the
    last ~20 trading days, used as the RVOL(15m) comparison baseline."""
    formatted = [f"{s.upper()}.KA" for s in symbols]
    try:
        raw = yf.download(
            tickers=formatted, period=BASELINE_LOOKBACK, interval="15m",
            auto_adjust=True, progress=False, threads=True,
        )
    except Exception as e:
        st.warning(f"Intraday baseline fetch failed: {e}")
        return 0
    if raw.empty:
        return 0

    written = 0
    with get_db() as conn:
        def store(sym: str, sub: pd.DataFrame):
            nonlocal written
            sub = sub[["Volume"]].dropna().reset_index()
            dt_col = sub.columns[0]
            sub["bucket"] = pd.to_datetime(sub[dt_col]).dt.strftime("%H:%M")
            for bucket, avg_vol in sub.groupby("bucket")["Volume"].mean().items():
                conn.execute(
                    "INSERT OR REPLACE INTO intraday_baseline (symbol, time_bucket, avg_volume) "
                    "VALUES (?, ?, ?)",
                    (sym, bucket, float(avg_vol)),
                )
                written += 1

        if isinstance(raw.columns, pd.MultiIndex):
            for ticker in raw.columns.levels[1]:
                if ticker not in raw.columns.get_level_values(1):
                    continue
                sub = raw.xs(ticker, axis=1, level=1)
                if not sub.empty:
                    store(ticker.replace(".KA", ""), sub)
        elif len(symbols) == 1:
            store(symbols[0].upper(), raw)
        conn.commit()
    return written


# ── RVOL / spike logic ─────────────────────────────────────────────────────
def compute_stage1(conn, today: str, rvol_threshold: float) -> List[str]:
    """Daily scan: flag symbols whose volume-so-far is >= threshold x their
    20-day average daily volume."""
    conn.execute("DELETE FROM spike_candidates WHERE date != ?", (today,))
    candidates = []
    for row in conn.execute("SELECT symbol, volume FROM daily_volume WHERE date=?", (today,)):
        sym, vol = row["symbol"], row["volume"]
        if vol is None:
            continue
        avg_row = conn.execute(
            "SELECT AVG(volume) as avg20 FROM ("
            "  SELECT volume FROM daily_volume WHERE symbol=? AND date<? "
            "  ORDER BY date DESC LIMIT 20"
            ")",
            (sym, today),
        ).fetchone()
        avg20 = avg_row["avg20"]
        if not avg20 or avg20 <= 0:
            continue
        rvol_daily = vol / avg20
        if rvol_daily >= rvol_threshold:
            existing = conn.execute(
                "SELECT flagged_at FROM spike_candidates WHERE symbol=?", (sym,)
            ).fetchone()
            is_new = existing is None
            flagged_at = existing["flagged_at"] if existing else datetime.now(PKT).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO spike_candidates "
                "(symbol, date, rvol_daily, status, flagged_at) VALUES (?, ?, ?, ?, ?)",
                (sym, today, rvol_daily, "candidate", flagged_at),
            )
            candidates.append(sym)
            if is_new:
                maybe_send_alert(
                    sym, f"📊 {sym} flagged as Volume Spike Candidate — RVOL {rvol_daily:.2f}x"
                )
    conn.commit()
    return candidates


def compute_stage2(conn, candidates: List[str], now_iso: str):
    """15-min monitoring for today's Stage-1 candidates: interval volume vs
    baseline, plus price/volume direction and a combined signal."""
    dt = datetime.now(PKT)
    bucket_minute = (dt.minute // 15) * 15
    bucket = dt.strftime("%H:") + f"{bucket_minute:02d}"

    for sym in candidates:
        logs = conn.execute(
            "SELECT ts, day_volume, price FROM intraday_log WHERE symbol=? ORDER BY ts DESC LIMIT 3",
            (sym,),
        ).fetchall()
        if len(logs) < 2:
            continue  # not enough snapshots yet to compute an interval

        latest, prev = logs[0], logs[1]
        interval_vol_now = latest["day_volume"] - prev["day_volume"]
        if interval_vol_now < 0:
            interval_vol_now = latest["day_volume"]  # new trading day rollover

        volume_direction = "flat"
        if len(logs) >= 3:
            older = logs[2]
            interval_vol_prev = prev["day_volume"] - older["day_volume"]
            if interval_vol_prev < 0:
                interval_vol_prev = prev["day_volume"]
            if interval_vol_now > interval_vol_prev * 1.05:
                volume_direction = "increasing"
            elif interval_vol_now < interval_vol_prev * 0.95:
                volume_direction = "decreasing"

        price_direction = "flat"
        if latest["price"] is not None and prev["price"] is not None:
            if latest["price"] > prev["price"]:
                price_direction = "up"
            elif latest["price"] < prev["price"]:
                price_direction = "down"

        baseline_row = conn.execute(
            "SELECT avg_volume FROM intraday_baseline WHERE symbol=? AND time_bucket=?",
            (sym, bucket),
        ).fetchone()
        rvol_15m = (
            interval_vol_now / baseline_row["avg_volume"]
            if baseline_row and baseline_row["avg_volume"]
            else None
        )

        if price_direction == "up" and volume_direction == "increasing":
            signal = "Strong Bullish"
        elif price_direction == "up" and volume_direction == "decreasing":
            signal = "Warning"
        elif price_direction == "down" and volume_direction == "increasing":
            signal = "Bearish Pressure"
        else:
            signal = "Neutral"

        prev_row = conn.execute("SELECT signal FROM spike_candidates WHERE symbol=?", (sym,)).fetchone()
        prev_signal = prev_row["signal"] if prev_row else None

        conn.execute(
            "UPDATE spike_candidates SET rvol_15m=?, volume_direction=?, price_direction=?, "
            "signal=?, last_checked=? WHERE symbol=?",
            (rvol_15m, volume_direction, price_direction, signal, now_iso, sym),
        )

        if signal == "Strong Bullish" and prev_signal != "Strong Bullish":
            rvol_txt = f"{rvol_15m:.2f}x" if rvol_15m else "n/a"
            maybe_send_alert(
                sym, f"🚀 {sym} Strong Bullish — price up, volume increasing (RVOL 15m: {rvol_txt})"
            )
    conn.commit()


def maybe_send_alert(symbol: str, message: str):
    if not st.session_state.get("alerts_enabled") or not st.session_state.get("ntfy_url"):
        return
    try:
        requests.post(
            st.session_state["ntfy_url"],
            data=message.encode("utf-8"),
            headers={"Title": f"PSX Volume Alert: {symbol}"},
            timeout=10,
        )
    except requests.RequestException:
        pass  # a failed notification shouldn't break the scan


def run_cycle(debug: bool = False) -> pd.DataFrame:
    """Full pipeline: fetch live data, store it, run Stage 1 + Stage 2, alert."""
    live_df = get_live_data(debug=debug)
    if live_df.empty:
        return live_df

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
            conn.execute(
                "INSERT INTO intraday_log (symbol, ts, day_volume, price) VALUES (?, ?, ?, ?)",
                (r["symbol"], now_iso, r.get("volume"), r.get("close")),
            )
        conn.commit()

        threshold = st.session_state.get("rvol_threshold", 2.0)
        candidates = compute_stage1(conn, today, threshold)
        compute_stage2(conn, candidates, now_iso)
        set_meta(conn, "last_run", now_iso)
        conn.commit()

    return live_df


# ── App Logic ──────────────────────────────────────────────────────────────
st.set_page_config(layout="centered", page_title="Volume Spike Detector")
init_db()

if HAS_AUTOREFRESH:
    st_autorefresh(interval=SCAN_INTERVAL_MINUTES * 60 * 1000, key="auto_cycle")

st.markdown("<h4 style='margin-bottom:0.2rem'>📈 Volume Spike Detector</h4>", unsafe_allow_html=True)

with st.expander("⚙️ Settings", expanded=False):
    st.session_state["ntfy_url"] = st.text_input(
        "NTFY topic URL", value=st.session_state.get("ntfy_url", ""),
        placeholder="https://ntfy.sh/your_topic",
    )
    st.session_state["alerts_enabled"] = st.checkbox(
        "Enable NTFY alerts", value=st.session_state.get("alerts_enabled", False)
    )
    st.session_state["rvol_threshold"] = st.number_input(
        "Daily RVOL threshold", min_value=1.0, max_value=10.0,
        value=st.session_state.get("rvol_threshold", 2.0), step=0.1,
    )
    if not HAS_AUTOREFRESH:
        st.caption("Install `streamlit-autorefresh` for automatic 15-min cycles while this page stays open.")

col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
scan_clicked = col1.button("🔄 Scan", use_container_width=True)
sync_clicked = col2.button("⬇️ History", use_container_width=True)
period = col3.selectbox("Period", ["1mo", "3mo", "6mo", "1y"], index=1, label_visibility="collapsed")
debug_mode = col4.checkbox("Debug", value=False)

# Auto-trigger a cycle if 15+ minutes have passed since the last one.
with get_db() as _conn:
    _last_run = get_meta(_conn, "last_run")
_should_auto_run = (
    _last_run is None
    or (datetime.now(PKT) - datetime.fromisoformat(_last_run)).total_seconds() >= SCAN_INTERVAL_MINUTES * 60
)

if scan_clicked or _should_auto_run:
    with st.spinner("Fetching..."):
        live_df = run_cycle(debug=debug_mode)
        if not live_df.empty:
            st.dataframe(
                live_df.sort_values("volume", ascending=False),
                use_container_width=True, hide_index=True, height=280,
            )

if sync_clicked:
    with st.spinner(f"Syncing {period} of history + intraday baseline for {len(WATCHLIST)} symbols..."):
        sync_history(period=period)

# ── Volume Spikes section ──────────────────────────────────────────────────
st.markdown("<h5 style='margin-top:1rem'>🎯 Volume Spikes</h5>", unsafe_allow_html=True)
with get_db() as conn:
    today = datetime.now(PKT).strftime("%Y-%m-%d")
    spikes = pd.read_sql(
        "SELECT symbol, rvol_daily, rvol_15m, price_direction, volume_direction, signal, last_checked "
        "FROM spike_candidates WHERE date = ? ORDER BY rvol_daily DESC",
        conn, params=(today,),
    )

if spikes.empty:
    st.caption("No volume spike candidates today yet.")
else:
    spikes.insert(0, "rank", range(1, len(spikes) + 1))
    spikes["rvol_daily"] = spikes["rvol_daily"].round(2)
    spikes["rvol_15m"] = spikes["rvol_15m"].round(2)
    st.dataframe(spikes, use_container_width=True, hide_index=True, height=320)

# ── Daily volume history ────────────────────────────────────────────────────
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
