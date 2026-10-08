from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

# Resolve data files relative to this script, not the process's working
# directory - Streamlit can be launched from elsewhere (e.g. a preview tool
# or a different cwd on Streamlit Cloud) and relative paths would silently
# fail to find results/*.csv otherwise.
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

# Tooltip text for every column shown anywhere below - st.column_config
# renders these as a small "?" icon in the header on hover, so a column's
# meaning is one hover away instead of needing to remember or re-read the
# README every time.
COLUMN_HELP = {
    "ticker": "Stock ticker symbol.",
    "sector": "GICS sector classification, from Yahoo Finance.",
    "price": "Current market price.",
    "dcf_value": (
        "Discounted Cash Flow estimate: what the business's future cash flows are worth "
        "today, independent of market price. Blank for Financial Services/Real Estate "
        "(FCF-based DCF doesn't work for them) or if this stock's DCF failed its own "
        "sanity checks. See the README's 'What DCF actually means' section for the full "
        "step-by-step walkthrough."
    ),
    "relative_value": (
        "What the stock would be worth if it traded at its sector's median P/E and "
        "EV/EBITDA multiples, instead of its own."
    ),
    "fair_value": "Blended estimate: 60% DCF + 40% relative value, or whichever one is available if only one exists.",
    "margin_of_safety": (
        "(fair_value - price) / fair_value - how far below fair value the price sits. "
        "NOT a probability or a confidence level, just a gap size. Our own track record "
        "found this barely correlates with actual subsequent returns."
    ),
    "valuation_gap_pct": (
        "How much the DCF and relative-value estimates disagree with each other. Lower = "
        "more agreement between two independent methods = stronger signal. This mattered "
        "more than margin-of-safety size in the track record."
    ),
    "undervalued": "True if margin of safety is 20% or more.",
    "revenue_not_declining": "True if the most recent year's revenue is at or above revenue from several years back.",
    "profitable": "True if the most recent fiscal year had positive net income.",
    "fcf_positive": "True if the most recent year's free cash flow was positive.",
    "debt_reasonable": "True if debt-to-equity is under 200%.",
    "analyst_consensus_agrees": (
        "True if Wall Street's average analyst price target is above the current price "
        "(requires at least 3 analysts covering the stock; blank if too few do)."
    ),
    "quality_checks_passed": "How many of the 5 quality checks passed.",
    "quality_checks_applicable": "How many of the 5 quality checks had enough data to actually run.",
    "market_cap": "Total market value of the company: price x shares outstanding.",
    "entry_date": "The day this stock was first flagged as a high-confidence pick (consecutive flagged days count as one entry, not a new pick each day).",
    "entry_price": "Price on the entry date.",
    "last_flagged_date": "Most recent day this stock was still flagged as a high-confidence pick.",
    "current_price": "Today's price.",
    "return_pct": "Percent change from entry_price to current_price.",
    "days_since_entry": "Calendar days since the entry date.",
    "status": "Whether it's still flagged as a high-confidence pick today, or has since dropped off the list.",
}


def column_config(columns: list[str]) -> dict:
    return {c: st.column_config.Column(help=COLUMN_HELP[c]) for c in columns if c in COLUMN_HELP}

st.title("Value Screener")
st.caption(
    "A fundamentals-based estimate of fair value (blended DCF + sector-relative "
    "multiples) compared against current price. This is a starting point for "
    "research, not investment advice."
)

try:
    df = pd.read_csv(RESULTS_DIR / "latest.csv")
except FileNotFoundError:
    st.warning(
        "No results yet. Run `python run.py` locally, or wait for the scheduled "
        "GitHub Action to populate results/latest.csv."
    )
    st.stop()

last_updated = df["last_updated"].iloc[0] if "last_updated" in df.columns and len(df) else "unknown"
st.caption(f"Last updated: {last_updated}")

