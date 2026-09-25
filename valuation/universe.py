"""Ticker universe to screen: the Russell 3000 (~2,600 tickers, effectively
all investable US equities), sourced from iShares' daily-published IWV ETF
holdings file - there's no clean official Russell 3000 constituent list
that's free, but an ETF that tracks the index has to disclose its holdings
daily, which amounts to the same thing.

Falls back to the S&P 500 (from Wikipedia) if the iShares file is
unreachable or its format changes, and to a small static list of large caps
if that fails too, so the pipeline never hard-fails just because it can't
find a ticker list - it degrades to a smaller universe instead.
"""

from __future__ import annotations

import io

import pandas as pd
import requests

IWV_HOLDINGS_URL = "https://www.ishares.com/us/products/239714/ishares-russell-3000-etf/latest-holdings.csv"
WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
# Both sites block requests without a browser-like User-Agent.
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (value-screener; +https://github.com)"}

FALLBACK_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "BRK-B", "TSLA", "JPM",
    "V", "UNH", "XOM", "JNJ", "PG", "MA", "HD", "MRK", "ABBV", "COST", "PEP",
    "AVGO", "KO", "WMT", "BAC", "CVX", "ADBE", "CRM", "MCD", "TMO", "CSCO",
    "ABT", "ACN", "LIN", "DHR", "NFLX", "AMD", "PFE", "TXN", "NKE", "DIS",
    "INTC", "VZ", "CMCSA", "WFC", "PM", "COP", "UPS", "ORCL", "IBM", "GE",
]


def get_russell3000_tickers() -> list[str] | None:
    try:
        resp = requests.get(IWV_HOLDINGS_URL, headers=REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
        # The file leads with ~9 lines of fund metadata before the header row.
        df = pd.read_csv(io.StringIO(resp.text), skiprows=9, thousands=",")
        equities = df[
            (df["Asset Class"] == "Equity")
            & (df["Ticker"] != "-")
            & (df["Exchange"] != "NO MARKET (E.G. UNLISTED)")
        ]
        # yfinance expects a dash for dual-class shares; the source file uses
        # a dot for some (e.g. "BRK.B") and a plain space for others (e.g.
        # "BRK B") - normalize both.
        tickers = (
            equities["Ticker"].astype(str).str.strip()
            .str.replace(".", "-", regex=False)
            .str.replace(" ", "-", regex=False)
        )
        tickers = sorted(set(tickers))
        if len(tickers) > 2000:
            return tickers
    except Exception:
        pass
    return None


def get_sp500_tickers() -> list[str] | None:
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
    return None


def get_universe_tickers() -> list[str]:
    return get_russell3000_tickers() or get_sp500_tickers() or sorted(set(FALLBACK_TICKERS))
