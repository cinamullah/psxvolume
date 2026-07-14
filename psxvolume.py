import streamlit as st
import yfinance as yf
import sqlite3
import pandas as pd
from datetime import datetime

# ── Configuration ────────────────────────────────────────────────────────
DB_NAME = "volume_scanner.db"
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
    """Fetch historical data, calculate metrics, and save to SQLite."""
    # Fetch 1 month to ensure 20-day SMA calculation is accurate
    raw_data = yf.download(WATCHLIST, period="1mo", group_by='ticker', threads=True)
    
    with sqlite3.connect(DB_NAME) as conn:
        for symbol in WATCHLIST:
            df = raw_data[symbol].copy()
            if df.empty: continue
            
            # Calculations
            sma_vol = df['Volume'].rolling(window=20).mean()
            rvol = df['Volume'] / sma_vol
            pct_change = df['Close'].pct_change() * 100
            
            clean_symbol = symbol.replace('.KA', '')
            
            # Iterate through the last 5 days to ensure we capture history
            for date, val in rvol.tail(5).items():
                if val >= 1.5:
                    conn.execute("""
                        INSERT OR REPLACE INTO spikes (date, symbol, rvol, price_change)
                        VALUES (?, ?, ?, ?)
                    """, (date.strftime('%Y-%m-%d'), clean_symbol, round(val, 2), round(pct_change.loc[date], 2)))
        conn.commit()

# ── Dashboard UI ─────────────────────────────────────────────────────────
st.set_page_config(layout="wide", page_title="Volume Spike History")
init_db()

st.title("📈 Historical Volume Spike Dashboard (RVOL ≥ 1.5)")

if st.button("🔄 Sync Historical Data"):
    with st.spinner("Processing..."):
        sync_data()
        st.success("Historical data synced!")
        st.rerun()

# ── Query & Display ──────────────────────────────────────────────────────
with sqlite3.connect(DB_NAME) as conn:
    # Fetch the 3 most recent trading dates stored in the database
    dates = [row[0] for row in conn.execute("SELECT DISTINCT date FROM spikes ORDER BY date DESC LIMIT 3").fetchall()]

cols = st.columns(3)
titles = ["Today (Latest)", "Yesterday", "Two Days Ago"]

for i, col in enumerate(cols):
    with col:
        if i < len(dates):
            target_date = dates[i]
            st.subheader(f"{titles[i]}")
            st.caption(f"Date: {target_date}")
            
            # Query for this specific date
            df = pd.read_sql(
                "SELECT symbol, rvol, price_change FROM spikes WHERE date = ? ORDER BY rvol DESC",
                conn, params=(target_date,)
            )
            
            if not df.empty:
                df.columns = ["Symbol", "Relative Volume", "Price Change %"]
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.info("No spikes for this day.")
        else:
            st.subheader(titles[i])
            st.info("No data available.")
