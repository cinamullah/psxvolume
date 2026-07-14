import os
import sqlite3
import time
import requests
import pandas as pd
import streamlit as st
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "volume_spikes.db")
POLL_SECONDS = 15 * 60
RVOL_SPIKE_THRESHOLD = 1.0  # Reset to 1.0 as requested
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

# ── DB ──────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_volume (date TEXT, symbol TEXT, volume REAL, PRIMARY KEY (date, symbol));
        CREATE TABLE IF NOT EXISTS intraday_reading (ts TEXT, time_key TEXT, symbol TEXT, price REAL, volume REAL, interval_volume REAL, PRIMARY KEY (ts, symbol));
        CREATE TABLE IF NOT EXISTS spike_state (symbol TEXT PRIMARY KEY, price REAL, day_volume REAL, avg_volume_20d REAL, rvol REAL, is_candidate INTEGER, trend TEXT, signal TEXT, last_interval_vol REAL, avg_interval_vol REAL, updated_at TEXT);
    """)
    conn.commit()
    conn.close()

# ── Historical Sync (Using your Scraper) ────────────────────────────────────
def sync_historical_data():
    conn = get_db()
    synced, skipped = 0, 0
    url = "https://dps.psx.com.pk/historical"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://dps.psx.com.pk/historical"}

    for symbol in KSE100:
        try:
            resp = requests.post(url, data={"symbol": symbol}, headers=headers, timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')
            table = soup.find('table', id='historicalTable')
            if not table: continue
            
            rows = table.find('tbody').find_all('tr')
            for row in rows:
                cols = row.find_all('td')
                if len(cols) < 6: continue
                # Date is usually index 0, Volume is usually index 5 (adjust if needed)
                date_str = pd.to_datetime(cols[0].text.strip()).strftime("%Y-%m-%d")
                vol_str = cols[5].get('data-value', cols[5].text.replace(',', ''))
                conn.execute("INSERT OR REPLACE INTO daily_volume (date, symbol, volume) VALUES (?,?,?)", 
                             (date_str, symbol, float(vol_str)))
            synced += 1
            time.sleep(0.5) # Avoid hitting rate limits
        except: skipped += 1
    conn.commit()
    conn.close()
    return synced, skipped

# ── Live Data (TradingView) ────────────────────────────────────────────────
def fetch_live_data():
    payload = {"filter": [{"left": "exchange", "operation": "equal", "right": "PSX"}], "columns": TV_COLS, "range": [0, 500]}
    try:
        data = requests.post(TV_SCAN_URL, json=payload, timeout=10).json().get("data", [])
        return {d['d'][0]: {"price": float(d['d'][1]), "volume": float(d['d'][2])} for d in data if d['d'][0] in KSE100}
    except: return {}

# ── UI Helpers ─────────────────────────────────────────────────────────────
def get_trading_dates(conn):
    # Fix: Fetch distinct dates directly from DB. 
    # This identifies "Yesterday" as the most recent date excluding today.
    rows = conn.execute("SELECT DISTINCT date FROM daily_volume ORDER BY date DESC LIMIT 5").fetchall()
    return [r["date"] for r in rows]

def get_top_spikes_for_date(conn, date):
    rows = conn.execute("""
        SELECT symbol, volume, 
        (SELECT AVG(volume) FROM daily_volume WHERE symbol=d.symbol AND date < ? LIMIT 20) as avg_vol 
        FROM daily_volume d WHERE date=?
    """, (date, date)).fetchall()
    
    results = []
    for r in rows:
        if r["avg_vol"] and r["avg_vol"] > 0:
            rvol = r["volume"] / r["avg_vol"]
            if rvol >= RVOL_SPIKE_THRESHOLD:
                results.append({"Symbol": r["symbol"], "RVOL": round(rvol, 2), "Day Volume": int(r["volume"]), "20d Avg": int(r["avg_vol"])})
    return sorted(results, key=lambda x: x["RVOL"], reverse=True)[:TOP_N]

# ── Main App ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="PSX Volume Spikes", layout="centered")
init_db()

col1, col2 = st.columns(2)
if col1.button("🔍 Scan"): st.rerun() # Simple trigger for now
if col2.button("⬇️ Sync History"):
    with st.spinner("Scraping PSX..."):
        s, sk = sync_historical_data()
        st.toast(f"Synced {s}, Skipped {sk}")
        st.rerun()

conn = get_db()
dates = get_trading_dates(conn)
today_str = datetime.now(PKT).strftime("%Y-%m-%d")

# Logic to identify yesterday
past_dates = [d for d in dates if d != today_str]
yesterday = past_dates[0] if len(past_dates) > 0 else None
two_days_ago = past_dates[1] if len(past_dates) > 1 else None

st.subheader(f"Yesterday: {yesterday}")
if yesterday:
    st.dataframe(pd.DataFrame(get_top_spikes_for_date(conn, yesterday)))

st.subheader(f"2 Days Ago: {two_days_ago}")
if two_days_ago:
    st.dataframe(pd.DataFrame(get_top_spikes_for_date(conn, two_days_ago)))

conn.close()
