"""Pulls the raw fundamentals yfinance exposes into a flat dict per ticker.

yfinance's field names shift between versions, so lookups are defensive:
missing pieces come back as None and downstream valuation code is expected
to handle that gracefully rather than assume every field is present.
"""

from __future__ import annotations

import yfinance as yf


def _first(series_dict, keys):
    """Return the first matching row (as a list of values, most-recent-first)
    found in a yfinance financial-statement DataFrame under any of `keys`.
    """
    for key in keys:
        if key in series_dict.index:
            return series_dict.loc[key].dropna().tolist()
    return []


def fetch_fundamentals(ticker: str) -> dict | None:
    t = yf.Ticker(ticker)
    info = t.info or {}
    if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
        return None

    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if not price:
        return None

    cashflow = t.cashflow
    if cashflow is None or cashflow.empty:
        operating_cf, capex = [], []
    else:
        operating_cf = _first(cashflow, ["Total Cash From Operating Activities", "Operating Cash Flow"])
        capex = _first(cashflow, ["Capital Expenditures", "Capital Expenditure"])

    fcf_history = []
    for ocf, cx in zip(operating_cf, capex):
        fcf_history.append(ocf + cx)  # capex is reported negative by yfinance

    total_debt = info.get("totalDebt") or 0
    total_cash = info.get("totalCash") or 0

    return {
        "ticker": ticker,
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "price": price,
        "market_cap": info.get("marketCap"),
        "shares_outstanding": info.get("sharesOutstanding"),
        "beta": info.get("beta"),
        "total_debt": total_debt,
        "total_cash": total_cash,
        "net_debt": total_debt - total_cash,
        "trailing_pe": info.get("trailingPE"),
        "forward_eps": info.get("forwardEps"),
        "trailing_eps": info.get("trailingEps"),
        "ebitda": info.get("ebitda"),
        "ev_to_ebitda": info.get("enterpriseToEbitda"),
        "fcf_history": fcf_history,  # most recent year first
    }
