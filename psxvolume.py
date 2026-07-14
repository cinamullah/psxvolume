"""
PSX Volume Spike Detector — single-file Streamlit app.

Every time the page loads (or is refreshed), if 15+ minutes have passed
since the last scan, it fetches live price/volume for the KSE-100 symbols
below, updates the SQLite DB, and recomputes RVOL / spike status.

  - RVOL = today's volume / 20-day average daily volume. Candidate if >= 2x.
  - 15-min trend for candidates: Increasing/Decreasing vs that symbol's own
    average for the same time-of-day, plus price direction -> Strong
    Bullish (price + volume both up) or Warning (price up, volume fading).
  - Three sections: Today, Yesterday, Two Days Ago — each Top 10 by RVOL.
    Today's numbers become "Yesterday" automatically once a new trading
    day starts.

Run:
    pip install streamlit requests pandas
    streamlit run app.py
"""

import os
import sqlite3
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "volume_spikes.db")
POLL_SECONDS = 15 * 60
RVOL_SPIKE_THRESHOLD = 2.0
AVG_DAYS = 20
TOP_N = 10

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


# ── Data fetch ───────────────────────────────────────────────────────────────
def fetch_live_data():
    payload = {
        "filter": [{"left": "exchange", "operation": "equal", "right": "PSX"}],
        "columns": TV_COLS,
        "range": [0, 500],
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
    now = datetime.now()
    today, ts, time_key = now.strftime("%Y-%m-%d"), now.isoformat(timespec="seconds"), now.strftime("%H:%M")

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

        prev = conn.execute(
            "SELECT volume FROM intraday_reading WHERE symbol=? AND ts<? AND ts LIKE ? ORDER BY ts DESC LIMIT 1",
            (symbol, ts, f"{today}%"),
        ).fetchone()
        if prev is not None:
            interval_volume, first_reading_of_day = max(0.0, day_volume - prev["volume"]), False
        else:
            # First poll of the day: day_volume is session-to-date, not a
            # real 15-min slice, so don't treat it as a trend signal.
            interval_volume, first_reading_of_day = None, True

        conn.execute(
            "INSERT OR REPLACE INTO intraday_reading (ts, time_key, symbol, price, volume, interval_volume) VALUES (?,?,?,?,?,?)",
            (ts, time_key, symbol, price, day_volume, interval_volume if interval_volume is not None else 0.0),
        )

        avg_vol_20d = get_avg_daily_volume(conn, symbol, today)
        rvol = (day_volume / avg_vol_20d) if avg_vol_20d else None
        is_candidate = bool(rvol is not None and rvol >= RVOL_SPIKE_THRESHOLD)

        trend, signal = "Neutral", "Neutral"
        avg_interval_vol = get_avg_interval_volume(conn, symbol, time_key, ts)
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
            (symbol, price, day_volume, avg_vol_20d, rvol, int(is_candidate), trend, signal, interval_volume, avg_interval_vol, ts),
        )

    conn.commit()
    conn.close()


# ── UI ───────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="PSX Volume Spikes", layout="wide")
init_db()

if "last_scan" not in st.session_state:
    st.session_state.last_scan = 0
if (datetime.now().timestamp() - st.session_state.last_scan) >= POLL_SECONDS:
    with st.spinner("Scanning..."):
        run_scan_cycle()
    st.session_state.last_scan = datetime.now().timestamp()

st.title("📊 Volume Spikes")
st.caption(f"KSE-100 · RVOL ≥ {RVOL_SPIKE_THRESHOLD}x · Top {TOP_N} by RVOL")

conn = get_db()
today_rows = conn.execute(
    "SELECT symbol AS Symbol, price AS Price, rvol AS RVOL, day_volume AS [Day Volume], "
    "avg_volume_20d AS [20d Avg Vol], trend AS Trend, signal AS Signal FROM spike_state "
    "WHERE is_candidate=1 AND rvol IS NOT NULL ORDER BY rvol DESC LIMIT ?",
    (TOP_N,),
).fetchall()
dates = get_trading_dates(conn, limit=3)
yesterday_rows = get_top_spikes_for_date(conn, dates[1]) if len(dates) > 1 else []
two_days_ago_rows = get_top_spikes_for_date(conn, dates[2]) if len(dates) > 2 else []
conn.close()

st.subheader("Today")
if today_rows:
    st.dataframe(pd.DataFrame([dict(r) for r in today_rows]), hide_index=True, use_container_width=True)
else:
    st.write("No spikes yet today.")

st.subheader(f"Yesterday{' (' + dates[1] + ')' if len(dates) > 1 else ''}")
st.dataframe(pd.DataFrame(yesterday_rows), hide_index=True, use_container_width=True) if yesterday_rows else st.write("No data yet.")

st.subheader(f"Two Days Ago{' (' + dates[2] + ')' if len(dates) > 2 else ''}")
st.dataframe(pd.DataFrame(two_days_ago_rows), hide_index=True, use_container_width=True) if two_days_ago_rows else st.write("No data yet.")
