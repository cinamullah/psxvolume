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
def get_live_data(debug: bool = False):
    """Scrapes live market-watch data from PSX (SYMBOL ... CURRENT ... VOLUME)."""
    url = "https://dps.psx.com.pk/market-watch"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        st.error(f"Live Scrape Failed — request error: {e}")
        return {}

    if debug:
        st.write(f"HTTP status: {resp.status_code} | response length: {len(resp.text)} chars")

    if resp.status_code != 200:
        st.error(f"Live Scrape Failed — PSX returned HTTP {resp.status_code} (not 200).")
        if debug:
            st.code(resp.text[:1500])
        return {}

    lowered = resp.text.lower()
    if "enable javascript" in lowered or "captcha" in lowered or "cf-chl" in lowered:
        st.error(
            "Live Scrape Failed — the response looks like a JS-challenge/anti-bot page, "
            "not the real market data. `requests` can't execute JavaScript, so if PSX is "
            "serving this table client-side (or behind Cloudflare's bot check) this scraping "
            "approach won't work — we'd need a headless browser (Playwright/Selenium) or the "
            "underlying JSON API instead."
        )
        if debug:
            st.code(resp.text[:1500])
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("tbody.tbl__body tr")

    if not rows:
        # Selector didn't match — try to find ANY table as a fallback so we can
        # at least tell the user what structure the page actually has.
        any_tables = soup.find_all("table")
        st.error(
            f"Live Scrape Failed — selector 'tbody.tbl__body tr' matched 0 rows. "
            f"Found {len(any_tables)} <table> tag(s) on the page overall."
        )
        if debug:
            st.write("First 2000 chars of HTML body for inspection:")
            st.code(resp.text[:2000])
        return {}

    data = {}
    skipped = 0
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 11:
            skipped += 1
            continue
        try:
            sym = cells[0].text.strip()
            price = float(cells[7].text.strip().replace(",", "") or 0)
            vol = float(cells[10].text.strip().replace(",", "") or 0)
            if sym:
                data[sym] = {"volume": vol, "price": price}
        except (ValueError, IndexError):
            skipped += 1
            continue

    if debug:
        st.write(f"Parsed {len(data)} symbols, skipped {skipped} malformed rows out of {len(rows)} total.")

    if not data:
        st.error(
            "Live Scrape Failed — rows were found but none parsed into valid symbol/price/volume "
            "data. The column layout may not match cells[0]=symbol, cells[7]=price, cells[10]=volume "
            "anymore. Turn on debug mode and check the raw row HTML."
        )

    return data


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

debug_mode = st.checkbox("Debug mode (show scrape diagnostics)", value=False)

if st.button("🔄 Full Scan & Sync"):
    with st.spinner("Fetching Live Data..."):
        live = get_live_data(debug=debug_mode)
        if debug_mode:
            st.write(f"Live symbols returned: {len(live)}")
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
    if not debug_mode:
        st.rerun()

if st.button("⬇️ Sync Historical"):
    with st.spinner("Fetching historical data..."):
        sync_history()
    if not debug_mode:
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
