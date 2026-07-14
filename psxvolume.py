import time
import sqlite3
import requests
import pandas as pd
import streamlit as st
from datetime import datetime
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo
from contextlib import contextmanager

# ── Setup ──────────────────────────────────────────────────────────────────
DB_PATH = "volume_spikes.db"
PKT = ZoneInfo("Asia/Karachi")
LIVE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://dps.psx.com.pk/",
}
HISTORICAL_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://dps.psx.com.pk/historical",
}

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
        resp = requests.get(url, headers=LIVE_HEADERS, timeout=15)
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


def _fetch_symbol_history(symbol: str):
    """
    Single POST with just {'symbol': symbol} returns the FULL historical
    table for that symbol (id='historicalTable'). No date parameter needed.
    Returns a list of (date_str, volume) tuples.
    """
    url = "https://dps.psx.com.pk/historical"
    resp = requests.post(url, data={"symbol": symbol}, headers=HISTORICAL_HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="historicalTable")
    if not table or not table.find("tbody"):
        return []

    # Map header labels to column indices instead of hardcoding positions,
    # so this survives PSX reordering columns.
    thead = table.find("thead")
    headers_row = [th.text.strip().upper() for th in thead.find_all("th")] if thead else []

    def find_col(*keywords):
        for i, h in enumerate(headers_row):
            if any(k in h for k in keywords):
                return i
        return None

    date_idx = find_col("DATE", "TIME")
    vol_idx = find_col("VOLUME")

    out = []
    for row in table.find("tbody").find_all("tr"):
        cols = row.find_all("td")
        if not cols:
            continue
        # Prefer data-value attribute (raw, unformatted) over display text
        values = [c.get("data-value", c.text.strip()) for c in cols]

        try:
            raw_date = values[date_idx] if date_idx is not None else values[0]
            raw_vol = values[vol_idx] if vol_idx is not None else values[-1]
            date_str = pd.to_datetime(raw_date).strftime("%Y-%m-%d")
            volume = float(str(raw_vol).replace(",", ""))
            out.append((date_str, volume))
        except (ValueError, IndexError, TypeError):
            continue

    return out


def sync_history(symbols=None):
    """Backfills daily_volume for the given symbols from their full history."""
    symbols = symbols or WATCHLIST
    results, errors = 0, []

    with get_db() as conn:
        for sym in symbols:
            try:
                rows = _fetch_symbol_history(sym)
                for date_str, volume in rows:
                    conn.execute(
                        "INSERT OR REPLACE INTO daily_volume (date, symbol, volume) "
                        "VALUES (?, ?, ?)",
                        (date_str, sym, volume),
                    )
                    results += 1
                time.sleep(0.3)  # be polite between symbols
            except requests.RequestException as e:
                errors.append(f"{sym}: {e}")
                continue
        conn.commit()

    if errors:
        st.warning(f"Synced {results} rows, {len(errors)} symbols failed: {'; '.join(errors)}")
    else:
        st.success(f"Synced {results} rows across {len(symbols)} symbols.")


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
