#!/usr/bin/env python3
"""
Small-cap momentum screener (Ross Cameron / Warrior Trading style).

Finds US stocks that are:
  * priced between MIN_PRICE and MAX_PRICE
  * up at least MIN_PCT_CHANGE % vs. the previous close
  * trading at least MIN_REL_VOLUME x their normal volume for this time of day
  * floating fewer than MAX_FLOAT shares
  * carrying a news headline from the last NEWS_MAX_AGE_HOURS hours

Data sources (no paid keys):
  * Universe: Nasdaq Trader symbol files.
  * Live price/volume: Schwab Market Data API (free with a Schwab account,
    real-time, includes premarket volume) when set up, otherwise Yahoo.
  * Average volume, float, news: Yahoo Finance via yfinance.

Usage (also runs as the "Momentum" tab in app.py):
    python momentum.py                 # one scan
    python momentum.py --loop          # rescan every LOOP_SECONDS
    python momentum.py --tickers GME,AMC,PLUG   # scan only these (quick test)
    python momentum.py --refresh       # re-download today's ticker list
    python momentum.py --source yahoo  # force Yahoo even if Schwab is set up
    python momentum.py --schwab-login  # Schwab login (first time, then every 7 days)
    python momentum.py --schwab-test AAPL   # show Schwab's raw quote (debugging)

DATA DELAY - read this:
  * Each scan prints how old the newest price is and how long the scan took.
  * Schwab: real-time quotes, including premarket trades and volume.
  * Yahoo (fallback): batch quotes, ~30 requests for the whole market. Measured
    within a minute or two of live, though Yahoo labels Nasdaq/NYSE data as
    15-20 min delayed, so no guarantee. Yahoo has no premarket volume, so
    relative volume can't be measured before 9:30 ET in Yahoo mode (see
    PASS_UNKNOWN_RVOL_PREMARKET).
  * Float comes from Yahoo's company profile, which updates slowly and can be
    weeks out of date after offerings or reverse splits. Double-check it.
  * News comes from Yahoo search results and can lag the wire by several minutes.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import pickle
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import warnings
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402
import requests  # noqa: E402
import yfinance as yf  # noqa: E402

# ================================ CONFIG ================================
# --- The five criteria ---
MIN_PRICE = 2.00
MAX_PRICE = 20.00
MIN_PCT_CHANGE = 10.0          # % up vs. previous close
MIN_REL_VOLUME = 5.0           # today's volume vs. average, adjusted for time of day
MAX_FLOAT = 20_000_000         # shares
NEWS_MAX_AGE_HOURS = 24
REQUIRE_NEWS = True            # False = still show stocks with no fresh headline

# --- How the criteria are measured ---
AVG_VOLUME_DAYS = 30           # trading days in the average-volume baseline
# If Yahoo has no float, use shares outstanding instead. Float can never exceed
# shares outstanding, so if that passes, the real float passes too. Marked "*".
USE_SHARES_OUTSTANDING_FALLBACK = True
# Yahoo gives 0 volume for premarket bars, so RVOL is unknown before 9:30 ET.
# True = let stocks through the RVOL filter premarket (shown as "n/a").
# False = drop them, meaning premarket scans will find nothing.
PASS_UNKNOWN_RVOL_PREMARKET = True

# Cumulative share of a typical day's volume that has traded by each ET time.
# Used to scale the average so 10:00 AM volume is compared with what is
# normal at 10:00 AM, not with a full day.
VOLUME_CURVE = [
    ("04:00", 0.00), ("08:00", 0.01), ("09:30", 0.03), ("10:00", 0.15),
    ("10:30", 0.22), ("11:00", 0.28), ("12:00", 0.38), ("13:00", 0.47),
    ("14:00", 0.56), ("15:00", 0.68), ("15:30", 0.78), ("16:00", 1.00),
]
MIN_VOLUME_FRACTION = 0.02     # floor so the first minutes don't explode RVOL

# --- Universe ---
# Security names matching this are skipped (warrants, rights, units, preferreds).
EXCLUDE_NAME_PATTERN = r"\b(warrants?|rights?|units?|preferred)\b"
NASDAQ_FILES = {
    "nasdaqlisted": [
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "ftp://ftp.nasdaqtrader.com/SymbolDirectory/nasdaqlisted.txt",
    ],
    "otherlisted": [
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
        "ftp://ftp.nasdaqtrader.com/SymbolDirectory/otherlisted.txt",
    ],
}

# --- Data source ---
# "auto"   = Schwab if you've set up .env and run --schwab-login, otherwise Yahoo
# "schwab" = Schwab (falls back to Yahoo for a scan if Schwab fails)
# "yahoo"  = Yahoo only
DATA_SOURCE = "auto"
SCHWAB_QUOTE_BATCH = 300       # symbols per Schwab quotes request

# --- Speed / rate limits ---
LOOP_SECONDS = 60              # pause between scans in --loop mode
YAHOO_QUOTE_BATCH = 200        # symbols per Yahoo quote request
BATCH_SIZE = 150               # tickers per yfinance download call (average volume)
BATCH_PAUSE_SECONDS = 0.5      # pause between batches
DOWNLOAD_THREADS = 8
MAX_RETRIES = 3                # retries after a rate limit
RETRY_BACKOFF_SECONDS = 10     # doubles on each retry
NEWS_CACHE_MINUTES = 5         # in --loop mode, reuse news this long

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / ".cache"
SCHWAB_ENV_FILE = HERE / ".env"                 # SCHWAB_APP_KEY / SCHWAB_APP_SECRET
SCHWAB_TOKEN_FILE = HERE / "schwab_token.json"  # written by --schwab-login
# ========================================================================

ET = ZoneInfo("America/New_York")
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
SCHWAB_API = "https://api.schwabapi.com"
SCHWAB_AUTHORIZE_URL = SCHWAB_API + "/v1/oauth/authorize"
SCHWAB_TOKEN_URL = SCHWAB_API + "/v1/oauth/token"
SCHWAB_DEFAULT_CALLBACK = "https://127.0.0.1:8182"
SCHWAB_REFRESH_TOKEN_DAYS = 7
MARKET_OPEN_MIN = 9 * 60 + 30



class _YfErrorLog(logging.Handler):
    """Collects yfinance's error messages (e.g. per-ticker download failures)
    instead of printing them. Works across yfinance versions; newer ones no
    longer expose download errors any other way."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: List[Tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append((record.thread, record.getMessage()))
        except Exception:
            pass

    def take(self) -> List[str]:
        """Pop and return the messages logged from the calling thread."""
        me = threading.get_ident()
        mine = [m for t, m in self.messages if t == me]
        self.messages = [(t, m) for t, m in self.messages if t != me]
        return mine


