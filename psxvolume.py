import os
import sqlite3
import requests
import pandas as pd
import streamlit as st
from datetime import datetime
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

# ── Setup ──────────────────────────────────────────────────────────────────
DB_PATH = "volume_spikes.db"
PKT = ZoneInfo("Asia/Karachi")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_volume (date TEXT, symbol TEXT, volume REAL, PRIMARY KEY (date, symbol));
        CREATE TABLE IF NOT EXISTS spike_state (symbol TEXT PRIMARY KEY, price REAL, volume REAL, updated_at TEXT);
    """)
    conn.commit()
    conn.close()

# ── Scrapers ───────────────────────────────────────────────────────────────
def get_live_data():
    """Scrapes live data from PSX website."""
    try:
        url = "https://dps.psx.com.pk/market-watch"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        rows = soup.select('tbody.tbl__body tr')
        data = {}
        for row in rows:
            cells = row.find_all('td')
            if len(cells) >= 11:
                sym = cells[0].text.strip()
                vol = float(cells[10].text.replace(',', ''))
                price = float(cells[7].text.replace(',', ''))
                data[sym] = {"volume": vol, "price": price}
        return data
    except Exception as e:
        st.error(f"Live Scrape Failed: {e}")
        return {}

def sync_history():
    """Scrapes historical data from PSX."""
    conn = get_db()
    try:
        url = "https://dps.psx.com.pk/historical"
        # Using a few example symbols to test
        for sym in ["KSE100", "LUCK", "ENGRO"]: 
            resp = requests.post(url, data={"symbol": sym}, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            table = soup.find('table', id='historicalTable')
            if not table: continue
            for row in table.find('tbody').find_all('tr'):
                cols = row.find_all('td')
                if len(cols) > 5:
                    date_str = pd.to_datetime(cols[0].text.strip()).strftime("%Y-%m-%d")
                    vol = float(cols[5].text.replace(',', ''))
                    conn.execute("INSERT OR REPLACE INTO daily_volume (date, symbol, volume) VALUES (?,?,?)", 
                                 (date_str, sym, vol))
        conn.commit()
    except Exception as e:
        st.error(f"Sync Failed: {e}")
    finally:
        conn.close()

# ── App Logic ──────────────────────────────────────────────────────────────
st.set_page_config(layout="centered")
init_db()

if st.button("🔄 Full Scan & Sync"):
    with st.spinner("Fetching Live Data..."):
        live = get_live_data()
        conn = get_db()
        today = datetime.now(PKT).strftime("%Y-%m-%d")
        for sym, d in live.items():
            conn.execute("INSERT OR REPLACE INTO spike_state VALUES (?,?,?,?)", (sym, d['price'], d['volume'], datetime.now().isoformat()))
            conn.execute("INSERT OR REPLACE INTO daily_volume VALUES (?,?,?)", (today, sym, d['volume']))
        conn.commit()
        conn.close()
    st.rerun()

if st.button("⬇️ Sync Historical"):
    sync_history()
    st.rerun()

# ── Display ────────────────────────────────────────────────────────────────
conn = get_db()
dates = [r['date'] for r in conn.execute("SELECT DISTINCT date FROM daily_volume ORDER BY date DESC LIMIT 3").fetchall()]
conn.close()

for i, d in enumerate(dates):
    st.subheader(f"Date: {d}")
    df = pd.read_sql(f"SELECT * FROM daily_volume WHERE date='{d}'", get_db())
    st.dataframe(df)
