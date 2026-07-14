import streamlit as st
import yfinance as yf
import requests
import sqlite3
import pandas as pd
import os
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

# ── Self-contained dark theme setup ─────────────────────────────────────
_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit")
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.toml")
if not os.path.exists(_CONFIG_PATH):
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        f.write('[theme]\nbase = "dark"\n')

# ── Configuration ────────────────────────────────────────────────────────
DB_NAME = "volume_scanner.db"
TZ = ZoneInfo("Asia/Karachi")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
RVOL_THRESHOLD = 1.0
SNAPSHOT_INTERVAL_MIN = 15
TV_URL = "https://scanner.tradingview.com/pakistan/scan"

WATCHLIST = [
    "ABL.KA", "ABOT.KA", "AGP.KA", "AICL.KA", "AIRLINK.KA", "AKBL.KA", "ATLH.KA",
    "ATRL.KA", "BAFL.KA", "BAHL.KA", "BOP.KA", "BPL.KA", "BWCL.KA", "CHCC.KA",
    "CNERGY.KA", "COLG.KA", "DGKC.KA", "EFERT.KA", "ENGRO.KA", "EPCL.KA",
    "FATIMA.KA", "FCCL.KA", "FFC.KA", "FFL.KA", "GHGL.KA", "GHNI.KA", "HALEON.KA",
    "HCAR.KA", "HINOON.KA", "HUBC.KA", "INDU.KA", "INIL.KA", "KAPCO.KA", "KEL.KA",
    "KOHC.KA", "KTML.KA", "LCI.KA", "LUCK.KA", "MARI.KA", "MCB.KA", "MEBL.KA",
    "NBP.KA", "OGDC.KA", "PAEL.KA", "PAKT.KA", "PIBTL.KA", "PIOC.KA", "PKGS.KA",
    "PPL.KA", "PRL.KA", "PSO.KA", "PTC.KA", "SAZEW.KA", "SEARL.KA", "SNGP.KA",
    "SYS.KA", "TGL.KA", "THALL.KA", "TPLP.KA", "TRG.KA", "UBL.KA", "WTL.KA"
]

# ── Database Setup ───────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trading_dates (
                idx INTEGER PRIMARY KEY,
                date TEXT UNIQUE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS spikes (
                date TEXT, symbol TEXT, rvol REAL, price_change REAL,
                volume_direction TEXT, price_direction TEXT,
                UNIQUE(date, symbol)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_snapshots (
                snap_time TEXT, symbol TEXT, cum_volume REAL, price REAL,
                rvol REAL, price_change REAL, volume_direction TEXT,
                price_direction TEXT, vol_chg_1h REAL, vol_chg_2h REAL,
                UNIQUE(snap_time, symbol)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT
            )
        """)
        conn.commit()

def get_meta(key):
    with sqlite3.connect(DB_NAME) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None

def set_meta(key, value):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        conn.commit()

# ── Helpers ──────────────────────────────────────────────────────────────
def is_market_open(now):
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE

def direction_labels(change):
    if change is None or pd.isna(change):
        return "— N/A"
    if change > 0:
        return "▲ Up"
    if change < 0:
        return "▼ Down"
    return "— Flat"

# ── Historical Sync (once a day) ────────────────────────────────────────
def sync_historical_data():
    try:
        raw_data = yf.download(WATCHLIST, period="2mo", group_by='ticker', threads=True, progress=False)
    except Exception as e:
        st.error(f"Failed to download historical data: {e}")
        return False

    sample_ticker = WATCHLIST[0]
    if sample_ticker not in raw_data or raw_data[sample_ticker].empty:
        st.error("No historical data returned. Please try again later.")
        return False

    trading_days = raw_data[sample_ticker].index.strftime('%Y-%m-%d').tolist()
    last_days = trading_days[-5:]

    with sqlite3.connect(DB_NAME) as conn:
        for idx, d_str in enumerate(reversed(last_days)):
            conn.execute("INSERT OR REPLACE INTO trading_dates (idx, date) VALUES (?, ?)", (idx, d_str))

        for symbol in WATCHLIST:
            if symbol not in raw_data:
                continue
            df = raw_data[symbol].copy()
            if df.empty:
                continue
            df = df.dropna(subset=['Volume', 'Close'])
            if df.empty:
                continue

            sma_vol = df['Volume'].rolling(window=20).mean()
            rvol = df['Volume'] / sma_vol
            pct_change = df['Close'].pct_change() * 100
            vol_diff = df['Volume'].diff()
            clean_symbol = symbol.replace('.KA', '')

            for date, val in rvol.tail(5).items():
                if pd.isna(val) or val < RVOL_THRESHOLD:
                    continue
                pchg = pct_change.loc[date]
                vdiff = vol_diff.loc[date]
                pchg = None if pd.isna(pchg) else round(float(pchg), 2)
                vol_dir = "▲ Rising" if (pd.notna(vdiff) and vdiff > 0) else "▼ Falling"
                price_dir = direction_labels(pchg)

                conn.execute("""
                    INSERT OR REPLACE INTO spikes
                    (date, symbol, rvol, price_change, volume_direction, price_direction)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (date.strftime('%Y-%m-%d'), clean_symbol, round(float(val), 2), pchg, vol_dir, price_dir))

        conn.commit()

    set_meta("last_sync_date", datetime.now(TZ).strftime('%Y-%m-%d'))
    set_meta("last_sync_time", datetime.now(TZ).isoformat())
    return True

