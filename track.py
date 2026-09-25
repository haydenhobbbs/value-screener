"""Builds a track record for every high-confidence pick the screener has
ever made: the price when it was first flagged, the price now, the return
since, and whether it's still flagged today.

Consecutive days a ticker stays flagged count as one "episode" (entry = the
first day it appeared), not a new pick every day it happens to still qualify
- otherwise a stock that sits at the top for two weeks would look like
fourteen separate calls instead of one.

Reads results/history/*.csv (written by run.py on every scheduled run) and
writes results/performance.csv. Run this after run.py, not instead of it.
"""

from __future__ import annotations

import glob
import os

import pandas as pd

from run import TOP_N_PICKS

HISTORY_GLOB = "results/history/*.csv"
LATEST_PATH = "results/latest.csv"
OUTPUT_PATH = "results/performance.csv"


def load_history() -> dict[str, pd.DataFrame]:
    history = {}
    for path in sorted(glob.glob(HISTORY_GLOB)):
        date = os.path.splitext(os.path.basename(path))[0]
        df = pd.read_csv(path)
        if "high_confidence_pick" not in df.columns:
            continue  # snapshot predates the picks feature; nothing to track
        history[date] = df
    return history


def price_on(df: pd.DataFrame, ticker: str) -> float | None:
    row = df.loc[df["ticker"] == ticker]
    return float(row.iloc[0]["price"]) if not row.empty else None


def group_consecutive(indices: list[int]) -> list[list[int]]:
    groups, current = [], [indices[0]]
    for idx in indices[1:]:
        if idx == current[-1] + 1:
            current.append(idx)
        else:
            groups.append(current)
            current = [idx]
    groups.append(current)
    return groups


def top_picks_for(df: pd.DataFrame) -> set[str]:
    """Reconstructs the actual top-N pick list for a day's snapshot - the
    same selection run.py applies when it writes results/top_picks.csv -
    rather than every ticker that merely passed the quality gate. That gate
    alone passes ~25% of the S&P 500 in a broad pullback; the dashboard only
    ever shows the top N, so that's what a track record should measure.
    """
    qualifying = df.loc[df["high_confidence_pick"] == True]  # noqa: E712
    top = qualifying.sort_values("margin_of_safety", ascending=False).head(TOP_N_PICKS)
    return set(top["ticker"])


def build_track_record(history: dict[str, pd.DataFrame], latest: pd.DataFrame) -> pd.DataFrame:
    dates = sorted(history.keys())
    flagged_dates_by_ticker: dict[str, list[str]] = {}
    for d in dates:
        for ticker in top_picks_for(history[d]):
            flagged_dates_by_ticker.setdefault(ticker, []).append(d)

    still_flagged_today = top_picks_for(latest)
    today = dates[-1]

    rows = []
    for ticker, flagged_dates in flagged_dates_by_ticker.items():
        indices = sorted(dates.index(d) for d in flagged_dates)
        for group in group_consecutive(indices):
            entry_date = dates[group[0]]
            last_flagged_date = dates[group[-1]]

            entry_price = price_on(history[entry_date], ticker)
            current_price = price_on(latest, ticker)
            if entry_price is None or current_price is None:
                continue

            return_pct = (current_price - entry_price) / entry_price * 100
            is_open = last_flagged_date == today and ticker in still_flagged_today

            rows.append({
                "ticker": ticker,
                "entry_date": entry_date,
                "entry_price": round(entry_price, 2),
                "last_flagged_date": last_flagged_date,
                "current_price": round(current_price, 2),
                "return_pct": round(return_pct, 2),
                "days_since_entry": (pd.Timestamp(today) - pd.Timestamp(entry_date)).days,
                "status": "still flagged" if is_open else "no longer flagged",
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(["entry_date", "ticker"]).reset_index(drop=True)


def main():
    history = load_history()
    if not history:
        print("No history with the picks feature yet - nothing to track.")
        return

    latest = pd.read_csv(LATEST_PATH)
    track_record = build_track_record(history, latest)
    track_record.to_csv(OUTPUT_PATH, index=False)

    if track_record.empty:
        print("No picks have been flagged yet.")
        return

    win_rate = (track_record["return_pct"] > 0).mean() * 100
    avg_return = track_record["return_pct"].mean()
    print(f"Wrote {len(track_record)} pick episodes to {OUTPUT_PATH}")
    print(f"Win rate: {win_rate:.1f}%  |  Average return: {avg_return:.2f}%")
    print(track_record.to_string(index=False))


if __name__ == "__main__":
    main()