_yf_errors = _YfErrorLog()
_yf_logger = logging.getLogger("yfinance")
_yf_logger.handlers = [_yf_errors]
_yf_logger.setLevel(logging.ERROR)
_yf_logger.propagate = False
CACHE_DIR.mkdir(exist_ok=True)
try:
    yf.set_tz_cache_location(str(CACHE_DIR / "yfinance"))
except Exception:
    pass


# Optional callback (set by the Streamlit page) that receives progress messages.
status_hook: Optional[Callable[[str], None]] = None


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    if status_hook:
        status_hook(msg.strip())


def progress(msg: str) -> None:
    """Single-line progress: overwrites itself in a terminal."""
    if status_hook:
        status_hook(msg.strip())
    else:
        print(f"\r{msg}", end="", file=sys.stderr, flush=True)


def now_et() -> datetime:
    return datetime.now(ET)


def looks_rate_limited(text: str) -> bool:
    text = text.lower()
    return any(s in text for s in ("rate limit", "too many requests", "429"))


def with_retries(fn: Callable, what: str):
    """Call fn(); back off and retry on rate limits; return None on any other failure."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as e:  # yfinance raises many different things
            if looks_rate_limited(repr(e)) and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * 2 ** attempt
                log(f"  rate limited on {what}; waiting {wait}s")
                time.sleep(wait)
                continue
            return None
    return None


# ------------------------------- universe -------------------------------

def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_symbol_file(text: str, symbol_col: str) -> List[str]:
    df = pd.read_csv(io.StringIO(text), sep="|", dtype=str).fillna("")
    df = df[~df[symbol_col].str.startswith("File Creation Time")]
    df = df[(df["Test Issue"] != "Y") & (df["ETF"] != "Y")]
    if "NextShares" in df.columns:
        df = df[df["NextShares"] != "Y"]
    excluded = df["Security Name"].str.contains(EXCLUDE_NAME_PATTERN, flags=re.I, regex=True)
    symbols = []
    for sym in df.loc[~excluded, symbol_col].str.strip():
        if not sym or any(c in sym for c in "$^=+"):
            continue  # preferreds and other odd share classes
        symbols.append(sym.replace(".", "-"))  # BRK.B -> BRK-B (Yahoo format)
    return symbols


def load_universe(refresh: bool = False) -> List[str]:
    cache = CACHE_DIR / f"universe_{now_et().date()}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())

    symbols: set = set()
    for name, urls in NASDAQ_FILES.items():
        text = None
        for url in urls:
            try:
                text = fetch_text(url)
                break
            except Exception as e:
                log(f"  could not fetch {url}: {e}")
        if text is None:
            older = sorted(CACHE_DIR.glob("universe_*.json"))
            if older:
                log(f"  Nasdaq Trader unreachable; using {older[-1].name}")
                return json.loads(older[-1].read_text())
            sys.exit("Could not download the Nasdaq Trader symbol files and no cached copy exists.")
        symbols.update(parse_symbol_file(text, "Symbol" if name == "nasdaqlisted" else "ACT Symbol"))

    universe = sorted(symbols)
    cache.write_text(json.dumps(universe))
    return universe


# ------------------------------ downloads -------------------------------

def download_chunk(tickers: List[str], **kwargs) -> Tuple[Dict[str, pd.DataFrame], bool]:
    """One yf.download call. Returns ({ticker: bars}, hit_rate_limit)."""
    _yf_errors.take()  # discard anything left over from earlier calls
    try:
        df = yf.download(tickers, group_by="ticker", progress=False, auto_adjust=False,
                         threads=DOWNLOAD_THREADS, **kwargs)
    except Exception as e:
        return {}, looks_rate_limited(repr(e))

    got: Dict[str, pd.DataFrame] = {}
    if df is not None and not df.empty:
        if isinstance(df.columns, pd.MultiIndex):
            for t in df.columns.get_level_values(0).unique():
                bars = df[t].dropna(subset=["Close"])
                if not bars.empty:
                    got[t] = bars
        elif len(tickers) == 1:
            got[tickers[0]] = df.dropna(subset=["Close"])
    return got, any(looks_rate_limited(m) for m in _yf_errors.take())


def download_batched(tickers: List[str], label: str, **kwargs) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    chunks = [tickers[i:i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    for n, chunk in enumerate(chunks, 1):
        pending = chunk
        for attempt in range(MAX_RETRIES + 1):
            got, rate_limited = download_chunk(pending, **kwargs)
            out.update(got)
            pending = [t for t in pending if t not in got]
            # Missing tickers without a rate limit are just delisted/no data.
            if not pending or not rate_limited or attempt == MAX_RETRIES:
                break
            wait = RETRY_BACKOFF_SECONDS * 2 ** attempt
            log(f"\n  rate limited; retrying {len(pending)} tickers in {wait}s")
            time.sleep(wait)
        progress(f"  {label}: batch {n}/{len(chunks)} ({len(out)} with data)")
        if n < len(chunks):
            time.sleep(BATCH_PAUSE_SECONDS)
    if not status_hook:
        print(file=sys.stderr)
    return out


# -------------------------------- metrics -------------------------------

def expected_volume_fraction(ts: datetime) -> float:
    minutes = ts.hour * 60 + ts.minute
    pts = [(int(h) * 60 + int(m), f) for (hm, f) in VOLUME_CURVE for h, m in [hm.split(":")]]
    if minutes <= pts[0][0]:
        frac = pts[0][1]
    elif minutes >= pts[-1][0]:
        frac = pts[-1][1]
    else:
        frac = pts[-1][1]
        for (m0, f0), (m1, f1) in zip(pts, pts[1:]):
            if m0 <= minutes <= m1:
                frac = f0 + (f1 - f0) * (minutes - m0) / (m1 - m0)
                break
    return max(frac, MIN_VOLUME_FRACTION)


def fetch_float(ticker: str, cache: dict) -> Tuple[Optional[float], bool]:
    """(float shares, is_shares_outstanding_fallback)."""
    if ticker in cache:
        return tuple(cache[ticker])
    info = with_retries(lambda: yf.Ticker(ticker).info, f"{ticker} profile") or {}
    value, fallback = info.get("floatShares"), False
    if not value and USE_SHARES_OUTSTANDING_FALLBACK:
        value = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        fallback = bool(value)
    result = (float(value) if value else None, fallback)
    if info:  # don't cache failures, so they get retried next scan
        cache[ticker] = list(result)
    return result


def parse_news_item(item: dict) -> Optional[Tuple[str, str, datetime, str]]:
    """Handle both the old flat and the newer nested ('content') Yahoo formats."""
    c = item.get("content") if isinstance(item.get("content"), dict) else item
    title = c.get("title")
    if not title:
        return None
    published = None
    if c.get("providerPublishTime"):
        published = datetime.fromtimestamp(int(c["providerPublishTime"]), tz=timezone.utc)
    elif c.get("pubDate"):
        try:
            published = datetime.fromisoformat(str(c["pubDate"]).replace("Z", "+00:00"))
        except ValueError:
            pass
    if published is None:
        return None
    provider = c.get("publisher") or (c.get("provider") or {}).get("displayName") or ""
    link = (c.get("link") or (c.get("canonicalUrl") or {}).get("url")
            or (c.get("clickThroughUrl") or {}).get("url") or "")
    return title.strip(), provider, published, link


_news_cache: Dict[str, Tuple[float, Optional[tuple]]] = {}


def fetch_latest_news(ticker: str) -> Optional[Tuple[str, str, datetime, str]]:
    """Newest headline about `ticker` within NEWS_MAX_AGE_HOURS, else None."""
    cached = _news_cache.get(ticker)
    if cached and time.time() - cached[0] < NEWS_CACHE_MINUTES * 60:
        return cached[1]

    items = with_retries(lambda: yf.Ticker(ticker).get_news(count=10), f"{ticker} news") or []
    if not items:
        # Yahoo's per-ticker news endpoint is often down; search still works.
        search = with_retries(lambda: yf.Search(ticker, max_results=1, news_count=10,
                                                raise_errors=True), f"{ticker} news search")
        items = [n for n in (search.news if search else [])
                 if ticker in (n.get("relatedTickers") or [ticker])]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_MAX_AGE_HOURS)
    parsed = [p for p in map(parse_news_item, items) if p and p[2] >= cutoff]
    latest = max(parsed, key=lambda p: p[2]) if parsed else None
    _news_cache[ticker] = (time.time(), latest)
    return latest


# -------------------------------- Schwab --------------------------------

class SchwabAuthError(Exception):
    pass


def load_env(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("'\"")
    return env


class Schwab:
    """Minimal Schwab Market Data client: OAuth login, token refresh, quotes."""

    def __init__(self, app_key: str, app_secret: str, callback: str):
        self.app_key, self.app_secret, self.callback = app_key, app_secret, callback

    @classmethod
    def from_env(cls) -> Optional["Schwab"]:
        env = load_env(SCHWAB_ENV_FILE)
        key, secret = env.get("SCHWAB_APP_KEY"), env.get("SCHWAB_APP_SECRET")
        if not key or not secret:
            return None
        return cls(key, secret, env.get("SCHWAB_CALLBACK_URL", SCHWAB_DEFAULT_CALLBACK))

    # --- tokens ---
    def _load_token(self) -> dict:
        try:
            return json.loads(SCHWAB_TOKEN_FILE.read_text())
        except (OSError, ValueError):
            raise SchwabAuthError("not logged in to Schwab; run: python momentum.py --schwab-login")

    def _save_token(self, token: dict) -> None:
        SCHWAB_TOKEN_FILE.write_text(json.dumps(token, indent=2))
        SCHWAB_TOKEN_FILE.chmod(0o600)

    def _token_request(self, form: dict) -> dict:
        r = requests.post(SCHWAB_TOKEN_URL, auth=(self.app_key, self.app_secret), data=form, timeout=20)
        if r.status_code != 200:
            raise SchwabAuthError(f"Schwab token request failed ({r.status_code}): {r.text[:200]}")
        return r.json()

    def has_token(self) -> bool:
        return SCHWAB_TOKEN_FILE.exists()

    def login(self) -> None:
        url = (f"{SCHWAB_AUTHORIZE_URL}?client_id={self.app_key}"
               f"&redirect_uri={urllib.parse.quote(self.callback, safe='')}")
        print("1. A Schwab login page will open (or copy this link into your browser):")
        print(f"   {url}")
        print("2. Log in with your normal Schwab brokerage login and approve access.")
        print("3. You'll land on a page that won't load (that's expected).")
        print("   Copy the FULL address from the address bar and paste it below quickly;")
        print("   the code in it expires in about 30 seconds.")
        webbrowser.open(url)
        received = input("\nPaste the address here: ").strip()
        code = urllib.parse.parse_qs(urllib.parse.urlparse(received).query).get("code", [None])[0]
        if not code:
            raise SchwabAuthError("no ?code= found in that address; run --schwab-login again")
        tok = self._token_request({"grant_type": "authorization_code", "code": code,
                                   "redirect_uri": self.callback})
        now = time.time()
        self._save_token({"access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
                          "access_expires_at": now + int(tok.get("expires_in", 1800)),
                          "refresh_issued_at": now})
        print(f"Logged in. You'll need to log in again in {SCHWAB_REFRESH_TOKEN_DAYS} days.")

    def _refresh(self) -> dict:
        token = self._load_token()
        try:
            tok = self._token_request({"grant_type": "refresh_token",
                                       "refresh_token": token["refresh_token"]})
        except SchwabAuthError:
            raise SchwabAuthError("Schwab login expired (it lasts 7 days); "
                                  "run: python momentum.py --schwab-login")
        token["access_token"] = tok["access_token"]
        token["refresh_token"] = tok.get("refresh_token", token["refresh_token"])
        token["access_expires_at"] = time.time() + int(tok.get("expires_in", 1800))
        self._save_token(token)
        return token

    def access_token(self) -> str:
        token = self._load_token()
        if time.time() > token.get("access_expires_at", 0) - 60:
            token = self._refresh()
        return token["access_token"]

    def login_days_left(self) -> Optional[float]:
        try:
            issued = self._load_token()["refresh_issued_at"]
        except (SchwabAuthError, KeyError):
            return None
        return SCHWAB_REFRESH_TOKEN_DAYS - (time.time() - issued) / 86400

    # --- data ---
    def get(self, path: str, params: dict) -> dict:
        for attempt in range(MAX_RETRIES + 1):
            r = requests.get(SCHWAB_API + path, params=params, timeout=20,
                             headers={"Authorization": f"Bearer {self.access_token()}"})
            if r.status_code == 401 and attempt == 0:
                self._refresh()
                continue
            if r.status_code == 429 and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * 2 ** attempt
                log(f"  Schwab rate limit; waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("Schwab request kept failing")

    def quotes(self, tickers: List[str]) -> Dict[str, dict]:
        """{yahoo_ticker: schwab quote object} for every ticker Schwab knows."""
        to_schwab = {t: t.replace("-", "/") for t in tickers}  # BRK-B -> BRK/B
        back = {v: k for k, v in to_schwab.items()}
        out: Dict[str, dict] = {}
        symbols = list(to_schwab.values())
        chunks = [symbols[i:i + SCHWAB_QUOTE_BATCH] for i in range(0, len(symbols), SCHWAB_QUOTE_BATCH)]
        for n, chunk in enumerate(chunks, 1):
            data = self.get("/marketdata/v1/quotes", {"symbols": ",".join(chunk),
                                                      "fields": "quote,extended,fundamental",
                                                      "indicative": "false"})
            for sym, q in data.items():
                if isinstance(q, dict) and sym in back and "quote" in q:
                    out[back[sym]] = q
            progress(f"  Schwab quotes: batch {n}/{len(chunks)} ({len(out)} quoted)")
        if not status_hook:
            print(file=sys.stderr)
        return out


def ms_to_et(ms) -> Optional[datetime]:
    return datetime.fromtimestamp(ms / 1000, tz=ET) if ms else None


# --------------------------------- scan ---------------------------------
# Each data source produces the same rows: {ticker, price, prev_close, volume,
# avg_vol} plus a `meta` dict describing the session and how fresh the data is.

_avg_vol_cache: Dict[Tuple[date, str], Optional[float]] = {}


def avg_volumes(tickers: List[str], session: date) -> Dict[str, Optional[float]]:
    """AVG_VOLUME_DAYS average daily volume before `session`, from Yahoo (cached)."""
    need = [t for t in tickers if (session, t) not in _avg_vol_cache]
    if need:
        data = download_batched(need, "average volume", period="3mo", interval="1d")
        for t in need:
            df = data.get(t)
            avg = None
            if df is not None:
                idx = pd.to_datetime(df.index)
                idx = idx.tz_localize(None) if idx.tz is not None else idx
                vols = df["Volume"][idx.normalize() < pd.Timestamp(session)].tail(AVG_VOLUME_DAYS)
                avg = float(vols.mean()) if len(vols) else None
            _avg_vol_cache[(session, t)] = avg
    return {t: _avg_vol_cache[(session, t)] for t in tickers}


def rows_from_quotes(parsed: Dict[str, dict]) -> Tuple[List[dict], date, datetime]:
    """Turn {ticker: {price, prev_close, traded, volume, fallback_avg}} into rows for
    stocks already in the price range and up enough, with average volume filled in."""
    # The newest session anyone has traded in. Tickers that haven't traded in it
    # yet (e.g. early premarket) are skipped.
    session = max(p["traded"].date() for p in parsed.values())
    rows = []
    for t, p in parsed.items():
        if p["traded"].date() != session:
            continue
        pct = (p["price"] / p["prev_close"] - 1) * 100
        if MIN_PRICE <= p["price"] <= MAX_PRICE and pct >= MIN_PCT_CHANGE:
            rows.append({"ticker": t, "price": p["price"], "prev_close": p["prev_close"],
                         "volume": p["volume"], "avg_vol": None})
    # Average volume only for the few stocks that are already up enough.
    avgs = avg_volumes([r["ticker"] for r in rows], session)
    for r in rows:
        r["avg_vol"] = avgs.get(r["ticker"]) or parsed[r["ticker"]].get("fallback_avg")
    return rows, session, max(p["traded"] for p in parsed.values())


def schwab_rows(universe: List[str], schwab: "Schwab", ts: datetime) -> Tuple[List[dict], dict]:
    quotes = schwab.quotes(universe)
    parsed = {}
    for t, q in quotes.items():
        quote, ext = q.get("quote") or {}, q.get("extended") or {}
        price, traded = quote.get("lastPrice"), quote.get("tradeTime") or 0
        # Use the extended-hours print if it is newer than the regular one.
        if ext.get("lastPrice") and (ext.get("tradeTime") or 0) > traded:
            price, traded = ext["lastPrice"], ext["tradeTime"]
        prev_close = quote.get("closePrice")
        if not price or not prev_close or not traded:
            continue
        parsed[t] = {
            "price": float(price), "prev_close": float(prev_close), "traded": ms_to_et(traded),
            "volume": float(max(quote.get("totalVolume") or 0, ext.get("totalVolume") or 0)),
            "fallback_avg": (q.get("fundamental") or {}).get("avg10DaysVolume"),
            "realtime": q.get("realtime"),
        }
    if not parsed:
        raise RuntimeError("Schwab returned no usable quotes")

    rows, session, newest = rows_from_quotes(parsed)
    delayed = sum(1 for p in parsed.values() if p["realtime"] is False)
    meta = {"source": "Schwab", "session": session, "considered": len(parsed), "newest": newest,
            "note": (f"{delayed} quotes flagged delayed by Schwab" if delayed
                     else "Schwab real-time quotes")}
    return rows, meta


def yahoo_quotes(tickers: List[str]) -> Dict[str, dict]:
    """Yahoo's batch quote endpoint: ~200 symbols per request, so the whole
    market is ~30 requests. Uses yfinance's session (cookies + crumb)."""
    from yfinance.data import YfData

    out: Dict[str, dict] = {}
    chunks = [tickers[i:i + YAHOO_QUOTE_BATCH] for i in range(0, len(tickers), YAHOO_QUOTE_BATCH)]
    failed = 0
    for n, chunk in enumerate(chunks, 1):
        params = {"symbols": ",".join(chunk), "formatted": "false"}
        data = with_retries(lambda: YfData().get_raw_json(YAHOO_QUOTE_URL, params=params),
                            f"Yahoo quotes batch {n}")
        result = ((data or {}).get("quoteResponse") or {}).get("result") or []
        if not result:
            failed += 1
        wanted = set(chunk)
        for q in result:
            if q.get("symbol") in wanted:
                out[q["symbol"]] = q
        progress(f"  Yahoo quotes: batch {n}/{len(chunks)} ({len(out)} quoted)")
        if n < len(chunks):
            time.sleep(BATCH_PAUSE_SECONDS)
    if not status_hook:
        print(file=sys.stderr)
    if failed:
        log(f"  {failed} of {len(chunks)} Yahoo quote batches came back empty")
    return out


