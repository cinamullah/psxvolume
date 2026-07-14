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
#  UI
# ══════════════════════════════════════════════════════════════════════════
st.set_page_config(layout="wide", page_title="PSX Scanner")
init_db()

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

* { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; }

.block-container {
    padding-top: 2rem;
    padding-bottom: 2rem;
    max-width: 1000px;
    background: #0a0a0f;
    min-height: 100vh;
}

/* ── Header ── */
.header {
    text-align: center;
    padding: 1.5rem 0 1rem 0;
    margin-bottom: 1.5rem;
    border-bottom: 1px solid #1e1e2e;
}
.header h1 {
    font-size: 1.6rem;
    font-weight: 700;
    margin: 0;
    background: linear-gradient(135deg, #a78bfa, #818cf8);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    letter-spacing: -0.02em;
}
.header .subtitle {
    font-size: 0.8rem;
    color: #6b7280;
    margin: 0.4rem 0 0 0;
    font-weight: 400;
}
.header .subtitle span {
    display: inline-block;
    margin: 0 0.4rem;
}

/* ── Status Bar ── */
.status-bar {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 1.5rem;
    padding: 0.6rem;
    background: linear-gradient(135deg, #111118, #161620);
    border: 1px solid #1e1e2e;
    border-radius: 12px;
    margin-bottom: 1.5rem;
    font-size: 0.78rem;
    color: #9ca3af;
}
.status-item {
    display: flex;
    align-items: center;
    gap: 0.4rem;
}
.status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
}
.status-dot.open { background: #22c55e; box-shadow: 0 0 6px rgba(34,197,94,0.4); }
.status-dot.closed { background: #ef4444; box-shadow: 0 0 6px rgba(239,68,68,0.4); }

/* ── Buttons row ── */
.btn-row {
    display: flex;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
}

/* ── Section Headers ── */
.section-title {
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #6b7280;
    margin: 1.2rem 0 0.5rem 0;
    padding-left: 0.25rem;
}

/* ── Card ── */
.card {
    background: linear-gradient(135deg, #111118, #161620);
    border: 1px solid #1e1e2e;
    border-radius: 12px;
    padding: 1rem 1.2rem;
    margin-bottom: 0.5rem;
}
.card .caption {
    font-size: 0.7rem;
    color: #6b7280;
    margin-bottom: 0.6rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid #1e1e2e;
}

/* ── Dataframe ── */
div[data-testid="stDataFrame"] { font-size: 0.8rem; }
div[data-testid="stDataFrame"] td {
    padding: 0.35rem 0.5rem !important;
    border-bottom: 1px solid #191924;
    color: #d1d5db;
}
div[data-testid="stDataFrame"] th {
    padding: 0.4rem 0.5rem !important;
    background: #13131d;
    color: #6b7280;
    font-weight: 500;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border-bottom: 1px solid #1e1e2e;
}
div[data-testid="stDataFrame"] tbody tr:hover { background: #1a1a28; }

/* ── Buttons ── */
div[data-testid="stButton"] button {
    font-size: 0.8rem;
    font-weight: 500;
    padding: 0.4rem 1rem;
    border-radius: 8px;
    border: 1px solid #2a2a3e;
    background: #161620;
    color: #e5e7eb;
    transition: all 0.15s ease;
}
div[data-testid="stButton"] button:hover {
    background: #1f1f30;
    border-color: #3a3a5e;
    box-shadow: 0 0 12px rgba(129,140,248,0.15);
}

/* ── Alerts ── */
div[data-testid="stAlert"] {
    border-radius: 8px;
    padding: 0.5rem 0.8rem;
    font-size: 0.8rem;
    border: 1px solid #1e1e2e;
}

/* ── Spinner ── */
div.stSpinner > div { margin: 0; }

/* ── Footer status ── */
.footer-status {
    text-align: center;
    font-size: 0.7rem;
    color: #4b5563;
    margin-top: 1.5rem;
    padding-top: 1rem;
    border-top: 1px solid #1e1e2e;
}
</style>
""", unsafe_allow_html=True)

now = datetime.now(TZ)
market_open = is_market_open(now)

st.markdown(f"""
<div class="header">
    <h1>PSX Volume Scanner</h1>
    <p class="subtitle">
        RVOL ≥ {RVOL_THRESHOLD} <span>·</span> {now.strftime('%A, %Y-%m-%d')} <span>·</span> {now.strftime('%H:%M')} PKT
    </p>
</div>
""", unsafe_allow_html=True)

st.markdown(f"""
<div class="status-bar">
    <div class="status-item">
        <span class="status-dot {'open' if market_open else 'closed'}"></span>
        Market {'Open' if market_open else 'Closed'} (9:15–15:30)
    </div>
    <div class="status-item">🔄 Refresh {SNAPSHOT_INTERVAL_MIN}m</div>
</div>
""", unsafe_allow_html=True)

if market_open:
    st.markdown(f'<meta http-equiv="refresh" content="{SNAPSHOT_INTERVAL_MIN * 60}">', unsafe_allow_html=True)

col_a, col_b, col_c = st.columns([1, 1, 5])
with col_a:
    scan_clicked = st.button("Scan Live", use_container_width=True)
with col_b:
    sync_clicked = st.button("Sync History", use_container_width=True)

if scan_clicked:
    with st.spinner("Scanning live market..."):
        if scan_live_data():
            st.success("Live data refreshed")
            st.rerun()

if sync_clicked:
    last_sync_date = get_meta("last_sync_date")
    today_str = now.strftime('%Y-%m-%d')
    if last_sync_date == today_str:
        st.info("Already synced today.")
    with st.spinner("Syncing historical data..."):
        if sync_historical_data():
            st.success("Historical data synced")
            st.rerun()

if not sync_clicked:
    today_str = now.strftime('%Y-%m-%d')
    if get_meta("last_sync_date") != today_str:
        with st.spinner("Auto-syncing..."):
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
        with st.spinner("Auto-scanning..."):
            scan_live_data()

# ─── Today Live ──────────────────────────────────────────────────────────
st.markdown('<div class="section-title">Live — Today</div>', unsafe_allow_html=True)
today_str = now.strftime('%Y-%m-%d')
with sqlite3.connect(DB_NAME) as conn:
    live_df = pd.read_sql("""
        SELECT snap_time, symbol, rvol, price_change, volume_direction, price_direction,
               vol_chg_1h, vol_chg_2h
        FROM live_snapshots
        WHERE snap_time LIKE ?
        ORDER BY snap_time DESC
    """, conn, params=(f"{today_str}%",))

st.markdown('<div class="card">', unsafe_allow_html=True)
if not live_df.empty:
    latest_time = live_df['snap_time'].max()
    live_df = live_df[live_df['snap_time'] == latest_time].drop(columns=['snap_time'])
    live_df = live_df.sort_values('rvol', ascending=False)
    t = latest_time.split(' ')[1]
    st.markdown(f'<div class="caption">{len(live_df)} symbols · {t} PKT</div>', unsafe_allow_html=True)
    live_df.columns = ["Symbol", "RVOL", "Chg%", "Vol Dir", "Pr Dir", "1H Vol%", "2H Vol%"]
    st.dataframe(live_df, use_container_width=True, hide_index=True)
else:
    st.info("No volume spikes detected yet today.")
st.markdown('</div>', unsafe_allow_html=True)

# ─── Historical ──────────────────────────────────────────────────────────
with sqlite3.connect(DB_NAME) as conn:
    hist_dates = [row[0] for row in
                  conn.execute("SELECT date FROM trading_dates ORDER BY idx ASC LIMIT 2").fetchall()]

for i, title in enumerate(["Yesterday", "Two Days Ago"]):
    st.markdown(f'<div class="section-title">{title}</div>', unsafe_allow_html=True)
    st.markdown('<div class="card">', unsafe_allow_html=True)
    if i < len(hist_dates):
        target_date = hist_dates[i]
        st.markdown(f'<div class="caption">{target_date}</div>', unsafe_allow_html=True)
        with sqlite3.connect(DB_NAME) as conn:
            df = pd.read_sql("""
                SELECT symbol, rvol, price_change, volume_direction, price_direction
                FROM spikes WHERE date = ? ORDER BY rvol DESC
            """, conn, params=(target_date,))
        if not df.empty:
            df.columns = ["Symbol", "RVOL", "Chg%", "Vol Dir", "Pr Dir"]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No spikes on this day.")
    else:
        st.info("No data. Click Sync to initialize.")
    st.markdown('</div>', unsafe_allow_html=True)

# ─── Footer ──────────────────────────────────────────────────────────────
s = get_meta("last_scan_time") or "—"
sy = get_meta("last_sync_date") or "—"
st.markdown(f'<div class="footer-status">Scan: {s} &nbsp;·&nbsp; Sync: {sy}</div>', unsafe_allow_html=True)
