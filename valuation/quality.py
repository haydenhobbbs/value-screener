"""Sanity checks that a human would normally do by hand before trusting a
"statistically cheap" stock: is revenue actually growing, is the company
profitable and cash-generative, is debt under control, and does Wall Street's
own independent analyst consensus agree there's upside.

None of these prove a stock is a good investment - they just rule out the
most common way a valuation screen fools you: a value trap, where the price
is low because the business is deteriorating, not because the market is
wrong. Each check is skipped (not failed) when the underlying data isn't
available, since sparse fundamentals shouldn't silently disqualify a stock.
"""

from __future__ import annotations

MAX_DEBT_TO_EQUITY = 200  # yfinance reports this as a percentage (e.g. 150 = 1.5x)
MIN_ANALYST_COVERAGE = 3  # ignore target price consensus with too few analysts


def _revenue_not_declining(f: dict) -> bool | None:
    hist = [v for v in f.get("revenue_history") or [] if v is not None]
    if len(hist) < 2:
        return None
    newest, oldest = hist[0], hist[-1]
    if oldest <= 0:
        return None
    return newest >= oldest  # flat-to-growing over the available history


def _profitable(f: dict) -> bool | None:
    hist = [v for v in f.get("net_income_history") or [] if v is not None]
    if not hist:
        return None
    return hist[0] > 0  # most recent fiscal year was net-income positive


def _fcf_positive(f: dict) -> bool | None:
    hist = [v for v in f.get("fcf_history") or [] if v is not None]
    if not hist:
        return None
    return hist[0] > 0


def _debt_reasonable(f: dict) -> bool | None:
    dte = f.get("debt_to_equity")
    if dte is None:
        return None
    return dte <= MAX_DEBT_TO_EQUITY


def _analyst_consensus_agrees(f: dict) -> bool | None:
    target = f.get("target_mean_price")
    coverage = f.get("number_of_analyst_opinions") or 0
    price = f.get("price")
    if target is None or coverage < MIN_ANALYST_COVERAGE or not price:
        return None
    return target > price


CHECKS = {
    "revenue_not_declining": _revenue_not_declining,
    "profitable": _profitable,
    "fcf_positive": _fcf_positive,
    "debt_reasonable": _debt_reasonable,
    "analyst_consensus_agrees": _analyst_consensus_agrees,
}


def run_quality_checks(f: dict) -> dict:
    """Returns each check's result (True/False/None-if-not-applicable), plus
    a summary: how many applicable checks passed vs. how many applied.
    """
    results = {name: check(f) for name, check in CHECKS.items()}
    applicable = [v for v in results.values() if v is not None]
    passed = sum(1 for v in applicable if v)
    results["checks_applicable"] = len(applicable)
    results["checks_passed"] = passed
    results["passes_all_applicable"] = len(applicable) > 0 and passed == len(applicable)
    return results