def yahoo_rows(universe: List[str], ts: datetime) -> Tuple[Optional[List[dict]], dict]:
    quotes = yahoo_quotes(universe)
    parsed = {}
    for t, q in quotes.items():
        if q.get("quoteType") not in (None, "EQUITY"):
            continue
        reg_price, reg_time = q.get("regularMarketPrice"), q.get("regularMarketTime") or 0
        price, traded = reg_price, reg_time
        # Use the premarket/after-hours price if it is newer than the regular one.
        for p_key, t_key in (("preMarketPrice", "preMarketTime"), ("postMarketPrice", "postMarketTime")):
            p, t_ = q.get(p_key), q.get(t_key) or 0
            if p and t_ > traded:
                price, traded = p, t_
        if not price or not traded or not reg_price:
            continue
        traded_et = datetime.fromtimestamp(traded, tz=ET)
        if traded_et.date() > datetime.fromtimestamp(reg_time, tz=ET).date():
            # Premarket: today's regular session hasn't started, so the "regular"
            # price is yesterday's close. Yahoo has no premarket volume.
            prev_close, volume = reg_price, 0.0
        else:
            prev_close, volume = q.get("regularMarketPreviousClose"), float(q.get("regularMarketVolume") or 0)
        if not prev_close:
            continue
        parsed[t] = {"price": float(price), "prev_close": float(prev_close), "traded": traded_et,
                     "volume": volume,
                     "fallback_avg": q.get("averageDailyVolume3Month") or q.get("averageDailyVolume10Day")}
    if not parsed:
        log("No quotes came back from Yahoo (down or rate limiting). Try again shortly.")
        return None, {}

    rows, session, newest = rows_from_quotes(parsed)
    meta = {"source": "Yahoo", "session": session, "considered": len(parsed), "newest": newest,
            "note": "Yahoo quotes"}
    return rows, meta


