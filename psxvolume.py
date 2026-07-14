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
        # Dedicated table to lock down the true past 3 trading days
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trading_dates (
                idx INTEGER PRIMARY KEY,
                date TEXT UNIQUE
            )
        """)

def sync_data():
    """Fetch historical data, calculate metrics, and save to SQLite."""
    raw_data = yf.download(WATCHLIST, period="1mo", group_by='ticker', threads=True)
    
    # Extract actual calendar trading dates from the market data
    sample_ticker = WATCHLIST[0]
    if sample_ticker in raw_data and not raw_data[sample_ticker].empty:
        trading_days = raw_data[sample_ticker].index.strftime('%Y-%m-%d').tolist()
        last_5_days = trading_days[-5:]
        
        with sqlite3.connect(DB_NAME) as conn:
            # Store them sorted by recency (0 = Today, 1 = Yesterday, 2 = 2 Days ago)
            for idx, d_str in enumerate(reversed(last_5_days)):
                conn.execute("INSERT OR REPLACE INTO trading_dates (idx, date) VALUES (?, ?)", (idx, d_str))
            conn.commit()

    with sqlite3.connect(DB_NAME) as conn:
        for symbol in WATCHLIST:
            df = raw_data[symbol].copy()
            if df.empty: continue
            
            # Calculations
            sma_vol = df['Volume'].rolling(window=20).mean()
            rvol = df['Volume'] / sma_vol
            pct_change = df['Close'].pct_change() * 100
            
            clean_symbol = symbol.replace('.KA', '')
            
            # Look through all trailing 5 days to capture history completely
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
    with st.spinner("Processing historical data..."):
        sync_data()
        st.success("Historical data synced successfully!")
        st.rerun()

# ── Query & Display ──────────────────────────────────────────────────────
with sqlite3.connect(DB_NAME) as conn:
    # Always pull the explicit historical calendar sequence
    dates = [row[0] for row in conn.execute("SELECT date FROM trading_dates ORDER BY idx ASC LIMIT 3").fetchall()]

cols = st.columns(3)
titles = ["Today (Latest)", "Yesterday", "Two Days Ago"]

for i, col in enumerate(cols):
    with col:
        st.subheader(titles[i])
        if i < len(dates):
            target_date = dates[i]
            st.caption(f"Date: {target_date}")
            
            with sqlite3.connect(DB_NAME) as conn:
                df = pd.read_sql(
                    "SELECT symbol, rvol, price_change FROM spikes WHERE date = ? ORDER BY rvol DESC",
                    conn, params=(target_date,)
                )
            
            if not df.empty:
                df.columns = ["Symbol", "Relative Volume", "Price Change %"]
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.info("No spikes crossed RVOL ≥ 1.5 on this day.")
        else:
            st.caption("Date: Unknown")
            st.info("Click 'Sync Historical Data' above to initialize tables.")
