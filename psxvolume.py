import streamlit as st
import yfinance as yf
import requests
import sqlite3
import pandas as pd
import os
import html
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
                idx INTEGER PRIMARY KEY, date TEXT UNIQUE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS spikes (
                date TEXT, symbol TEXT, rvol REAL, price_change REAL,
                volume_direction TEXT, price_direction TEXT, UNIQUE(date, symbol)
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
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)
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

def is_market_open(now):
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE

def direction_labels(change):
    if change is None or pd.isna(change):
        return "—"
    if change > 0:
        return "▲ Up"
    if change < 0:
        return "▼ Down"
    return "— Flat"

# ── Historical Sync ──────────────────────────────────────────────────────
def sync_historical_data():
    try:
        raw_data = yf.download(WATCHLIST, period="2mo", group_by='ticker', threads=True, progress=False)
    except Exception as e:
        st.error(f"Failed to download historical data: {e}")
        return False

    sample_ticker = WATCHLIST[0]
    if sample_ticker not in raw_data or raw_data[sample_ticker].empty:
        st.error("No historical data returned.")
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

# ── Live Scan ────────────────────────────────────────────────────────────
def fetch_tv_snapshot():
    tv_symbols = ["PSX:" + s.replace(".KA", "") for s in WATCHLIST]
    payload = {
        "symbols": {"tickers": tv_symbols, "query": {"types": []}},
        "columns": ["close", "volume", "change", "relative_volume_10d_calc"],
    }
    try:
        resp = requests.post(TV_URL, json=payload, timeout=15, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        rows = resp.json().get("data", [])
    except Exception as e:
        st.error(f"Failed to fetch live data: {e}")
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
        st.warning("No live data returned. Market may be closed.")
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
                volume_direction = "—"
                price_direction = "—"

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

# ══════════════════════════════════════════════════════════════════════════
#  UI  (inspired by psx-scanner.py)
# ══════════════════════════════════════════════════════════════════════════
st.set_page_config(layout="wide", page_title="PSX Volume Spike Scanner")
init_db()

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Libre+Baskerville:ital,wght@0,400;0,700;1,400&family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@300;400;500;600;700&display=swap');

* { box-sizing: border-box; }
html, body, [class*="css"] {
    background: #060a14 !important;
    color: #d8dde8 !important;
    font-family: 'Inter', -apple-system, sans-serif !important;
}
footer, #MainMenu, [data-testid="stToolbar"] { visibility: hidden !important; }
.main .block-container {
    max-width: 1240px !important;
    padding: 1.8rem 2.2rem !important;
    margin: 0 auto !important;
}

/* ── Masthead ── */
.report-masthead { padding-bottom: 2px; margin-bottom: 0; }
.report-title {
    font-family: 'Libre Baskerville', Georgia, serif;
    font-size: 1.45rem; font-weight: 700;
    letter-spacing: 0.18em; text-transform: uppercase;
    background: linear-gradient(135deg, #e8ecf5 0%, #a0aec0 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    margin: 0; line-height: 1.2;
}
.report-subtitle {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.68rem; color: #4a5568; margin-top: 3px; letter-spacing: 0.06em;
}

/* ── Divider ── */
.double-rule { border-bottom: 3px double #1e2a3a; margin: 10px 0 16px; }

/* ── Market Bar ── */
.market-bar {
    display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px;
    border: 1px solid #1a2535; padding: 10px 20px;
    margin-bottom: 12px; background: linear-gradient(135deg,#0a0f1e 0%,#0c1428 100%);
    font-family: 'JetBrains Mono', monospace; font-size: 0.78rem;
    box-shadow: 0 1px 12px rgba(0,0,0,0.4);
}
.market-item { text-align: center; min-width: 80px; }
.market-label {
    font-size: 0.58rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.14em;
    color: #4a5568; margin-bottom: 3px;
}
.market-value { font-size: 0.90rem; font-weight: 700; color: #d8dde8; }

/* ── Section Headers ── */
.section-header {
    font-family: 'Libre Baskerville', Georgia, serif;
    font-size: 0.92rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.14em;
    border-bottom: 2px solid #1e2a3a; padding-bottom: 5px;
    margin-top: 28px; margin-bottom: 3px; color: #d8dde8;
}
.section-meta {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.64rem; color: #4a5568; margin-bottom: 10px; letter-spacing: 0.04em;
}

/* ── Card ── */
.data-card {
    border: 1px solid #1a2535;
    background: linear-gradient(135deg,#0a0f1e 0%,#0c1428 100%);
    padding: 1rem 1.2rem;
    margin-bottom: 6px;
}

/* ── Alerts ── */
.paper-alert {
    border: 1px solid rgba(248,113,113,0.28);
    border-left: 4px solid #f87171;
    padding: 8px 14px; margin-bottom: 12px;
    font-size: 0.74rem; color: #fca5a5;
    background: rgba(239,68,68,0.05);
}
.paper-info {
    border: 1px solid #1a2535; border-left: 4px solid #243040;
    padding: 8px 14px; margin-bottom: 12px;
    font-size: 0.74rem; color: #8899aa;
    background: rgba(10,15,30,0.5);
}

/* ── DataFrames ── */
[data-testid="stDataFrame"] { background: transparent !important; border-radius: 0 !important; }
[data-testid="stDataFrame"] > div { border: 1px solid #1a2535 !important; }
thead tr th {
    background: #09101f !important; color: #4a5568 !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.60rem !important; font-weight: 700 !important;
    letter-spacing: 0.10em !important; text-transform: uppercase;
    padding: 9px 7px !important; border-bottom: 2px solid #1e2a3a !important;
}
tbody tr { border-bottom: 1px solid #111928 !important; }
tbody tr:nth-child(even) { background: rgba(10,15,30,0.35) !important; }
tbody tr:hover { background: rgba(22,32,52,0.55) !important; }
tbody td {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.69rem !important; color: #d8dde8 !important;
    padding: 5px 7px !important;
}

/* ── Buttons ── */
.stButton button {
    background: #09101f !important; border: 1px solid #1e2a3a !important;
    border-radius: 0 !important; color: #d8dde8 !important;
    padding: 5px 14px !important; font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.68rem !important; font-weight: 700 !important;
    text-transform: uppercase !important; letter-spacing: 0.10em !important;
    min-height: unset !important; line-height: 1.4 !important;
    transition: all 0.15s ease !important;
}
.stButton button:hover {
    background: #111928 !important; border-color: #2d4060 !important;
    box-shadow: 0 0 8px rgba(96,165,250,0.12) !important;
}

/* ── Footer ── */
.report-footer {
    border-top: 1px solid #1a2535; margin-top: 40px; padding-top: 10px;
    font-family: 'JetBrains Mono', monospace; font-size: 0.58rem;
    color: #2d3a4a; line-height: 1.8;
}

div.stSpinner > div { margin: 0; }
hr { border-color: #1a2535 !important; margin: 1.2rem 0 !important; }

@media (max-width: 768px) {
    .main .block-container { padding: 1rem !important; }
    .report-title { font-size: 1.1rem; }
    .market-bar { flex-wrap: wrap; gap: 8px; padding: 8px 12px; }
}
</style>
""", unsafe_allow_html=True)

now = datetime.now(TZ)
market_open = is_market_open(now)

# ── Header ───────────────────────────────────────────────────────────────
head_cols = st.columns([8, 1, 1])
with head_cols[0]:
    st.markdown(f"""
    <div class="report-masthead">
        <div class="report-title">PSX Volume Spike Scanner</div>
        <div class="report-subtitle">
            Relative Volume &middot; Live & Historical &middot;
            {now.strftime("%A, %B %d, %Y")} &middot; {now.strftime("%H:%M")} PKT
        </div>
    </div>
    """, unsafe_allow_html=True)
with head_cols[1]:
    st.markdown('<div style="padding-top:8px"></div>', unsafe_allow_html=True)
    scan_clicked = st.button("SCAN", use_container_width=True)
with head_cols[2]:
    st.markdown('<div style="padding-top:8px"></div>', unsafe_allow_html=True)
    sync_clicked = st.button("SYNC", use_container_width=True)

st.markdown('<div class="double-rule"></div>', unsafe_allow_html=True)

# ── Market Bar ───────────────────────────────────────────────────────────
state_text = "OPEN ●" if market_open else "CLOSED"
state_color = "#34d399" if market_open else "#8899aa"
st.markdown(f"""
<div class="market-bar">
    <div class="market-item">
        <div class="market-label">Market</div>
        <div class="market-value" style="color:{state_color}">{state_text}</div>
    </div>
    <div class="market-item">
        <div class="market-label">Session</div>
        <div class="market-value">9:15 – 15:30</div>
    </div>
    <div class="market-item">
        <div class="market-label">RVOL Threshold</div>
        <div class="market-value">≥ {RVOL_THRESHOLD}x</div>
    </div>
    <div class="market-item">
        <div class="market-label">Refresh</div>
        <div class="market-value">{SNAPSHOT_INTERVAL_MIN} min</div>
    </div>
    <div class="market-item">
        <div class="market-label">Watchlist</div>
        <div class="market-value">{len(WATCHLIST)} sym</div>
    </div>
</div>
""", unsafe_allow_html=True)

if market_open:
    st.markdown(f'<meta http-equiv="refresh" content="{SNAPSHOT_INTERVAL_MIN * 60}">', unsafe_allow_html=True)

# ── Actions ──────────────────────────────────────────────────────────────
if scan_clicked:
    with st.spinner("Scanning live market data..."):
        if scan_live_data():
            st.rerun()

if sync_clicked:
    last_sync_date = get_meta("last_sync_date")
    today_str = now.strftime('%Y-%m-%d')
    if last_sync_date == today_str:
        st.markdown(
            '<div class="paper-info">Already synced today. Re-syncing.</div>',
            unsafe_allow_html=True,
        )
    with st.spinner("Syncing historical data..."):
        if sync_historical_data():
            st.rerun()

# Auto-sync (once per day)
if not sync_clicked:
    today_str = now.strftime('%Y-%m-%d')
    if get_meta("last_sync_date") != today_str:
        with st.spinner("Auto-syncing historical data..."):
            sync_historical_data()

# Auto-scan (every 15 min during market hours)
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
        with st.spinner("Auto-scanning market data..."):
            scan_live_data()

# ── Today Live ───────────────────────────────────────────────────────────
st.markdown('<div class="section-header"><b>Today</b> — Live Spikes</div>', unsafe_allow_html=True)

today_str = now.strftime('%Y-%m-%d')
with sqlite3.connect(DB_NAME) as conn:
    live_df = pd.read_sql("""
        SELECT snap_time, symbol, rvol, price_change, volume_direction, price_direction,
               vol_chg_1h, vol_chg_2h
        FROM live_snapshots
        WHERE snap_time LIKE ?
        ORDER BY snap_time DESC
    """, conn, params=(f"{today_str}%",))

st.markdown('<div class="data-card">', unsafe_allow_html=True)
if not live_df.empty:
    latest_time = live_df['snap_time'].max()
    live_df = live_df[live_df['snap_time'] == latest_time].drop(columns=['snap_time'])
    live_df = live_df.sort_values('rvol', ascending=False)
    t = latest_time.split(' ')[1]
    st.markdown(
        f'<div class="section-meta" style="margin-bottom:6px">{len(live_df)} symbols &middot; {t} PKT</div>',
        unsafe_allow_html=True,
    )
    live_df.columns = ["Symbol", "RVOL", "Chg %", "Vol Dir", "Pr Dir", "1H Vol Δ %", "2H Vol Δ %"]
    st.dataframe(live_df, use_container_width=True, hide_index=True)
else:
    st.markdown(
        '<div class="paper-info">No volume spikes detected yet today.</div>',
        unsafe_allow_html=True,
    )
st.markdown('</div>', unsafe_allow_html=True)

# ── Yesterday & Two Days Ago ────────────────────────────────────────────
with sqlite3.connect(DB_NAME) as conn:
    hist_dates = [row[0] for row in
                  conn.execute("SELECT date FROM trading_dates ORDER BY idx ASC LIMIT 2").fetchall()]

for i, title in enumerate(["Yesterday", "Two Days Ago"]):
    st.markdown(f'<div class="section-header"><b>{title}</b></div>', unsafe_allow_html=True)
    st.markdown('<div class="data-card">', unsafe_allow_html=True)
    if i < len(hist_dates):
        target_date = hist_dates[i]
        with sqlite3.connect(DB_NAME) as conn:
            df = pd.read_sql("""
                SELECT symbol, rvol, price_change, volume_direction, price_direction
                FROM spikes WHERE date = ? ORDER BY rvol DESC
            """, conn, params=(target_date,))
        if not df.empty:
            df.columns = ["Symbol", "RVOL", "Chg %", "Vol Dir", "Pr Dir"]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.markdown(
                '<div class="paper-info">No spikes on this day.</div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            '<div class="paper-info">No data. Click SYNC to initialize.</div>',
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)

# ── Footer ───────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="report-footer">
    Generated {now.strftime("%Y-%m-%d %H:%M:%S")} PKT
    &middot; Source: TradingView Scanner API, Yahoo Finance
    &middot; Watchlist: {len(WATCHLIST)} symbols<br>
    Spikes are identified when Relative Volume (based on 10-day average from TV or 20-day SMA from Yahoo)
    meets or exceeds {RVOL_THRESHOLD}x. Quantitative screening tool only &mdash; not investment advice.
</div>
""", unsafe_allow_html=True)