def scan(universe: List[str], float_cache: dict, schwab: Optional["Schwab"]) -> Optional[dict]:
    """Run one scan. Returns a dict with results and metadata, or None if no data came back."""
    started = time.time()
    ts = now_et()

    rows, meta = None, {}
    if schwab:
        try:
            rows, meta = schwab_rows(universe, schwab, ts)
        except SchwabAuthError as e:
            log(f"Schwab: {e}. Using Yahoo for this scan.")
        except Exception as e:
            log(f"Schwab failed ({e!r}). Using Yahoo for this scan.")
    if rows is None:
        rows, meta = yahoo_rows(universe, ts)
        if rows is None:
            return None

    session = meta["session"]
    live = session == ts.date()
    premarket = live and ts.hour * 60 + ts.minute < MARKET_OPEN_MIN
    frac = expected_volume_fraction(ts) if live else 1.0

    stage1 = []
    for r in rows:
        price, pct = r["price"], (r["price"] / r["prev_close"] - 1) * 100
        if not (MIN_PRICE <= price <= MAX_PRICE and pct >= MIN_PCT_CHANGE):
            continue
        rvol = r["volume"] / (r["avg_vol"] * frac) if r["volume"] > 0 and r["avg_vol"] else None
        if rvol is None:
            if not (premarket and PASS_UNKNOWN_RVOL_PREMARKET):
                continue
        elif rvol < MIN_REL_VOLUME:
            continue
        stage1.append({"ticker": r["ticker"], "price": price, "pct": pct, "rvol": rvol})

    # Stage 2: float, then news, only for the handful that passed stage 1.
    stage2 = []
    for row in stage1:
        flt, fallback = fetch_float(row["ticker"], float_cache)
        if flt is None or flt >= MAX_FLOAT:
            continue
        row.update(float=flt, float_fallback=fallback)
        stage2.append(row)
    for row in stage2:
        row["news"] = fetch_latest_news(row["ticker"])
    results = [r for r in stage2 if r["news"] or not REQUIRE_NEWS]
    results.sort(key=lambda r: r["pct"], reverse=True)

    save_float_cache(float_cache)
    days = schwab.login_days_left() if schwab else None
    return {"results": results, "ts": ts, "meta": meta, "live": live, "premarket": premarket,
            "elapsed": time.time() - started, "schwab_days_left": days,
            "funnel": [len(universe), meta["considered"], len(stage1), len(stage2), len(results)]}


