from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

import momentum as m

# Same idea as the value page: every column gets a hover tooltip.
COLUMN_HELP = {
    "New": "First appeared on the list within the last 5 minutes.",
    "Ticker": "Stock ticker symbol.",
    "Price": "Latest trade price, including premarket/after-hours trades.",
    "Change %": "Percent change from the previous day's close.",
    "Rel volume": (
        "Today's volume divided by what's normal by this time of day (30-day average "
        "scaled by a typical intraday volume curve). n/a = no volume data yet, e.g. "
        "premarket when using Yahoo."
    ),
    "Float (M)": "Shares available to trade, in millions, from Yahoo. Can lag after offerings or reverse splits.",
    "Float est.": "True if Yahoo had no float, so shares outstanding is shown instead (the real float is at most this).",
    "News age": "How long ago the latest headline was published.",
    "Headline": "Most recent news headline from the last 24 hours.",
    "Article": "Link to the article.",
    "First seen (ET)": (
        "Eastern time this stock first passed all five pillars today. Lets you tell a "
        "fresh mover from one that's been on the list for hours. Resets if the app restarts."
    ),
}


@st.cache_resource
def get_scanner() -> m.BackgroundScanner:
    # One scanner shared by every browser tab, so opening the page twice
    # doesn't double the requests to Yahoo/Schwab.
    return m.BackgroundScanner(every=m.LOOP_SECONDS)


scanner = get_scanner()

st.title("Momentum Screener")
st.caption(
    f"Ross Cameron's (Warrior Trading) 5 pillars: price \\${m.MIN_PRICE:g}–\\${m.MAX_PRICE:g}, "
    f"up at least {m.MIN_PCT_CHANGE:g}% on the day, {m.MIN_REL_VOLUME:g}x relative volume, "
    f"float under {m.MAX_FLOAT / 1e6:g}M shares, and a news headline in the last "
    f"{m.NEWS_MAX_AGE_HOURS}h. Scans every US-listed stock every {m.LOOP_SECONDS}s while this "
    "page is open. Thresholds live in the CONFIG section of momentum.py. Not investment advice."
)

col1, col2, _ = st.columns([1, 1, 4])
auto = col1.toggle("Auto-refresh", value=True, help="Update this page every 5 seconds.")
if col2.button("Scan now", help="Start the next scan immediately instead of waiting."):
    scanner.scan_now()


@st.fragment(run_every=5 if auto else None)
def live_view() -> None:
    scanner.touch()
    snap = scanner.snapshot()
    res = snap["latest"]

    if snap["error"]:
        st.warning(snap["error"])
    if snap["scanning"]:
        st.info(f"Scanning... {snap['status']}", icon=":material/progress_activity:")
    else:
        st.caption(snap["status"])

    if res is None:
        st.write("Waiting for the first scan. It usually takes under a minute.")
        return

    # Never let old results pass for current ones.
    age_min = (m.now_et() - res["ts"]).total_seconds() / 60
    if age_min > max(5, 3 * m.LOOP_SECONDS / 60):
        when = res["ts"].strftime("%a %b %d, %I:%M %p ET")
        st.warning(f"These results are from {when} ({age_min:.0f} min ago). "
                   + ("A new scan is running." if snap["scanning"] else "Click Scan now to refresh."),
                   icon=":material/schedule:")

    header = m.report_header(res)
    st.subheader(header[0])
    for line in header[1:-1]:
        st.caption(line)

    total, considered, stage1, stage2, final = res["funnel"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Stocks scanned", f"{total:,}")
    c2.metric("Price, % up and volume", stage1)
    c3.metric("…and float", stage2)
    c4.metric("…and news (matches)", final)

    results = res["results"]
    if not results:
        st.info("No stocks match all five pillars right now.")
        return

    session = res["meta"]["session"]
    rows = []
    for r in results:
        seen = snap["first_seen"].get((session, r["ticker"]), res["ts"])
        title, provider, published, link = r["news"] if r["news"] else (None, "", None, "")
        rows.append({
            "New": "NEW" if res["ts"] - seen <= timedelta(minutes=5) else "",
            "Ticker": r["ticker"],
            "Price": r["price"],
            "Change %": r["pct"],
            "Rel volume": r["rvol"],
            "Float (M)": r["float"] / 1e6,
            "Float est.": r["float_fallback"],
            "News age": m.fmt_age(published) if published else "",
            "Headline": f"{title} ({provider})" if title and provider else (title or ""),
            "Article": link or None,
            "First seen (ET)": seen.astimezone(m.ET).strftime("%I:%M:%S %p").lstrip("0"),
        })
    df = pd.DataFrame(rows)

    help_ = COLUMN_HELP
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        column_config={
            "New": st.column_config.TextColumn(help=help_["New"], width="small"),
            "Ticker": st.column_config.TextColumn(help=help_["Ticker"], width="small"),
            "Price": st.column_config.NumberColumn(help=help_["Price"], format="$%.2f"),
            "Change %": st.column_config.NumberColumn(help=help_["Change %"], format="%.1f%%"),
            "Rel volume": st.column_config.NumberColumn(help=help_["Rel volume"], format="%.1fx"),
            "Float (M)": st.column_config.NumberColumn(help=help_["Float (M)"], format="%.1f"),
            "Float est.": st.column_config.CheckboxColumn(help=help_["Float est."], width="small"),
            "News age": st.column_config.TextColumn(help=help_["News age"], width="small"),
            "Headline": st.column_config.TextColumn(help=help_["Headline"], width="large"),
            "Article": st.column_config.LinkColumn(help=help_["Article"], display_text="open"),
            "First seen (ET)": st.column_config.TextColumn(help=help_["First seen (ET)"]),
        },
    )


live_view()
