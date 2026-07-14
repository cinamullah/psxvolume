"""
PSX Volume Spike Detector — single-file Streamlit app.

Auto-scans live data every 15 min, but only while PSX is open: Mon-Fri,
9:15 AM - 3:30 PM Pakistan time (Asia/Karachi). Two buttons let you force
things manually:
  - Scan  : fetch live price/volume right now and recompute spikes.
  - Sync  : backfill ~25 trading days of real historical daily volume per
            symbol (via Yahoo Finance) so the 20-day average is accurate
            from day one, instead of slowly building up from live scans.

  - RVOL = today's volume / 20-day average daily volume. Candidate if >= 2x.
  - 15-min trend for candidates: Increasing/Decreasing vs that symbol's own
    average for the same time-of-day, plus price direction -> Strong
    Bullish (price + volume both up) or Warning (price up, volume fading).
  - Three sections: Today, Yesterday, Two Days Ago — each Top 10 by RVOL.
    Today's numbers become "Yesterday" automatically once a new trading
    day starts.

Run:
    pip install streamlit requests pandas yfinance
    streamlit run app.py
"""

import os
import sqlite3
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "volume_spikes.db")
POLL_SECONDS = 15 * 60
RVOL_SPIKE_THRESHOLD = 1.5
AVG_DAYS = 20
TOP_N = 10
PKT = ZoneInfo("Asia/Karachi")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

KSE100 = [
    "ABL", "ABOT", "AGP", "AHCL", "AICL", "AIRLINK", "AKBL", "APL", "ATLH", "ATRL",
    "BAFL", "BAHL", "BNWM", "BOP", "BWCL", "CHCC", "CNERGY", "COLG", "CPHL", "DCR",
    "DGKC", "EFERT", "ENGROH", "FABL", "FATIMA", "FCCL", "FFC", "FFL", "FHAM", "GADT",
    "GAL", "GHGL", "GHNI", "GLAXO", "HALEON", "HBL", "HCAR", "HGFA", "HINOON", "HMB",
    "HUBC", "HUMNL", "IBFL", "ILP", "INDU", "INIL", "ISL", "JDWS", "JVDC", "KAPCO",
    "KEL", "KOHC", "KTML", "LCI", "LOTCHEM", "LUCK", "MARI", "MCB", "MEBL", "MEHT",
    "MLCF", "MTL", "MUREB", "NATF", "NBP", "NESTLE", "NML", "NPL", "OGDC", "PABC",
    "PAEL", "PAKT", "PGLC", "PIBTL", "PIOC", "PKGS", "POL", "POWER", "PPL", "PSEL",
    "PSO", "PSX", "PTC", "RMPL", "SAZEW", "SCBPL", "SEARL", "SHFA", "SNGP", "SRVI",
    "SSGC", "SSOM", "SYS", "TGL", "THALL", "TPLRF1", "TRG", "UBL", "UPFL", "YOUW",
]

TV_SCAN_URL = "https://scanner.tradingview.com/pakistan/scan"
TV_COLS = ["name", "close", "volume", "change"]