def run_scan(universe: List[str], float_cache: dict, schwab: Optional["Schwab"]) -> None:
    res = scan(universe, float_cache, schwab)
    if res:
        print_report(res)


# -------------------------------- output --------------------------------

def fmt_shares(n: float) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K"


def fmt_age(published: datetime) -> str:
    mins = int((datetime.now(timezone.utc) - published).total_seconds() // 60)
    return f"{mins}m" if mins < 60 else f"{mins // 60}h"


def report_header(res: dict) -> List[str]:
    """Plain-English lines describing a scan: phase, source, data delay, warnings, funnel."""
    ts, meta, live, premarket = res["ts"], res["meta"], res["live"], res["premarket"]
    session, newest, source = meta["session"], meta["newest"], meta["source"]
    lag_min = max(0.0, (ts - newest).total_seconds() / 60)
    phase = "PREMARKET" if premarket else ("LIVE" if live else f"LAST SESSION ({session})")
    lines = [f"Momentum scan  {ts:%Y-%m-%d %H:%M:%S} ET  [{phase}]  source: {source}"]
    if live:
        lines.append(f"Data delay: newest {source} trade is from {newest:%H:%M:%S} ET, "
                     f"{lag_min:.1f} min before the scan started. The scan took {res['elapsed']:.0f}s, "
                     f"so the oldest prices are about {lag_min + res['elapsed'] / 60:.1f} min old. "
                     f"({meta['note']})")
    else:
        lines.append(f"Market is not trading for {ts:%Y-%m-%d} yet; showing the {session} session "
                     f"(last data {newest:%H:%M} ET).")
    if premarket and source == "Yahoo":
        lines.append("Premarket: Yahoo reports no premarket volume, so RVOL shows n/a and is "
                     + ("NOT filtered." if PASS_UNKNOWN_RVOL_PREMARKET else "filtered out."))
    days = res.get("schwab_days_left")
    if days is not None and days < 1.5:
        lines.append(f"Heads up: Schwab login expires in {max(days, 0) * 24:.0f}h. "
                     "Run: python momentum.py --schwab-login")
    u, c, s1, s2, final = res["funnel"]
    lines.append(f"Funnel: {u} tickers -> {c} quoted -> {s1} pass price/%chg/RVOL "
                 f"-> {s2} pass float -> {final} with news")
    return lines


def print_report(res: dict) -> None:
    results = res["results"]
    width = shutil.get_terminal_size((120, 20)).columns
    print()
    print("=" * min(width, 110))
    for line in report_header(res):
        print(line)
    print("-" * min(width, 110))

    if not results:
        print("No stocks match right now.")
        return

    header = f"{'#':>2}  {'Ticker':<6} {'Price':>7} {'Chg%':>7} {'RVol':>6} {'Float':>7}  {'Age':>4}  Headline"
    print(header)
    for i, r in enumerate(results, 1):
        rvol = f"{r['rvol']:.1f}x" if r["rvol"] is not None else "n/a"
        flt = fmt_shares(r["float"]) + ("*" if r["float_fallback"] else "")
        if r["news"]:
            title, provider, published, _ = r["news"]
            age, headline = fmt_age(published), f"{title} ({provider})" if provider else title
        else:
            age, headline = "-", "(no news in window)"
        line = (f"{i:>2}  {r['ticker']:<6} {r['price']:>7.2f} {r['pct']:>6.1f}% {rvol:>6} "
                f"{flt:>7}  {age:>4}  ")
        room = max(width - len(line), 20)
        print(line + (headline if len(headline) <= room else headline[:room - 1] + "…"))
    if any(r["float_fallback"] for r in results):
        print("* no float on Yahoo; showing shares outstanding (the float is at most this).")


# -------------------------- background scanner --------------------------

class BackgroundScanner:
    """Rescans in a background thread while someone is viewing the app.

    The Streamlit page calls touch() on every refresh and reads snapshot().
    The thread stops after `idle_stop` seconds without a viewer and restarts
    on the next touch(), so it doesn't hammer Yahoo when nobody is looking.
    """

    def __init__(self, every: int = LOOP_SECONDS, idle_stop: int = 300):
        self.every, self.idle_stop = every, idle_stop
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.latest: Optional[dict] = None
        self.status = "Starting..."
        self.scanning = False
        self.error: Optional[str] = None
        self.first_seen: Dict[Tuple[date, str], datetime] = {}
        self.last_view = time.time()

    def touch(self) -> None:
        self.last_view = time.time()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="momentum-scanner", daemon=True)
                self._thread.start()

    def scan_now(self) -> None:
        self._wake.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {"latest": self.latest, "status": self.status, "scanning": self.scanning,
                    "error": self.error, "first_seen": dict(self.first_seen)}

    def _set_status(self, msg: str) -> None:
        if msg:
            self.status = msg

    def _run(self) -> None:
        global status_hook
        status_hook = self._set_status
        schwab = Schwab.from_env()
        if schwab and not schwab.has_token():
            schwab = None
        loaded_for, universe, float_cache = None, [], {}

        while time.time() - self.last_view < self.idle_stop:
            self.scanning, self.error = True, None
            if loaded_for != now_et().date():  # new day: fresh ticker list and floats
                try:
                    universe, float_cache = load_universe(), load_float_cache()
                    loaded_for = now_et().date()
                except BaseException as e:  # load_universe calls sys.exit on failure
                    self.error, self.scanning = f"Could not load the ticker list: {e}", False
                    return
            try:
                res = scan(universe, float_cache, schwab)
                if res is None:
                    self.error = "No price data came back (Yahoo down or rate limiting). Retrying."
                else:
                    session = res["meta"]["session"]
                    with self._lock:
                        for r in res["results"]:
                            self.first_seen.setdefault((session, r["ticker"]), res["ts"])
                        self.latest = res
            except Exception as e:
                self.error = f"Scan failed: {e!r}"
            self.scanning = False
            self.status = f"Waiting {self.every}s until the next scan"
            self._wake.wait(self.every)
            self._wake.clear()
        self.status = "Paused (no one viewing)"


