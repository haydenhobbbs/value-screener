"""Main pipeline: pull fundamentals for the universe, compute a DCF value and
a relative-valuation cross-check for each, blend them into a single "fair
value", and write out the ranked results.

This is a fundamentals-based value screen, not a prediction of what the
market will do next. Nothing here is investment advice.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import pandas as pd

from valuation.data import fetch_fundamentals
from valuation.dcf import dcf_value_per_share
from valuation.quality import run_quality_checks
from valuation.relative import relative_value_per_share, sector_median_multiples
from valuation.universe import get_sp500_tickers

DCF_WEIGHT = 0.6
RELATIVE_WEIGHT = 0.4
UNDERVALUED_THRESHOLD = 0.20  # flag stocks priced >=20% below fair value
MIN_MARKET_CAP = 2_000_000_000  # skip illiquid micro/small caps
REQUEST_PAUSE_SECONDS = 0.15

# "Top picks" tier: every available valuation method must agree the stock is
# underpriced, every quality check that had data must pass (no revenue
# decline, profitable, FCF positive, sane debt, analysts independently see
# upside), and there must be enough data for that agreement to mean
# something. Margin of safety just breaks ties among survivors - a stock
# that clears every check is the point, not the exact percentage.
MIN_EDGE = 0.01
MIN_APPLICABLE_QUALITY_CHECKS = 3
TOP_N_PICKS = 5

# Operating cash flow for these sectors is dominated by float, trading
# positions, deposits, and reserve movements rather than owner earnings, so
# an FCF-based DCF produces meaningless numbers (banks/insurers routinely
# came out at 5-10x their actual price in testing). Rely on relative
# valuation only for these.
DCF_EXCLUDED_SECTORS = {"Financial Services", "Real Estate"}


def build_dataset(tickers: list[str]) -> list[dict]:
    rows = []
    for i, ticker in enumerate(tickers):
        try:
            fundamentals = fetch_fundamentals(ticker)
        except Exception as exc:
            print(f"[{i+1}/{len(tickers)}] {ticker}: skipped ({exc})", file=sys.stderr)
            continue
        if fundamentals is None:
            continue
        rows.append(fundamentals)
        time.sleep(REQUEST_PAUSE_SECONDS)
    return rows


def score(rows: list[dict]) -> pd.DataFrame:
    sector_medians = sector_median_multiples(rows)
    out = []
    for f in rows:
        if not f.get("market_cap") or f["market_cap"] < MIN_MARKET_CAP:
            continue

        if f.get("sector") in DCF_EXCLUDED_SECTORS:
            dcf_value = None
        else:
            dcf_value = dcf_value_per_share(f)
        relative_value = relative_value_per_share(f, sector_medians)

        if dcf_value and relative_value:
            fair_value = DCF_WEIGHT * dcf_value + RELATIVE_WEIGHT * relative_value
        elif dcf_value or relative_value:
            fair_value = dcf_value or relative_value
        else:
            continue

        price = f["price"]
        margin_of_safety = (fair_value - price) / fair_value

        valuation_signals = [v for v in (dcf_value, relative_value) if v is not None]
        signals_agree_undervalued = bool(valuation_signals) and all(v > price for v in valuation_signals)

        quality = run_quality_checks(f)
        high_confidence = (
            signals_agree_undervalued
            and margin_of_safety >= MIN_EDGE
            and quality["checks_applicable"] >= MIN_APPLICABLE_QUALITY_CHECKS
            and quality["passes_all_applicable"]
            and pd.notna(f.get("sector"))  # missing sector means the relative-value
            # cross-check had no peer group to compare against - not a real signal
        )

        out.append({
            "ticker": f["ticker"],
            "sector": f.get("sector"),
            "price": round(price, 2),
            "dcf_value": round(dcf_value, 2) if dcf_value else None,
            "relative_value": round(relative_value, 2) if relative_value else None,
            "fair_value": round(fair_value, 2),
            "margin_of_safety": round(margin_of_safety, 4),
            "undervalued": margin_of_safety >= UNDERVALUED_THRESHOLD,
            "valuation_signals_agree": signals_agree_undervalued,
            "revenue_not_declining": quality["revenue_not_declining"],
            "profitable": quality["profitable"],
            "fcf_positive": quality["fcf_positive"],
            "debt_reasonable": quality["debt_reasonable"],
            "analyst_consensus_agrees": quality["analyst_consensus_agrees"],
            "quality_checks_passed": quality["checks_passed"],
            "quality_checks_applicable": quality["checks_applicable"],
            "high_confidence_pick": high_confidence,
            "market_cap": f["market_cap"],
        })

    df = pd.DataFrame(out).sort_values("margin_of_safety", ascending=False)
    return df.reset_index(drop=True)


def main():
    tickers = get_sp500_tickers()
    print(f"Screening {len(tickers)} tickers...")
    rows = build_dataset(tickers)
    print(f"Fetched fundamentals for {len(rows)} tickers.")

    df = score(rows)
    df["last_updated"] = datetime.now(timezone.utc).isoformat()

    df.to_csv("results/latest.csv", index=False)
    history_path = f"results/history/{datetime.now(timezone.utc):%Y-%m-%d}.csv"
    df.to_csv(history_path, index=False)

    top_picks = (
        df[df["high_confidence_pick"]]
        .sort_values("margin_of_safety", ascending=False)
        .head(TOP_N_PICKS)
    )
    top_picks.to_csv("results/top_picks.csv", index=False)

    print(f"Wrote {len(df)} scored tickers to results/latest.csv")
    print(f"Wrote {len(top_picks)} high-confidence picks to results/top_picks.csv")
    print(top_picks[["ticker", "sector", "price", "fair_value", "margin_of_safety"]].to_string(index=False))


if __name__ == "__main__":
    main()
