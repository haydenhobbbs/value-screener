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
from valuation.universe import get_universe_tickers

DCF_WEIGHT = 0.6
RELATIVE_WEIGHT = 0.4
UNDERVALUED_THRESHOLD = 0.20  # flag stocks priced >=20% below fair value
MIN_MARKET_CAP = 2_000_000_000  # skip illiquid micro/small caps
REQUEST_PAUSE_SECONDS = 0.5

# yfinance hits Yahoo's undocumented API directly and gets rate-limited hard
# past a few hundred requests in a short window - each ticker costs 3
# requests (info/cashflow/financials), so the full Russell 3000 is ~7,800
# requests per run. On a rate-limit error, back off and retry a few times
# before giving up on that ticker; if it keeps happening across many
# consecutive tickers, the whole run is blocked, not just unlucky - abort
# rather than burn through thousands of guaranteed-to-fail requests.
# Worst case before giving up on the whole run: threshold * sum(delays) =
# 8 * (15+45) = ~8 minutes of wasted retrying, not tens of minutes.
RATE_LIMIT_RETRY_DELAYS = [15, 45]
CONSECUTIVE_RATE_LIMIT_ABORT_THRESHOLD = 8
# If fewer than this fraction of the universe was actually fetched, the run
# was degraded (rate-limited, network issue, etc.) - refuse to write results
# rather than silently overwrite a good day's data with a tiny, misleading
# partial scan.
MIN_FETCH_FRACTION = 0.7

# "High-confidence pick" tier: every available valuation method must agree
# the stock is underpriced, every quality check that had data must pass (no
# revenue decline, profitable, FCF positive, sane debt, analysts
# independently see upside), and there must be enough data for that
# agreement to mean something. Every stock clearing all of that ships to
# results/top_picks.csv - uncapped, since the point is "everything that
# passes," not a fixed-size shortlist. In a broad market pullback that can
# still be 100+ names (quality checks alone don't discriminate much among
# Russell 3000 blue chips) - margin of safety just orders the list, it doesn't
# trim it.
MIN_EDGE = 0.01
MIN_APPLICABLE_QUALITY_CHECKS = 3

# Operating cash flow for these sectors is dominated by float, trading
# positions, deposits, and reserve movements rather than owner earnings, so
# an FCF-based DCF produces meaningless numbers (banks/insurers routinely
# came out at 5-10x their actual price in testing). Rely on relative
# valuation only for these - which also means stocks in these sectors can
# never become a high-confidence pick, since that tier now requires a DCF.
DCF_EXCLUDED_SECTORS = {"Financial Services", "Real Estate"}


def _is_rate_limit_error(exc: Exception) -> bool:
    return "rate limit" in str(exc).lower() or "too many requests" in str(exc).lower()


class SustainedRateLimitError(RuntimeError):
    """Raised when Yahoo keeps rate-limiting us across many consecutive
    tickers - a sign the whole run is blocked, not that a few requests were
    unlucky. Callers should stop fetching and work with whatever was already
    collected rather than burn through the rest of the universe.
    """


def _fetch_with_retry(ticker: str, position: str) -> dict | None:
    """Tries fetch_fundamentals, retrying on rate-limit errors with backoff.
    Returns None (and logs) for a non-rate-limit failure. Re-raises if every
    retry is still rate-limited, so the caller can track consecutive misses.
    """
    delays = [0] + RATE_LIMIT_RETRY_DELAYS
    for attempt, delay in enumerate(delays):
        if delay:
            print(f"{position} {ticker}: rate limited, retrying in {delay}s "
                  f"(attempt {attempt+1}/{len(delays)})", file=sys.stderr)
            time.sleep(delay)
        try:
            return fetch_fundamentals(ticker)
        except Exception as exc:
            if not _is_rate_limit_error(exc):
                print(f"{position} {ticker}: skipped ({exc})", file=sys.stderr)
                return None
            if attempt == len(delays) - 1:
                raise
    return None  # unreachable, satisfies type checkers


