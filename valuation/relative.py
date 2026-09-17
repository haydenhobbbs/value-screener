"""Cross-check for the DCF: what would this stock be worth if it traded at
its sector's median multiple instead of its current one? A DCF can be wildly
wrong when growth/discount-rate assumptions are off, so anchoring part of
the "fair value" to how the market actually prices similar businesses keeps
the composite from being a pure model artifact.
"""

from __future__ import annotations

import pandas as pd


def sector_median_multiples(rows: list[dict]) -> dict:
    df = pd.DataFrame(rows)
    medians = {}
    for sector, group in df.groupby("sector"):
        medians[sector] = {
            "trailing_pe": group["trailing_pe"].median(skipna=True),
            "ev_to_ebitda": group["ev_to_ebitda"].median(skipna=True),
        }
    return medians


def relative_value_per_share(fundamentals: dict, sector_medians: dict) -> float | None:
    sector = fundamentals.get("sector")
    medians = sector_medians.get(sector, {})
    estimates = []

    median_pe = medians.get("trailing_pe")
    eps = fundamentals.get("trailing_eps")
    if median_pe and median_pe > 0 and eps and eps > 0:
        estimates.append(median_pe * eps)

    median_ev_ebitda = medians.get("ev_to_ebitda")
    ebitda = fundamentals.get("ebitda")
    shares = fundamentals.get("shares_outstanding")
    if median_ev_ebitda and median_ev_ebitda > 0 and ebitda and ebitda > 0 and shares:
        implied_ev = median_ev_ebitda * ebitda
        implied_equity = implied_ev - fundamentals.get("net_debt", 0)
        if implied_equity > 0:
            estimates.append(implied_equity / shares)

    if not estimates:
        return None
    return sum(estimates) / len(estimates)