# --------------------------------- main ---------------------------------

def load_float_cache() -> dict:
    path = CACHE_DIR / f"floats_{now_et().date()}.json"
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def save_float_cache(cache: dict) -> None:
    try:
        (CACHE_DIR / f"floats_{now_et().date()}.json").write_text(json.dumps(cache))
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Small-cap momentum screener (free data).")
    parser.add_argument("--loop", action="store_true", help=f"rescan every {LOOP_SECONDS}s")
    parser.add_argument("--every", type=int, default=LOOP_SECONDS, help="seconds between scans in --loop mode")
    parser.add_argument("--tickers", help="comma-separated tickers to scan instead of the full universe")
    parser.add_argument("--refresh", action="store_true", help="re-download today's ticker list")
    parser.add_argument("--source", choices=["auto", "schwab", "yahoo"], default=DATA_SOURCE,
                        help="where live prices and volume come from")
    parser.add_argument("--schwab-login", action="store_true", help="log in to Schwab (needed every 7 days)")
    parser.add_argument("--schwab-test", metavar="SYMBOL", help="print Schwab's raw quote for one symbol")
    args = parser.parse_args()

    schwab = None
    if args.source != "yahoo" or args.schwab_login or args.schwab_test:
        schwab = Schwab.from_env()
        if schwab is None and (args.source == "schwab" or args.schwab_login or args.schwab_test):
            sys.exit(f"Put SCHWAB_APP_KEY and SCHWAB_APP_SECRET in {SCHWAB_ENV_FILE} first "
                     "(see .env.example).")
    if args.schwab_login:
        schwab.login()
        return
    if args.schwab_test:
        sym = args.schwab_test.upper().replace("-", "/")
        print(json.dumps(schwab.get("/marketdata/v1/quotes", {
            "symbols": sym, "fields": "quote,extended,fundamental", "indicative": "false"}), indent=2))
        return
    if schwab and args.source == "auto" and not schwab.has_token():
        log("Schwab keys found but not logged in yet; using Yahoo. Run --schwab-login.")
        schwab = None

    if args.tickers:
        universe = sorted({t.strip().upper() for t in args.tickers.split(",") if t.strip()})
    else:
        log("Loading ticker universe from Nasdaq Trader...")
        universe = load_universe(args.refresh)
    log(f"Universe: {len(universe)} tickers")

    float_cache = load_float_cache()
    while True:
        try:
            run_scan(universe, float_cache, schwab)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # never let one bad scan kill the loop
            log(f"Scan failed: {e!r}")
        if not args.loop:
            break
        log(f"Next scan in {args.every}s (Ctrl+C to stop)")
        time.sleep(args.every)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
