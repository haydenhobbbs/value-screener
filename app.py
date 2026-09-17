import pandas as pd
import streamlit as st

st.set_page_config(page_title="Value Screener", layout="wide")

st.title("Value Screener")
st.caption(
    "A fundamentals-based estimate of fair value (blended DCF + sector-relative "
    "multiples) compared against current price. This is a starting point for "
    "research, not investment advice."
)

try:
    df = pd.read_csv("results/latest.csv")
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
    "Every available valuation method (DCF, sector-relative multiples) agrees the "
    "stock is underpriced, AND it passes every applicable quality check: revenue "
    "not declining, profitable, free-cash-flow positive, debt at a sane level, and "
    "Wall Street's own analyst consensus independently sees upside too. The size of "
    "the margin of safety is secondary here - agreement across independent signals "
    "is the point, not the exact percentage. Still not investment advice: read the "
    "why-it's-cheap story yourself before acting on any of these."
)

try:
    picks = pd.read_csv("results/top_picks.csv")
except FileNotFoundError:
    picks = pd.DataFrame()

if picks.empty:
    st.info(
        "No stock currently clears every check at once. That's a normal, expected "
        "outcome of a strict filter - it means nothing this run is worth act­ing on, "
        "not that the screener is broken."
    )
else:
    st.dataframe(
        picks[[
            "ticker", "sector", "price", "fair_value", "margin_of_safety",
            "revenue_not_declining", "profitable", "fcf_positive",
            "debt_reasonable", "analyst_consensus_agrees",
        ]],
        use_container_width=True,
        hide_index=True,
    )

st.divider()
st.header("Full screen")
st.caption("Every S&P 500 stock scored, for browsing/research beyond the strict picks above.")

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
st.dataframe(
    filtered[[
        "ticker", "sector", "price", "dcf_value", "relative_value",
        "fair_value", "margin_of_safety", "undervalued",
        "quality_checks_passed", "quality_checks_applicable", "market_cap",
    ]],
    use_container_width=True,
    hide_index=True,
)

st.subheader("Top 20 by margin of safety")
top20 = filtered.sort_values("margin_of_safety", ascending=False).head(20).set_index("ticker")
st.bar_chart(top20["margin_of_safety"])