# ── Live Scan (every 15 min during market hours) ────────────────────────
def fetch_tv_snapshot():
    tv_symbols = ["PSX:" + s.replace(".KA", "") for s in WATCHLIST]
    payload = {
        "symbols": {"tickers": tv_symbols, "query": {"types": []}},
        "columns": ["close", "volume", "change", "relative_volume_10d_calc"],
    }
    try:
        resp = requests.post(TV_URL, json=payload, timeout=15,
                              headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        rows = resp.json().get("data", [])
    except Exception as e:
        st.error(f"Failed to fetch live data from TradingView: {e}")
        return {}

    snapshot = {}
    for row in rows:
        tv_symbol = row.get("s", "")
        clean_symbol = tv_symbol.replace("PSX:", "")
        vals = row.get("d") or []
        if len(vals) < 4:
            continue
        close, volume, change, rvol = vals[0], vals[1], vals[2], vals[3]
        if close is None or volume is None or rvol is None:
            continue
        snapshot[clean_symbol] = {
            "price": float(close),
            "cum_volume": float(volume),
            "price_change": round(float(change), 2) if change is not None else None,
            "rvol": round(float(rvol), 2),
        }
    return snapshot

def _snapshot_before(conn, symbol, today_str, cutoff_dt):
    cutoff_str = cutoff_dt.strftime('%Y-%m-%d %H:%M')
    return conn.execute("""
        SELECT cum_volume, price FROM live_snapshots
        WHERE symbol = ? AND snap_time LIKE ? AND snap_time <= ?
        ORDER BY snap_time DESC LIMIT 1
    """, (symbol, f"{today_str}%", cutoff_str)).fetchone()

def scan_live_data():
    now = datetime.now(TZ)
    tv_data = fetch_tv_snapshot()
    if not tv_data:
        return False

    today_str = now.strftime('%Y-%m-%d')
    snap_time = now.strftime('%Y-%m-%d %H:%M')

    with sqlite3.connect(DB_NAME) as conn:
        for clean_symbol, vals in tv_data.items():
            cum_volume = vals["cum_volume"]
            price = vals["price"]
            price_change = vals["price_change"]
            rvol = vals["rvol"]

            prev_row = _snapshot_before(conn, clean_symbol, today_str, now - timedelta(minutes=1))
            if prev_row:
                volume_direction = "▲ Rising" if cum_volume > prev_row[0] else "▼ Falling"
                price_direction = direction_labels(price - prev_row[1])
            else:
                volume_direction = "— N/A"
                price_direction = "— N/A"

            def pct_change_vs(cutoff_dt):
                row = _snapshot_before(conn, clean_symbol, today_str, cutoff_dt)
                if row and row[0]:
                    return round(((cum_volume - row[0]) / row[0]) * 100, 2)
                return None

            vol_chg_1h = pct_change_vs(now - timedelta(minutes=60))
            vol_chg_2h = pct_change_vs(now - timedelta(minutes=120))

            if rvol < RVOL_THRESHOLD:
                continue

            conn.execute("""
                INSERT OR REPLACE INTO live_snapshots
                (snap_time, symbol, cum_volume, price, rvol, price_change,
                 volume_direction, price_direction, vol_chg_1h, vol_chg_2h)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (snap_time, clean_symbol, cum_volume, price, rvol, price_change,
                  volume_direction, price_direction, vol_chg_1h, vol_chg_2h))
        conn.commit()

    set_meta("last_scan_time", now.isoformat())
    return True

# ── Page Setup & Styling ─────────────────────────────────────────────────
st.set_page_config(layout="wide", page_title="PSX Scanner")
init_db()

st.markdown("""
<style>
    :root { --border: #333844; --muted: #a3a8b4; --accent: #ff4b4b; }
    .block-container { padding-top: 0.5rem; padding-bottom: 0.5rem; max-width: 1150px; }
    h1, h2, h3, h4 { font-family: Georgia, 'Times New Roman', serif; letter-spacing: 0.2px; }
    .report-header { border-bottom: 1px solid var(--border); padding-bottom: 3px; margin-bottom: 3px; }
    .report-header h1 { font-size: 1.1rem; margin: 0; }
    .report-header .caption-line { font-family: Georgia, 'Times New Roman', serif; font-size: 0.7rem; color: var(--muted); margin: 0; line-height: 1.2; }
    .section-title { font-family: Georgia, 'Times New Roman', serif; font-size: 0.75rem; font-weight: 700; color: var(--accent); text-transform: uppercase; letter-spacing: 0.4px; margin: 6px 0 2px 0; border-bottom: 1px solid var(--border); padding-bottom: 2px; }
    .section-card { background-color: #1a1d24; border: 1px solid var(--border); border-radius: 3px; padding: 4px 8px 2px 8px; margin-bottom: 3px; }
    .section-card p[data-testid="stCaptionContainer"] { font-size: 0.65rem !important; margin-bottom: 2px !important; }
    .status-line { font-family: Georgia, 'Times New Roman', serif; font-size: 0.65rem; color: var(--muted); margin: 2px 0 4px 0; }
    div[data-testid="stVerticalBlock"] { gap: 0.15rem !important; }
    div[data-testid="stElementToolbar"] { display: none; }
    div[data-testid="stDataFrame"] { font-size: 0.72rem; }
    div[data-testid="stDataFrame"] td, div[data-testid="stDataFrame"] th { padding: 0.1rem 0.3rem !important; }
    div[data-testid="stButton"] button { font-size: 0.72rem; padding: 0.1rem 0.4rem; min-height: 0; }
    div[data-testid="stAlert"] { padding: 3px 6px; font-size: 0.72rem; }
    div.stSpinner > div { margin: 0; }
    .stApp header { display: none; }
</style>
""", unsafe_allow_html=True)

now = datetime.now(TZ)
market_open = is_market_open(now)

st.markdown(f"""
<div class="report-header">
    <h1>PSX Volume Spike Report</h1>
    <p class="caption-line">RVOL ≥ {RVOL_THRESHOLD} · {now.strftime('%Y-%m-%d %H:%M')} ·
    {'🟢 Open' if market_open else '🔴 Closed'} (9:15–15:30) · {SNAPSHOT_INTERVAL_MIN}m refresh</p>
</div>
""", unsafe_allow_html=True)

if market_open:
    st.markdown(f'<meta http-equiv="refresh" content="{SNAPSHOT_INTERVAL_MIN * 60}">', unsafe_allow_html=True)

col_a, col_b, col_c = st.columns([1, 1, 6])
with col_a:
    scan_clicked = st.button("Scan", use_container_width=True)
with col_b:
    sync_clicked = st.button("Sync", use_container_width=True)

if scan_clicked:
    with st.spinner("Scanning..."):
        if scan_live_data():
            st.success("OK")
            st.rerun()

if sync_clicked:
    last_sync_date = get_meta("last_sync_date")
    today_str = now.strftime('%Y-%m-%d')
    if last_sync_date == today_str:
        st.info("Already synced today. Re-syncing.")
    with st.spinner("Syncing..."):
        if sync_historical_data():
            st.success("Synced")
            st.rerun()

if not sync_clicked:
    today_str = now.strftime('%Y-%m-%d')
    if get_meta("last_sync_date") != today_str:
        with st.spinner("Auto-sync..."):
            sync_historical_data()

if market_open and not scan_clicked:
    last_scan_str = get_meta("last_scan_time")
    should_scan = True
    if last_scan_str:
        try:
            last_scan = datetime.fromisoformat(last_scan_str)
            if (now - last_scan).total_seconds() < SNAPSHOT_INTERVAL_MIN * 60:
                should_scan = False
        except ValueError:
            pass
    if should_scan:
        with st.spinner("Auto-scan..."):
            scan_live_data()

last_scan_time = get_meta("last_scan_time")
last_sync_time = get_meta("last_sync_date")
st.markdown(
    f'<p class="status-line">Scan: {last_scan_time or "—"} · Sync: {last_sync_time or "—"}</p>',
    unsafe_allow_html=True
)

# ── Section 1: Today (Live) ──────────────────────────────────────────────
st.markdown('<div class="section-title">Today — Live</div>', unsafe_allow_html=True)
today_str = now.strftime('%Y-%m-%d')
with sqlite3.connect(DB_NAME) as conn:
    live_df = pd.read_sql("""
        SELECT snap_time, symbol, rvol, price_change, volume_direction, price_direction,
               vol_chg_1h, vol_chg_2h
        FROM live_snapshots
        WHERE snap_time LIKE ?
        ORDER BY snap_time DESC
    """, conn, params=(f"{today_str}%",))

st.markdown('<div class="section-card">', unsafe_allow_html=True)
if not live_df.empty:
    latest_time = live_df['snap_time'].max()
    live_df = live_df[live_df['snap_time'] == latest_time].drop(columns=['snap_time'])
    live_df = live_df.sort_values('rvol', ascending=False)
    st.caption(f"{len(live_df)} sym · {latest_time.split(' ')[1]} · RVOL≥{RVOL_THRESHOLD}")
    live_df.columns = ["Sym", "RVOL", "Chg%", "Vol Dir", "Pr Dir", "1H Vol Δ%", "2H Vol Δ%"]
    st.dataframe(live_df, use_container_width=True, hide_index=True)
else:
    st.info("No spikes yet today.")
st.markdown('</div>', unsafe_allow_html=True)

# ── Sections: Yesterday / Two Days Ago ──────────────────────────────────
with sqlite3.connect(DB_NAME) as conn:
    hist_dates = [row[0] for row in
                  conn.execute("SELECT date FROM trading_dates ORDER BY idx ASC LIMIT 2").fetchall()]

hist_titles = ["Yesterday", "Two Days Ago"]

for i in range(2):
    st.markdown(f'<div class="section-title">{hist_titles[i]}</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    if i < len(hist_dates):
        target_date = hist_dates[i]
        st.caption(target_date)

        with sqlite3.connect(DB_NAME) as conn:
            df = pd.read_sql("""
                SELECT symbol, rvol, price_change, volume_direction, price_direction
                FROM spikes WHERE date = ? ORDER BY rvol DESC
            """, conn, params=(target_date,))

        if not df.empty:
            df.columns = ["Sym", "RVOL", "Chg%", "Vol Dir", "Pr Dir"]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info(f"No spikes RVOL ≥ {RVOL_THRESHOLD}.")
    else:
        st.caption("—")
        st.info("Sync to initialize.")
    st.markdown('</div>', unsafe_allow_html=True)
