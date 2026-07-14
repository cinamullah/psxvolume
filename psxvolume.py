import streamlit as st
import yfinance as yf
import sqlite3
import pandas as pd
from datetime import datetime

# ── Configuration ────────────────────────────────────────────────────────
DB_NAME = "volume_scanner.db"
# Note: yfinance requires .KA suffix for Karachi Stock Exchange symbols
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
            CREATE TABLE IF NOT EXISTS spikes (
                date TEXT, 
                symbol TEXT, 
                rvol REAL, 
                price_change REAL,
                UNIQUE(date, symbol)
            )
        """)

def sync_data():
    """Fetch data, calculate metrics, and save to SQLite."""
    # Download 1 month to ensure 20-day SMA is accurate
    raw_data = yf.download(WATCHLIST, period="1mo", group_by='ticker', threads=True)
    
    with sqlite3.connect(DB_NAME) as conn:
        for symbol in WATCHLIST:
            df = raw_data[symbol].copy()
            if df.empty: continue
            
            # Calculations
            sma_vol = df['Volume'].rolling(window=20).mean()
            rvol = df['Volume'] / sma_vol
            pct_change = df['Close'].pct_change() * 100
            
            # Prepare data
            clean_symbol = symbol.replace('.KA', '')
            
            # Loop through last 5 days to find any spikes
            for date, val in rvol.tail(5).items():
                if val >= 1.5:
                    conn.execute("""
                        INSERT OR REPLACE INTO spikes (date, symbol, rvol, price_change)
                        VALUES (?, ?, ?, ?)
                    """, (date.strftime('%Y-%m-%d'), clean_symbol, round(val, 2), round(pct_change.loc[date], 2)))
        conn.commit()

# ── Dashboard UI ─────────────────────────────────────────────────────────
st.set_page_config(layout="wide", page_title="Volume Spike Dashboard")
init_db()

st.title("📈 Volume Spike Dashboard (RVOL ≥ 1.5)")

if st.button("🔄 Sync Market Data"):
    with st.spinner("Calculating Spikes..."):
        sync_data()
        st.success("Data synced!")
        st.rerun()

# Get the last 3 dates available in the database
with sqlite3.connect(DB_NAME) as conn:
    dates = [row[0] for row in conn.execute("SELECT DISTINCT date FROM spikes ORDER BY date DESC LIMIT 3").fetchall()]

# Create 3 columns
cols = st.columns(3)

# Mapping indexes for readability
titles = ["Today", "Yesterday", "Two Days Ago"]

for i, col in enumerate(cols):
    if i < len(dates):
        current_date = dates[i]
        with col:
            st.subheader(f"{titles[i]} ({current_date})")
            
            # Query for this specific date
            df = pd.read_sql(
                "SELECT symbol, rvol, price_change FROM spikes WHERE date = ? ORDER BY rvol DESC",
                conn, params=(current_date,)
            )
            
            if not df.empty:
                df.columns = ["Symbol", "Relative Volume", "Price Change %"]
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.write("No spikes found.")
