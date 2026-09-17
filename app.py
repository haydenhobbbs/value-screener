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
        "fair_value", "margin_of_safety", "undervalued", "market_cap",
    ]],
    use_container_width=True,
    hide_index=True,
)

st.subheader("Top 20 by margin of safety")
top20 = filtered.sort_values("margin_of_safety", ascending=False).head(20).set_index("ticker")
st.bar_chart(top20["margin_of_safety"])
