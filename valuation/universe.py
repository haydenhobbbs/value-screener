"""Ticker universe to screen. Tries to pull the current S&P 500 list from
Wikipedia; falls back to a small static list of large caps if that fails
(e.g. no network, or the page structure changed) so the pipeline never
hard-fails just because it can't find a ticker list.
"""

import io

import pandas as pd
import requests

WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
# Wikipedia returns 403 to requests without a browser-like User-Agent.
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (value-screener; +https://github.com)"}

FALLBACK_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "BRK-B", "TSLA", "JPM",
    "V", "UNH", "XOM", "JNJ", "PG", "MA", "HD", "MRK", "ABBV", "COST", "PEP",
    "AVGO", "KO", "WMT", "BAC", "CVX", "ADBE", "CRM", "MCD", "TMO", "CSCO",
    "ABT", "ACN", "LIN", "DHR", "NFLX", "AMD", "PFE", "TXN", "NKE", "DIS",
    "INTC", "VZ", "CMCSA", "WFC", "PM", "COP", "UPS", "ORCL", "IBM", "GE",
]


def get_sp500_tickers() -> list[str]:
    try:
        resp = requests.get(WIKI_SP500_URL, headers=REQUEST_HEADERS, timeout=10)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        df = tables[0]
        tickers = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
        if len(tickers) > 400:
            return sorted(set(tickers))
    except Exception:
        pass
    return sorted(set(FALLBACK_TICKERS))
