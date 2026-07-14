import streamlit as st
import yfinance as yf
import sqlite3
import pandas as pd
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

# ── Self-contained dark theme setup ─────────────────────────────────────
# Streamlit only reads theme settings from .streamlit/config.toml at process
# startup, so we write it out here (next to this script) if it's missing.
# This keeps everything in this one file — just run `streamlit run` and,
# on the very first launch, restart once so the theme takes effect.
_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit")
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.toml")
if not os.path.exists(_CONFIG_PATH):
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        f.write('[theme]\nbase = "dark"\n')

# ── Configuration ────────────────────────────────────────────────────────
DB_NAME = "volume_scanner.db"
TZ = ZoneInfo("Asia/Karachi")          # PSX timezone
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
RVOL_THRESHOLD = 1.0
SNAPSHOT_INTERVAL_MIN = 15

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
                date TEXT,
                symbol TEXT,
                rvol REAL,
                price_change REAL,
                volume_direction TEXT,
                price_direction TEXT,
                UNIQUE(date, symbol)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_snapshots (
                snap_time TEXT,
                symbol TEXT,
                cum_volume REAL,
                price REAL,
                rvol REAL,
                price_change REAL,
                volume_direction TEXT,
                price_direction TEXT,
                vol_chg_1h REAL,
                vol_chg_2h REAL,
                UNIQUE(snap_time, symbol)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
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
    """Returns an up/down/flat label for a numeric change value."""
    if change is None or pd.isna(change):
        return "— N/A"
    if change > 0:
        return "▲ Up"
    if change < 0:
        return "▼ Down"
    return "— Flat"


def trading_fraction_elapsed(now):
    """Fraction of today's trading session that has elapsed, used to annualize live volume."""
    open_dt = datetime.combine(now.date(), MARKET_OPEN, tzinfo=TZ)
    close_dt = datetime.combine(now.date(), MARKET_CLOSE, tzinfo=TZ)
    if now <= open_dt:
        return 0.02
    if now >= close_dt:
        return 1.0
    total = (close_dt - open_dt).total_seconds()
    elapsed = (now - open_dt).total_seconds()
    return max(elapsed / total, 0.02)


# ── Historical Sync (once a day) ────────────────────────────────────────
def sync_historical_data():
    """Fetch daily historical data, calculate RVOL/price-change/direction, save to SQLite."""
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
        # idx 0 = most recent closed trading day (Yesterday), idx 1 = day before that, etc.
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
def get_reference_data():
    """20-day average daily volume + last completed close, per symbol, from daily history."""
    try:
        hist = yf.download(WATCHLIST, period="2mo", group_by='ticker', threads=True, progress=False)
    except Exception as e:
        st.error(f"Failed to download reference data: {e}")
        return {}, {}

    today_str = datetime.now(TZ).strftime('%Y-%m-%d')
    avg_vol, prev_close = {}, {}
    for symbol in WATCHLIST:
        if symbol not in hist:
            continue
        df = hist[symbol].dropna(subset=['Volume', 'Close'])
        if df.empty:
            continue
        # Exclude an in-progress "today" row if present, so prev_close is truly the last close.
        completed = df[df.index.strftime('%Y-%m-%d') < today_str]
        base = completed if not completed.empty else df
        avg_vol[symbol] = base['Volume'].tail(20).mean()
        prev_close[symbol] = base['Close'].iloc[-1]
    return avg_vol, prev_close


def scan_live_data():
    """Pull today's intraday bars (15m) and compute live RVOL / direction metrics."""
    now = datetime.now(TZ)
    avg_vol_map, prev_close_map = get_reference_data()
    if not avg_vol_map:
        return False

    try:
        intraday = yf.download(WATCHLIST, period="1d", interval="15m", group_by='ticker',
                                threads=True, progress=False)
    except Exception as e:
        st.error(f"Failed to download live intraday data: {e}")
        return False

    sample_ticker = WATCHLIST[0]
    if sample_ticker not in intraday or intraday[sample_ticker].empty:
        st.warning("No intraday data available yet (market may be closed).")
        return False

    fraction = trading_fraction_elapsed(now)
    snap_time = now.strftime('%Y-%m-%d %H:%M')

    with sqlite3.connect(DB_NAME) as conn:
        for symbol in WATCHLIST:
            if symbol not in intraday:
                continue
            df = intraday[symbol].dropna(subset=['Volume', 'Close'])
            if df.empty:
                continue

            clean_symbol = symbol.replace('.KA', '')
            cum_volume = float(df['Volume'].sum())
            last_price = float(df['Close'].iloc[-1])

            avg_daily_vol = avg_vol_map.get(symbol)
            prev_close = prev_close_map.get(symbol)
            if not avg_daily_vol or avg_daily_vol == 0:
                continue

            rvol = cum_volume / (avg_daily_vol * fraction)
            price_change = ((last_price - prev_close) / prev_close) * 100 if prev_close else None
            price_change = round(price_change, 2) if price_change is not None else None

            if len(df) >= 2:
                volume_direction = "▲ Rising" if df['Volume'].iloc[-1] > df['Volume'].iloc[-2] else "▼ Falling"
                price_direction = direction_labels(df['Close'].iloc[-1] - df['Close'].iloc[-2])
            else:
                volume_direction = "— N/A"
                price_direction = "— N/A"

            def pct_change_since(bars_back):
                if len(df) > bars_back:
                    past_cum = df['Volume'].iloc[:len(df) - bars_back].sum()
                    if past_cum > 0:
                        return round(((cum_volume - past_cum) / past_cum) * 100, 2)
                return None

            vol_chg_1h = pct_change_since(4)   # 4 x 15min = 1 hour
            vol_chg_2h = pct_change_since(8)   # 8 x 15min = 2 hours

            if rvol >= RVOL_THRESHOLD:
                conn.execute("""
                    INSERT OR REPLACE INTO live_snapshots
                    (snap_time, symbol, cum_volume, price, rvol, price_change,
                     volume_direction, price_direction, vol_chg_1h, vol_chg_2h)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (snap_time, clean_symbol, cum_volume, last_price, round(rvol, 2), price_change,
                      volume_direction, price_direction, vol_chg_1h, vol_chg_2h))
        conn.commit()

    set_meta("last_scan_time", now.isoformat())
    return True


# ── Page Setup & Styling ─────────────────────────────────────────────────
st.set_page_config(layout="wide", page_title="PSX Volume Spike Scanner")
init_db()

st.markdown("""
<style>
    :root {
        --border: #333844;
        --muted: #a3a8b4;
        --accent: #ff4b4b;
    }
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 1.5rem;
        max-width: 1150px;
    }
    h1, h2, h3, h4 {
        font-family: Georgia, 'Times New Roman', serif;
        letter-spacing: 0.2px;
    }
    .report-header {
        border-bottom: 1px solid var(--border);
        padding-bottom: 8px;
        margin-bottom: 10px;
    }
    .report-header h1 {
        font-size: 1.5rem;
        margin: 0 0 4px 0;
    }
    .report-header .caption-line {
        font-family: Georgia, 'Times New Roman', serif;
        font-size: 0.85rem;
        color: var(--muted);
        margin: 0;
        line-height: 1.4;
    }
    .section-title {
        font-family: Georgia, 'Times New Roman', serif;
        font-size: 0.95rem;
        font-weight: 700;
        color: var(--accent);
        text-transform: uppercase;
        letter-spacing: 0.6px;
        margin: 16px 0 6px 0;
        border-bottom: 1px solid var(--border);
        padding-bottom: 4px;
    }
    .section-card {
        background-color: #1a1d24;
        border: 1px solid var(--border);
        border-radius: 4px;
        padding: 10px 14px 6px 14px;
        margin-bottom: 6px;
    }
    .section-card p[data-testid="stCaptionContainer"] {
        font-size: 0.82rem !important;
        margin-bottom: 6px !important;
    }
    .status-line {
        font-family: Georgia, 'Times New Roman', serif;
        font-size: 0.8rem;
        color: var(--muted);
        margin: 4px 0 14px 0;
    }
    div[data-testid="stVerticalBlock"] { gap: 0.5rem !important; }
    div[data-testid="stElementToolbar"] { display: none; }
    div[data-testid="stDataFrame"] { font-size: 0.85rem; }
    div[data-testid="stButton"] button {
        font-size: 0.85rem;
        padding: 0.3rem 0.75rem;
    }
    div[data-testid="stAlert"] {
        padding: 8px 12px;
        font-size: 0.85rem;
    }
</style>
""", unsafe_allow_html=True)

now = datetime.now(TZ)
market_open = is_market_open(now)

st.markdown(f"""
<div class="report-header">
    <h1>PSX Volume Spike Report</h1>
    <p class="caption-line">RVOL threshold ≥ {RVOL_THRESHOLD} &nbsp;·&nbsp; Karachi time: {now.strftime('%Y-%m-%d %H:%M:%S')} &nbsp;·&nbsp; 
    Market {'🟢 OPEN' if market_open else '🔴 CLOSED'} (session 9:15–15:30)</p>
</div>
""", unsafe_allow_html=True)

# Auto-refresh the page every 15 minutes while the market is open, so the live
# section keeps itself current without the user needing to click anything.
if market_open:
    st.markdown(f'<meta http-equiv="refresh" content="{SNAPSHOT_INTERVAL_MIN * 60}">', unsafe_allow_html=True)

# ── Buttons ──────────────────────────────────────────────────────────────
col_a, col_b, col_c = st.columns([1, 1, 4])
with col_a:
    scan_clicked = st.button("Scan Live", use_container_width=True)
with col_b:
    sync_clicked = st.button("Sync Historical", use_container_width=True)

if scan_clicked:
    with st.spinner("Scanning live market data..."):
        if scan_live_data():
            st.success("Live scan complete.")
            st.rerun()

if sync_clicked:
    last_sync_date = get_meta("last_sync_date")
    today_str = now.strftime('%Y-%m-%d')
    if last_sync_date == today_str:
        st.info("Historical data was already synced today. Re-syncing anyway.")
    with st.spinner("Processing historical data..."):
        if sync_historical_data():
            st.success("Historical data synced successfully!")
            st.rerun()

# Auto-scan once every 15 minutes during market hours, without requiring a click.
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
        with st.spinner("Auto-scanning live market data (15-min cycle)..."):
            scan_live_data()

last_scan_time = get_meta("last_scan_time")
last_sync_time = get_meta("last_sync_date")
st.markdown(
    f'<p class="status-line">Last live scan: {last_scan_time or "never"} &nbsp;·&nbsp; '
    f'Last historical sync: {last_sync_time or "never"}</p>',
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
    st.caption(f"As of {latest_time} (PKT) · refreshes every {SNAPSHOT_INTERVAL_MIN} min while market is open")
    live_df = live_df[live_df['snap_time'] == latest_time].drop(columns=['snap_time'])
    live_df = live_df.sort_values('rvol', ascending=False)
    live_df.columns = ["Symbol", "Relative Volume", "Price Change %", "Volume Direction",
                        "Price Direction", "Vol Δ vs 1H Ago %", "Vol Δ vs 2H Ago %"]
    st.dataframe(live_df, use_container_width=True, hide_index=True)
else:
    st.info("No live spikes yet today. Click 'Scan Live Data' during market hours (9:15–15:30 PKT).")
st.markdown('</div>', unsafe_allow_html=True)

# ── Sections 2 & 3: Yesterday / Two Days Ago (Historical, stacked) ──────
with sqlite3.connect(DB_NAME) as conn:
    hist_dates = [row[0] for row in
                  conn.execute("SELECT date FROM trading_dates ORDER BY idx ASC LIMIT 2").fetchall()]

hist_titles = ["Yesterday", "Two Days Ago"]

for i in range(2):
    st.markdown(f'<div class="section-title">{hist_titles[i]}</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    if i < len(hist_dates):
        target_date = hist_dates[i]
        st.caption(f"Date: {target_date}")

        with sqlite3.connect(DB_NAME) as conn:
            df = pd.read_sql("""
                SELECT symbol, rvol, price_change, volume_direction, price_direction
                FROM spikes WHERE date = ? ORDER BY rvol DESC
            """, conn, params=(target_date,))

        if not df.empty:
            df.columns = ["Symbol", "Relative Volume", "Price Change %",
                          "Volume Direction", "Price Direction"]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info(f"No spikes crossed RVOL ≥ {RVOL_THRESHOLD} on this day.")
    else:
        st.caption("Date: Unknown")
        st.info("Click 'Sync Historical Data' above to initialize this table.")
    st.markdown('</div>', unsafe_allow_html=True)
