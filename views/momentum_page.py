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
    "Day high %": "How far above the previous close the stock got at its high today.",
    "Rel volume": (
        "Today's volume divided by what's normal by this time of day (30-day average "
        "scaled by a typical intraday volume curve). Blank = no volume data yet, e.g. "
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
    "Flag price": "Price when the screener first flagged it today: the hypothetical entry.",
    "Since flag %": (
        "Gain or loss from the flag price to now (after 4 PM: to the 4 PM close). "
        "Assumes a fill at the flag price, which on fast small caps is optimistic."
    ),
    "Missing": "The one pillar this stock fails.",
    "Status": "Whether it's on the list now, and if not, which pillars it fails.",
}

def col(kind: str, name: str, **kw):
    """A column config of the given kind, with this page's tooltip attached."""
    help_ = COLUMN_HELP.get(name)
    if kind == "money":
        return st.column_config.NumberColumn(help=help_, format="$%.2f", **kw)
    if kind == "pct":
        return st.column_config.NumberColumn(help=help_, format="%+.1f%%", **kw)
    if kind == "x":
        return st.column_config.NumberColumn(help=help_, format="%.1fx", **kw)
    if kind == "num":
        return st.column_config.NumberColumn(help=help_, format="%.1f", **kw)
    if kind == "check":
        return st.column_config.CheckboxColumn(help=help_, **kw)
    if kind == "link":
        return st.column_config.LinkColumn(help=help_, display_text="open", **kw)
    return st.column_config.TextColumn(help=help_, **kw)


def news_cols(r: dict) -> dict:
    title, provider, published, link = r["news"] if r["news"] else (None, "", None, "")
    return {
        "News age": m.fmt_age(published) if published else "",
        "Headline": f"{title} ({provider})" if title and provider else (title or ""),
        "Article": link or None,
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

    session = res["meta"]["session"]
    flags = {t: v for (sess, t), v in snap["first_seen"].items() if sess == session}
    # After the 4 PM close, "held to close" means the regular-session close, not after-hours.
    after_close = not res["live"] or res["ts"].hour >= 16

    def since_flag(ticker: str):
        f, px = flags.get(ticker), res["prices"].get(ticker)
        if not f or not px:
            return None
        exit_px = (px["regular_price"] if after_close and px["regular_price"] else px["price"])
        return (exit_px / f["price"] - 1) * 100

    matches_section(res, flags, since_flag)
    win_rate_section(flags, since_flag, after_close)
    near_miss_section(res)
    runners_section(res, flags, since_flag)


def matches_section(res, flags, since_flag) -> None:
    st.markdown("### Matches: all 5 pillars")
    if not res["results"]:
        st.info("No stocks match all five pillars right now.")
        return
    rows = []
    for r in res["results"]:
        f = flags.get(r["ticker"]) or {"time": res["ts"], "price": r["price"]}
        rows.append({
            "New": "NEW" if res["ts"] - f["time"] <= timedelta(minutes=5) else "",
            "Ticker": r["ticker"], "Price": r["price"], "Change %": r["pct"],
            "Rel volume": r["rvol"], "Float (M)": r["float"] / 1e6, "Float est.": r["float_fallback"],
            **news_cols(r),
            "First seen (ET)": f["time"].astimezone(m.ET).strftime("%I:%M:%S %p").lstrip("0"),
            "Flag price": f["price"], "Since flag %": since_flag(r["ticker"]),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "New": col("text", "New", width="small"), "Ticker": col("text", "Ticker", width="small"),
        "Price": col("money", "Price"), "Change %": col("pct", "Change %"),
        "Rel volume": col("x", "Rel volume"), "Float (M)": col("num", "Float (M)"),
        "Float est.": col("check", "Float est.", width="small"),
        "News age": col("text", "News age", width="small"),
        "Headline": col("text", "Headline", width="large"), "Article": col("link", "Article"),
        "First seen (ET)": col("text", "First seen (ET)"), "Flag price": col("money", "Flag price"),
        "Since flag %": col("pct", "Since flag %"),
    })


def win_rate_section(flags, since_flag, after_close) -> None:
    rets = [x for x in (since_flag(t) for t in flags) if x is not None]
    if not rets:
        return
    wins = sum(1 for x in rets if x > 0)
    exit_label = "held to the 4 PM close" if after_close else "if sold now"
    c1, c2, c3 = st.columns(3)
    c1.metric("Flagged today", len(flags))
    c2.metric(f"In profit ({exit_label})", f"{wins}/{len(rets)} = {wins / len(rets) * 100:.0f}%")
    c3.metric("Average since flag", f"{sum(rets) / len(rets):+.1f}%")
    st.caption(
        "Win rate = flagged stocks you'd be up on if you bought at the flag price "
        f"and {exit_label}. Counts every stock flagged since the app started today, "
        "including ones that have since faded off the list. Fills at the flag price are "
        "optimistic; this is a scorecard, not a strategy."
    )


def near_miss_section(res) -> None:
    st.markdown("### Near misses: 4 of 5 pillars")
    st.caption("Moving and in the price range, but failing exactly one pillar. Check these "
               "yourself: Yahoo's small-cap news coverage is thin, so \"no news\" often "
               "just means Yahoo missed it.")
    if not res["near_misses"]:
        st.info("No near misses right now.")
        return
    rows = [{
        "Ticker": r["ticker"], "Price": r["price"], "Change %": r["pct"], "Rel volume": r["rvol"],
        "Float (M)": r["float"] / 1e6 if r["float"] else None, "Missing": r["missing"][0],
        **news_cols(r),
    } for r in res["near_misses"]]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Ticker": col("text", "Ticker", width="small"), "Price": col("money", "Price"),
        "Change %": col("pct", "Change %"), "Rel volume": col("x", "Rel volume"),
        "Float (M)": col("num", "Float (M)"), "Missing": col("text", "Missing", width="medium"),
        "News age": col("text", "News age", width="small"),
        "Headline": col("text", "Headline", width="large"), "Article": col("link", "Article"),
    })


