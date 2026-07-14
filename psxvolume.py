import time
import sqlite3
import requests
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo
from contextlib import contextmanager

# ── Setup ──────────────────────────────────────────────────────────────────
DB_PATH = "volume_spikes.db"
PKT = ZoneInfo("Asia/Karachi")
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Symbols to track for historical sync. Replace with your full watchlist
# (e.g. load from a CSV / the KSE-100 constituent list) instead of hardcoding.
WATCHLIST = ["KSE100", "LUCK", "ENGRO"]


@contextmanager
def get_db():
    """Context-managed connection so every caller closes it automatically."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS daily_volume (
                date TEXT, symbol TEXT, volume REAL,
                PRIMARY KEY (date, symbol)
            );
            CREATE TABLE IF NOT EXISTS spike_state (
                symbol TEXT PRIMARY KEY, price REAL, volume REAL, updated_at TEXT
            );
        """)
        conn.commit()


# ── Scrapers ───────────────────────────────────────────────────────────────
def get_live_data():
    """Scrapes live market-watch data from PSX (SYMBOL ... CURRENT ... VOLUME)."""
    try:
        url = "https://dps.psx.com.pk/market-watch"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("tbody.tbl__body tr")
        data = {}
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 11:
                continue
            try:
                sym = cells[0].text.strip()
                price = float(cells[7].text.strip().replace(",", "") or 0)
                vol = float(cells[10].text.strip().replace(",", "") or 0)
                if sym:
                    data[sym] = {"volume": vol, "price": price}
            except (ValueError, IndexError):
                # Skip malformed rows instead of aborting the whole scrape
                continue
        return data
    except requests.RequestException as e:
        st.error(f"Live Scrape Failed: {e}")
        return {}


def _fetch_one_day(symbol: str, date_str: str):
    """
    PSX's /historical endpoint returns data for ONE symbol on ONE date per
    request (POST {symbol, date}) — it does NOT return a date-range table
    for just a symbol. This fetches a single day's row for a single symbol.
    Returns None if there's no trading data for that date (e.g. holiday/weekend).
    """
    url = "https://dps.psx.com.pk/historical"
    resp = requests.post(
        url, data={"symbol": symbol, "date": date_str}, headers=HEADERS, timeout=15
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table or not table.find("tbody"):
        return None
    row = table.find("tbody").find("tr")
    if not row:
        return None
    cols = [c.text.strip() for c in row.find_all("td")]
    if len(cols) < 6:
        return None
    try:
        # Typical column order: DATE, OPEN, HIGH, LOW, CLOSE, VOLUME
        volume = float(cols[5].replace(",", ""))
        return volume
    except (ValueError, IndexError):
        return None


def sync_history(symbols=None, days_back: int = 5):
    """
    Backfills daily_volume for the given symbols over the last `days_back`
    calendar days. One symbol/day failing does not abort the whole sync.
    """
    symbols = symbols or WATCHLIST
    today = datetime.now(PKT).date()
    results, errors = 0, []

    with get_db() as conn:
        for sym in symbols:
            for i in range(days_back):
                day = today - timedelta(days=i)
                if day.weekday() >= 5:  # skip Sat/Sun — PSX doesn't trade
                    continue
                date_str = day.strftime("%Y-%m-%d")
                try:
                    volume = _fetch_one_day(sym, date_str)
                    if volume is not None:
                        conn.execute(
                            "INSERT OR REPLACE INTO daily_volume (date, symbol, volume) "
                            "VALUES (?, ?, ?)",
                            (date_str, sym, volume),
                        )
                        results += 1
                    time.sleep(0.3)  # be polite to the endpoint
                except requests.RequestException as e:
                    errors.append(f"{sym} {date_str}: {e}")
                    continue
        conn.commit()

    if errors:
        st.warning(f"Synced {results} rows, {len(errors)} requests failed.")
    else:
        st.success(f"Synced {results} rows.")


# ── App Logic ──────────────────────────────────────────────────────────────
st.set_page_config(layout="centered")
init_db()

if st.button("🔄 Full Scan & Sync"):
    with st.spinner("Fetching Live Data..."):
        live = get_live_data()
        today = datetime.now(PKT).strftime("%Y-%m-%d")
        now_iso = datetime.now(PKT).isoformat()
        with get_db() as conn:
            for sym, d in live.items():
                conn.execute(
                    "INSERT OR REPLACE INTO spike_state (symbol, price, volume, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (sym, d["price"], d["volume"], now_iso),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO daily_volume (date, symbol, volume) VALUES (?, ?, ?)",
                    (today, sym, d["volume"]),
                )
            conn.commit()
    st.rerun()

if st.button("⬇️ Sync Historical"):
    with st.spinner("Fetching historical data..."):
        sync_history()
    st.rerun()

# ── Display ────────────────────────────────────────────────────────────────
with get_db() as conn:
    dates = [
        r["date"]
        for r in conn.execute(
            "SELECT DISTINCT date FROM daily_volume ORDER BY date DESC LIMIT 3"
        ).fetchall()
    ]
    for d in dates:
        st.subheader(f"Date: {d}")
        df = pd.read_sql(
            "SELECT * FROM daily_volume WHERE date = ?", conn, params=(d,)
        )
        st.dataframe(df)
