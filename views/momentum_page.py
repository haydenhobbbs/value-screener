from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

import momentum as m
import replay

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
    "Score": (
        "Experimental 0-100 rank: smaller float, fresher news, earlier in the move and "
        "higher volume score higher. Built from one day's replay; the Track record "
        "below shows whether high scores actually do better. Not a buy signal."
    ),
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
    if kind == "score":
        return st.column_config.ProgressColumn(help=help_, min_value=0, max_value=100, format="%d", **kw)
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
            "Ticker": r["ticker"], "Score": r["score"], "Price": r["price"], "Change %": r["pct"],
            "Rel volume": r["rvol"], "Float (M)": r["float"] / 1e6, "Float est.": r["float_fallback"],
            **news_cols(r),
            "First seen (ET)": f["time"].astimezone(m.ET).strftime("%I:%M:%S %p").lstrip("0"),
            "Flag price": f["price"], "Since flag %": since_flag(r["ticker"]),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "New": col("text", "New", width="small"), "Ticker": col("text", "Ticker", width="small"),
        "Score": col("score", "Score", width="small"),
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


st.divider()
st.markdown("### Track record (replay log)")
st.caption(
    "Every weekday after the close, replay.py re-runs the day minute by minute and logs each "
    "stock the screener would have flagged: what it scored, and what happened if you bought "
    "at the flag and held to the 4 PM close (long), or shorted it with a "
    f"{replay.SHORT_STOP_PCT:g}% stop-loss (short). Fills are assumed at the flag price, shares "
    "are assumed borrowable for shorts, and there are no fees, so real results would be worse. "
    "A handful of days proves nothing; give it a few weeks. Not investment advice."
)
try:
    log = pd.read_csv(replay.LOG_PATH)
except FileNotFoundError:
    log = pd.DataFrame()
summary = replay.log_summary(log) if not log.empty else {}
if not summary:
    st.info("No replay history yet. It fills in automatically after each trading day.")
else:
    o = summary["overall"].iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Days logged", int(o["Days"]))
    c2.metric("Stocks flagged", int(o["Trades"]))
    c3.metric("Long win rate (hold to close)", f"{o['Long win rate']:.0f}%",
              f"avg {o['Long avg']:+.1f}% per trade", delta_color="off")
    c4.metric(f"Short win rate ({replay.SHORT_STOP_PCT:g}% stop)", f"{o['Short (stop) win rate']:.0f}%",
              f"avg {o['Short (stop) avg']:+.1f}% per trade", delta_color="off")
    stat_cols = {
        "Trades": st.column_config.NumberColumn(format="%d"),
        "Long win rate": st.column_config.NumberColumn(format="%.0f%%"),
        "Long avg": st.column_config.NumberColumn(format="%+.1f%%"),
        "Short (stop) win rate": st.column_config.NumberColumn(format="%.0f%%"),
        "Short (stop) avg": st.column_config.NumberColumn(format="%+.1f%%"),
        "Avg peak after flag": st.column_config.NumberColumn(format="%+.1f%%"),
    }
    left, right = st.columns(2)
    with left:
        st.markdown("**By rank score:** do high scores beat low ones?")
        st.dataframe(summary["by_score"], hide_index=True, width="stretch", column_config=stat_cols)
    with right:
        st.markdown("**By float**")
        st.dataframe(summary["by_float"], hide_index=True, width="stretch", column_config=stat_cols)
    with st.expander("Every logged trade"):
        st.dataframe(log.sort_values(["date", "flag_time_et"], ascending=[False, True]),
                     hide_index=True, width="stretch")