def build_dataset(tickers: list[str]) -> list[dict]:
    rows = []
    consecutive_rate_limits = 0

    for i, ticker in enumerate(tickers):
        position = f"[{i+1}/{len(tickers)}]"
        try:
            fundamentals = _fetch_with_retry(ticker, position)
        except Exception:
            consecutive_rate_limits += 1
            print(f"{position} {ticker}: still rate limited after retries "
                  f"({consecutive_rate_limits} in a row)", file=sys.stderr)
            if consecutive_rate_limits >= CONSECUTIVE_RATE_LIMIT_ABORT_THRESHOLD:
                raise SustainedRateLimitError(
                    f"{consecutive_rate_limits} consecutive tickers rate-limited even "
                    f"after retries - Yahoo has blocked this run. Stopping with "
                    f"{len(rows)}/{i+1} tickers fetched so far."
                )
            continue

        consecutive_rate_limits = 0
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

        # How much the two independent valuation methods disagree with each
        # other - not with the price. Two methods built on completely
        # different assumptions landing near the same number is a stronger
        # signal than either one alone; a wide gap usually means the DCF (the
        # more assumption-sensitive of the two) went noisy, not that the
        # stock is a rare bargain. NaN when only one method was available.
        if dcf_value and relative_value:
            valuation_gap_pct = abs(dcf_value - relative_value) / ((dcf_value + relative_value) / 2) * 100
        else:
            valuation_gap_pct = None

        quality = run_quality_checks(f)
        high_confidence = (
            signals_agree_undervalued
            and margin_of_safety >= MIN_EDGE
            and quality["checks_applicable"] >= MIN_APPLICABLE_QUALITY_CHECKS
            and quality["passes_all_applicable"]
            and pd.notna(f.get("sector"))  # missing sector means the relative-value
            # cross-check had no peer group to compare against - not a real signal
            and dcf_value is not None  # relative-valuation-only picks (mostly
            # Financial Services/Real Estate, DCF-excluded by design, plus any
            # stock whose DCF failed its own sanity checks) measurably
            # underperformed in the track record: 14% win rate / -2.74% avg
            # return vs 30% / -1.91% for picks that had a DCF. A lone
            # relative-value estimate has nothing to cross-check it against.
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
            "valuation_gap_pct": round(valuation_gap_pct, 2) if valuation_gap_pct is not None else None,
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
    tickers = get_universe_tickers()
    print(f"Screening {len(tickers)} tickers...")

    try:
        rows = build_dataset(tickers)
    except SustainedRateLimitError as exc:
        # Don't write anything - a tiny partial scan overwriting a good
        # day's results would be worse than just skipping today's update.
        sys.exit(f"Aborting without writing results: {exc}")

    print(f"Fetched fundamentals for {len(rows)} tickers.")

    fetch_fraction = len(rows) / len(tickers)
    if fetch_fraction < MIN_FETCH_FRACTION:
        sys.exit(
            f"Aborting without writing results: only fetched {len(rows)}/{len(tickers)} "
            f"tickers ({fetch_fraction:.0%}, below the {MIN_FETCH_FRACTION:.0%} floor) - "
            f"this run was degraded, not a real screen."
        )

    df = score(rows)
    df["last_updated"] = datetime.now(timezone.utc).isoformat()

    df.to_csv("results/latest.csv", index=False)
    history_path = f"results/history/{datetime.now(timezone.utc):%Y-%m-%d}.csv"
    df.to_csv(history_path, index=False)

    # Ranked by how tightly the two valuation methods agree (real signal),
    # not by margin-of-safety size (measured noise - see README). Stocks
    # with only one valuation method (NaN gap) sort last within the list,
    # since there's nothing to cross-check them against.
    top_picks = df[df["high_confidence_pick"]].sort_values(
        ["valuation_gap_pct", "margin_of_safety"], ascending=[True, False], na_position="last"
    )
    top_picks.to_csv("results/top_picks.csv", index=False)

    print(f"Wrote {len(df)} scored tickers to results/latest.csv")
    print(f"Wrote {len(top_picks)} high-confidence picks to results/top_picks.csv")
    print(top_picks[["ticker", "sector", "price", "fair_value", "valuation_gap_pct", "margin_of_safety"]].head(20).to_string(index=False))


if __name__ == "__main__":
    main()