def runners_section(res, flags, since_flag) -> None:
    st.markdown(f"### Today's runners: hit +{m.MIN_PCT_CHANGE:g}% at some point")
    st.caption(f"Every stock that got {m.MIN_PCT_CHANGE:g}%+ above yesterday's close today "
               f"(with at least {m.RUNNER_MIN_REL_VOLUME:g}x normal volume), even if it has faded "
               "since. Works even if you open the app late in the day. Yahoo doesn't report "
               "premarket highs, so a stock that only spiked premarket shows up only while "
               "it's still up.")
    if not res["runners"]:
        st.info("No runners yet today.")
        return
    rows = []
    for r in res["runners"]:
        f = flags.get(r["ticker"])
        rows.append({
            "Ticker": r["ticker"], "Day high %": r["high_pct"], "Change %": r["pct"],
            "Price": r["price"], "Rel volume": r["rvol"],
            "Float (M)": r["float"] / 1e6 if r["float"] else None,
            "First seen (ET)": f["time"].astimezone(m.ET).strftime("%I:%M %p").lstrip("0") if f else "",
            "Flag price": f["price"] if f else None, "Since flag %": since_flag(r["ticker"]),
            "Status": r["status"],
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Ticker": col("text", "Ticker", width="small"), "Day high %": col("pct", "Day high %"),
        "Change %": col("pct", "Change %"), "Price": col("money", "Price"),
        "Rel volume": col("x", "Rel volume"), "Float (M)": col("num", "Float (M)"),
        "First seen (ET)": col("text", "First seen (ET)"), "Flag price": col("money", "Flag price"),
        "Since flag %": col("pct", "Since flag %"), "Status": col("text", "Status", width="large"),
    })


live_view()