# ── DB ───────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_volume (
            date TEXT NOT NULL, symbol TEXT NOT NULL, volume REAL NOT NULL,
            PRIMARY KEY (date, symbol)
        );
        CREATE TABLE IF NOT EXISTS intraday_reading (
            ts TEXT NOT NULL, time_key TEXT NOT NULL, symbol TEXT NOT NULL,
            price REAL NOT NULL, volume REAL NOT NULL, interval_volume REAL NOT NULL,
            PRIMARY KEY (ts, symbol)
        );
        CREATE TABLE IF NOT EXISTS spike_state (
            symbol TEXT PRIMARY KEY, price REAL, day_volume REAL,
            avg_volume_20d REAL, rvol REAL, is_candidate INTEGER DEFAULT 0,
            trend TEXT, signal TEXT, last_interval_vol REAL,
            avg_interval_vol REAL, updated_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()


# ── Market hours ─────────────────────────────────────────────────────────────
def is_market_open():
    now = datetime.now(PKT)
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


# ── Historical sync ──────────────────────────────────────────────────────────
def sync_historical_data(days=30):
    import yfinance as yf

    conn = get_db()
    synced, skipped = 0, 0
    for symbol in KSE100:
        try:
            hist = yf.Ticker(f"{symbol}.KA").history(period=f"{days}d")
            if hist.empty:
                skipped += 1
                continue
            for idx, row in hist.iterrows():
                date_str = idx.strftime("%Y-%m-%d")
                vol = float(row["Volume"] or 0)
                if vol <= 0:
                    continue
                conn.execute(
                    "INSERT INTO daily_volume (date, symbol, volume) VALUES (?,?,?) "
                    "ON CONFLICT(date, symbol) DO UPDATE SET volume=excluded.volume",
                    (date_str, symbol, vol),
                )
            synced += 1
        except Exception:
            skipped += 1
    conn.commit()
    conn.close()
    return synced, skipped


# ── Data fetch ───────────────────────────────────────────────────────────────
def fetch_live_data():
    payload = {
        "filter": [{"left": "exchange", "operation": "equal", "right": "PSX"}],
        "columns": TV_COLS,
        "range": [0, 1000],  # FIX: Increased range to ensure all >500 tickers are pulled
    }
    try:
        resp = requests.post(TV_SCAN_URL, json=payload, timeout=20)
        resp.raise_for_status()
        rows = resp.json().get("data", [])
    except Exception:
        return {}
    out = {}
    for row in rows:
        d = row.get("d", [])
        if len(d) < 4 or not d[0] or d[0] not in KSE100:
            continue
        try:
            out[d[0]] = {"price": float(d[1] or 0), "volume": float(d[2] or 0), "change_pct": float(d[3] or 0)}
        except (TypeError, ValueError):
            continue
    return out


# ── Averages ─────────────────────────────────────────────────────────────────
def get_avg_daily_volume(conn, symbol, before_date):
    rows = conn.execute(
        "SELECT volume FROM daily_volume WHERE symbol=? AND date<? ORDER BY date DESC LIMIT ?",
        (symbol, before_date, AVG_DAYS),
    ).fetchall()
    return sum(r["volume"] for r in rows) / len(rows) if rows else None


def get_avg_interval_volume(conn, symbol, time_key, before_ts):
    rows = conn.execute(
        "SELECT interval_volume FROM intraday_reading "
        "WHERE symbol=? AND time_key=? AND ts<? AND interval_volume>0 ORDER BY ts DESC LIMIT ?",
        (symbol, time_key, before_ts, AVG_DAYS),
    ).fetchall()
    return sum(r["interval_volume"] for r in rows) / len(rows) if rows else None


def get_trading_dates(conn, limit=3):
    rows = conn.execute("SELECT DISTINCT date FROM daily_volume ORDER BY date DESC LIMIT ?", (limit,)).fetchall()
    return [r["date"] for r in rows]


def get_top_spikes_for_date(conn, date):
    rows = conn.execute("SELECT symbol, volume FROM daily_volume WHERE date=?", (date,)).fetchall()
    results = []
    for r in rows:
        avg_vol_20d = get_avg_daily_volume(conn, r["symbol"], date)
        if not avg_vol_20d:
            continue
        rvol = r["volume"] / avg_vol_20d
        if rvol >= RVOL_SPIKE_THRESHOLD:
            results.append({"Symbol": r["symbol"], "RVOL": round(rvol, 2), "Day Volume": int(r["volume"]), "20d Avg Vol": int(avg_vol_20d)})
    results.sort(key=lambda x: x["RVOL"], reverse=True)
    return results[:TOP_N]


# ── Core scan cycle ──────────────────────────────────────────────────────────
def run_scan_cycle():
    conn = get_db()
    now = datetime.now(PKT)  # FIX: Enforced PKT to prevent timezone mismatch
    today = now.strftime("%Y-%m-%d")

    # FIX: Bucket times down to the 15 min mark so historical matching finds exact time_keys
    bucket_minute = (now.minute // 15) * 15
    bucket = now.replace(minute=bucket_minute, second=0, microsecond=0)
    
    bucket_ts = bucket.isoformat(timespec="seconds") 
    time_key = bucket.strftime("%H:%M")
    actual_ts = now.isoformat(timespec="seconds") # Used for exact 'updated_at' logging

    live = fetch_live_data()
    if not live:
        conn.close()
        return

    for symbol, d in live.items():
        price, day_volume = d["price"], d["volume"]

        conn.execute(
            "INSERT INTO daily_volume (date, symbol, volume) VALUES (?,?,?) "
            "ON CONFLICT(date, symbol) DO UPDATE SET volume=excluded.volume",
            (today, symbol, day_volume),
        )

        # Uses bucket_ts to ensure we fetch the interval ending *prior* to our current bucket
        prev = conn.execute(
            "SELECT volume FROM intraday_reading WHERE symbol=? AND ts<? AND ts LIKE ? ORDER BY ts DESC LIMIT 1",
            (symbol, bucket_ts, f"{today}%"),
        ).fetchone()
        
        if prev is not None:
            interval_volume, first_reading_of_day = max(0.0, day_volume - prev["volume"]), False
        else:
            interval_volume, first_reading_of_day = None, True

        # Insert using bucket_ts. If user spams manual scan, this will safely REPLACE 
        # the row in the current 15 min bucket instead of shrinking interval_volume to zero.
        conn.execute(
            "INSERT OR REPLACE INTO intraday_reading (ts, time_key, symbol, price, volume, interval_volume) VALUES (?,?,?,?,?,?)",
            (bucket_ts, time_key, symbol, price, day_volume, interval_volume if interval_volume is not None else 0.0),
        )

        avg_vol_20d = get_avg_daily_volume(conn, symbol, today)
        rvol = (day_volume / avg_vol_20d) if avg_vol_20d else None
        is_candidate = bool(rvol is not None and rvol >= RVOL_SPIKE_THRESHOLD)

        trend, signal = "Neutral", "Neutral"
        avg_interval_vol = get_avg_interval_volume(conn, symbol, time_key, bucket_ts)
        
        if is_candidate and not first_reading_of_day:
            prev_state = conn.execute("SELECT last_interval_vol, price FROM spike_state WHERE symbol=?", (symbol,)).fetchone()
            prev_interval_vol = prev_state["last_interval_vol"] if prev_state else None
            prev_price = prev_state["price"] if prev_state else None
            price_rising = prev_price is not None and price > prev_price

            if avg_interval_vol:
                trend = "Increasing" if interval_volume >= avg_interval_vol else "Decreasing"
            elif prev_interval_vol is not None:
                trend = "Increasing" if interval_volume > prev_interval_vol else (
                    "Decreasing" if interval_volume < prev_interval_vol else "Neutral"
                )

            if price_rising and trend == "Increasing":
                signal = "Strong Bullish"
            elif price_rising and trend == "Decreasing":
                signal = "Warning"

        conn.execute(
            "INSERT INTO spike_state (symbol, price, day_volume, avg_volume_20d, rvol, is_candidate, trend, signal, last_interval_vol, avg_interval_vol, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
            "price=excluded.price, day_volume=excluded.day_volume, avg_volume_20d=excluded.avg_volume_20d, "
            "rvol=excluded.rvol, is_candidate=excluded.is_candidate, trend=excluded.trend, signal=excluded.signal, "
            "last_interval_vol=excluded.last_interval_vol, avg_interval_vol=excluded.avg_interval_vol, updated_at=excluded.updated_at",
            (symbol, price, day_volume, avg_vol_20d, rvol, int(is_candidate), trend, signal, interval_volume, avg_interval_vol, actual_ts),
        )

    conn.commit()
    conn.close()


# ── UI ───────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="PSX Volume Spikes", layout="centered")
init_db()

if "last_scan" not in st.session_state:
    st.session_state.last_scan = 0
if is_market_open() and (datetime.now(PKT).timestamp() - st.session_state.last_scan) >= POLL_SECONDS:
    with st.spinner("Scanning..."):
        run_scan_cycle()
    st.session_state.last_scan = datetime.now(PKT).timestamp()

st.caption(f"📊 KSE-100 · RVOL ≥ {RVOL_SPIKE_THRESHOLD}x · Top {TOP_N} · "
           f"Market {'🟢 open' if is_market_open() else '🔴 closed'} (9:15–3:30 PM PKT)")

col1, col2 = st.columns(2)

# FIX: Replaced invalid `width="stretch"` with standard Streamlit kwarg `use_container_width=True`
if col1.button("🔍 Scan", use_container_width=True):
    with st.spinner("Scanning..."):
        run_scan_cycle()
    st.session_state.last_scan = datetime.now(PKT).timestamp()
    st.rerun()
if col2.button("⬇️ Sync", use_container_width=True):
    with st.spinner("Syncing history..."):
        synced, skipped = sync_historical_data()
    st.toast(f"Synced {synced} symbols" + (f", skipped {skipped}" if skipped else ""))
    st.rerun()

conn = get_db()
_today_str = datetime.now(PKT).strftime("%Y-%m-%d") # FIX: Enforced PKT

today_rows = conn.execute(
    "SELECT symbol AS Symbol, price AS Price, rvol AS RVOL, day_volume AS [Day Volume], "
    "avg_volume_20d AS [20d Avg Vol], trend AS Trend, signal AS Signal FROM spike_state "
    "WHERE is_candidate=1 AND rvol IS NOT NULL AND updated_at LIKE ? "
    "ORDER BY rvol DESC LIMIT ?",
    (f"{_today_str}%", TOP_N),
).fetchall()
today_rows = [dict(r) for r in today_rows]

all_dates = get_trading_dates(conn, limit=40)
past_dates = [d for d in all_dates if d != _today_str]
yesterday_date = past_dates[0] if len(past_dates) > 0 else None
two_days_ago_date = past_dates[1] if len(past_dates) > 1 else None

yesterday_rows = get_top_spikes_for_date(conn, yesterday_date) if yesterday_date else []
two_days_ago_rows = get_top_spikes_for_date(conn, two_days_ago_date) if two_days_ago_date else []

_history_days = len(all_dates)
conn.close()

if _history_days < AVG_DAYS:
    st.caption(f"ℹ️ Only {_history_days} day(s) of history — click Sync to backfill {AVG_DAYS} days for RVOL to work.")

_COL_CFG = {
    "RVOL": st.column_config.NumberColumn("RVOL", format="%.2fx", width="small"),
    "Price": st.column_config.NumberColumn("Price", format="%.2f", width="small"),
    "Day Volume": st.column_config.NumberColumn("Volume", format="compact", width="small"),
    "20d Avg Vol": st.column_config.NumberColumn("20d Avg", format="compact", width="small"),
    "Trend": st.column_config.TextColumn("Trend", width="small"),
    "Signal": st.column_config.TextColumn("Signal", width="small"),
    "Symbol": st.column_config.TextColumn("Symbol", width="small"),
}

def show(label, rows):
    st.caption(label)
    if not rows:
        st.caption("—")
        return
    df = rows if isinstance(rows, list) and isinstance(rows[0], dict) else [dict(r) for r in rows]
    st.dataframe(
        pd.DataFrame(df), hide_index=True, width="stretch",
        row_height=28, height=28 * len(df) + 38, column_config=_COL_CFG,
    )

show(f"Today ({_today_str})", today_rows)
show(f"Yesterday ({yesterday_date})" if yesterday_date else "Yesterday", yesterday_rows)
show(f"2 Days Ago ({two_days_ago_date})" if two_days_ago_date else "2 Days Ago", two_days_ago_rows)