st.header("High-confidence picks")
st.caption(
    "Requires both a DCF and a sector-relative-multiple estimate, and both must agree "
    "the stock is underpriced - excludes Financial Services/Real Estate (no reliable "
    "DCF) and any stock whose DCF failed sanity checks, since the track record showed "
    "DCF-less picks perform measurably worse (14% win rate vs 30%). It also has to pass "
    "every applicable quality check: revenue not declining, profitable, free-cash-flow "
    "positive, debt at a sane level, and Wall Street's own analyst consensus "
    "independently sees upside too. The size of the margin of safety is secondary here "
    "- agreement across independent signals is the point, not the exact percentage. "
    "Still not investment advice: read the why-it's-cheap story yourself before acting "
    "on any of these."
)

try:
    picks = pd.read_csv(RESULTS_DIR / "top_picks.csv")
except FileNotFoundError:
    picks = pd.DataFrame()

if picks.empty:
    st.info(
        "No stock currently clears every check at once. That's a normal, expected "
        "outcome of a strict filter - it means nothing this run is worth act­ing on, "
        "not that the screener is broken."
    )
else:
    st.caption(
        f"{len(picks)} stocks clear every check today. In a broad market pullback "
        "the quality checks alone don't discriminate much among Russell 3000 blue chips, "
        "so this list can run long - it's not a curated shortlist, it's everything "
        "that passes. Ranked by valuation_gap_pct (how closely the DCF and relative "
        "multiple agree with each other) - the tracker found that mattered a lot "
        "more than the margin-of-safety size does, which turned out to be mostly noise."
    )
    picks_cols = [
        "ticker", "sector", "price", "fair_value", "valuation_gap_pct", "margin_of_safety",
        "revenue_not_declining", "profitable", "fcf_positive",
        "debt_reasonable", "analyst_consensus_agrees",
    ]
    st.dataframe(
        picks[picks_cols],
        width="stretch",
        hide_index=True,
        column_config=column_config(picks_cols),
    )

st.divider()
st.header("Track record")
st.caption(
    "How past picks have actually done: entry price is what it cost the day it was "
    "first flagged as a top pick, current price is today's. Consecutive days the same "
    "stock stays flagged count as one entry, not a new pick each day. Statistically "
    "meaningless with only a few weeks of history - this becomes worth trusting only "
    "after months of runs accumulate."
)

try:
    performance = pd.read_csv(RESULTS_DIR / "performance.csv")
except FileNotFoundError:
    performance = pd.DataFrame()

if performance.empty:
    st.info("No closed-out track record yet - check back after the screener has run for a while.")
else:
    col1, col2, col3 = st.columns(3)
    col1.metric("Pick episodes", len(performance))
    col2.metric("Win rate", f"{(performance['return_pct'] > 0).mean() * 100:.0f}%")
    col3.metric("Average return", f"{performance['return_pct'].mean():.2f}%")
    st.dataframe(
        performance.sort_values("entry_date", ascending=False),
        width="stretch",
        hide_index=True,
        column_config=column_config(performance.columns.tolist()),
    )

st.divider()
st.header("Full screen")
st.caption("Every Russell 3000 stock scored, for browsing/research beyond the strict picks above.")

sectors = sorted(df["sector"].dropna().unique().tolist())
col1, col2, col3 = st.columns(3)
with col1:
    selected_sectors = st.multiselect("Sector", sectors, default=sectors)
with col2:
    min_margin = st.slider("Minimum margin of safety", -0.5, 1.0, 0.0, 0.05)
with col3:
    undervalued_only = st.checkbox("Undervalued only (>=20% margin)", value=False)

filtered = df[df["sector"].isin(selected_sectors) & (df["margin_of_safety"] >= min_margin)]
if undervalued_only:
    filtered = filtered[filtered["undervalued"]]

st.subheader(f"{len(filtered)} stocks")
full_screen_cols = [
    "ticker", "sector", "price", "dcf_value", "relative_value",
    "fair_value", "margin_of_safety", "undervalued",
    "quality_checks_passed", "quality_checks_applicable", "market_cap",
]
st.dataframe(
    filtered[full_screen_cols],
    width="stretch",
    hide_index=True,
    column_config=column_config(full_screen_cols),
)

st.subheader("Top 20 by margin of safety")
top20 = filtered.sort_values("margin_of_safety", ascending=False).head(20).set_index("ticker")
st.bar_chart(top20["margin_of_safety"])
