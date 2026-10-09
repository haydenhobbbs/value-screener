#!/usr/bin/env python3
"""
Replay a trading day through the momentum screener, minute by minute.

For every stock that ran that day, this answers:
  * When would the screener have flagged it (all five pillars), and at what price?
  * If you bought at the flag price and held to the 4:00 PM close, would you
    have been in profit? (the "hold to close" win rate)
  * How high did it go after the flag?

It uses the exact thresholds and logic in momentum.py, in Yahoo mode: premarket
volume is unknown, so relative volume isn't required before 9:30 ET (same as
the live screener's PASS_UNKNOWN_RVOL_PREMARKET setting).

Usage:
    python replay.py                      # the most recent session, all of its runners
    python replay.py --tickers VIVK,FEDU  # specific stocks (any day in the last ~7)
    python replay.py --date 2026-10-08 --tickers VIVK,FEDU

Limits:
  * Yahoo keeps 1-minute bars for about 7 days, so older days can't be replayed.
  * Without --tickers, runners are found from today's quotes (day high 10%+ above
    the previous close), which only works for the most recent session.
  * Float is today's float; news is whatever Yahoo's search returns now (the last
    ~20 headlines), checked against each minute's timestamp.
  * Fills are assumed at the 1-minute close. Real fills on fast small caps are
    often worse, so treat these results as optimistic.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

import momentum as m

REGULAR_CLOSE = (16, 0)
RUNNER_MIN_DAY_VOLUME_X = 2.0   # when auto-finding runners: full-day volume vs. 3-month average


def find_runners(session: Optional[date]) -> List[str]:
    """Stocks whose day high was MIN_PCT_CHANGE%+ above the previous close (latest session)."""
    quotes = m.yahoo_quotes(m.load_universe())
    out = []
    for t, q in quotes.items():
        pc, hi = q.get("regularMarketPreviousClose"), q.get("regularMarketDayHigh")
        vol, avg = q.get("regularMarketVolume") or 0, q.get("averageDailyVolume3Month") or 0
        if q.get("quoteType") != "EQUITY" or not pc or not hi or not avg:
            continue
        if session and datetime.fromtimestamp(q.get("regularMarketTime") or 0, tz=m.ET).date() != session:
            continue
        if (hi / pc - 1) * 100 >= m.MIN_PCT_CHANGE and m.MIN_PRICE <= hi and pc <= m.MAX_PRICE \
                and vol / avg >= RUNNER_MIN_DAY_VOLUME_X:
            out.append(t)
    return sorted(out)


def minute_bars(ticker: str, day: date) -> pd.DataFrame:
    df = yf.download(ticker, start=day.isoformat(), end=(day + timedelta(days=1)).isoformat(),
                     interval="1m", prepost=True, progress=False, auto_adjust=False,
                     multi_level_index=False)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.dropna(subset=["Close"])
    idx = df.index.tz_localize("UTC") if df.index.tz is None else df.index
    df.index = idx.tz_convert(m.ET)
    return df[df.index.date == day]


def prev_close_and_avg(ticker: str, day: date):
    daily = yf.download(ticker, period="6mo", interval="1d", progress=False, auto_adjust=False,
                        multi_level_index=False)
    if daily is None or daily.empty:
        return None, None
    idx = pd.to_datetime(daily.index)
    idx = idx.tz_localize(None) if idx.tz is not None else idx
    before = daily[idx.normalize() < pd.Timestamp(day)]
    if before.empty:
        return None, None
    return float(before["Close"].iloc[-1]), float(before["Volume"].tail(m.AVG_VOLUME_DAYS).mean())


def replay_ticker(ticker: str, day: date) -> Optional[dict]:
    bars = minute_bars(ticker, day)
    prev_close, avg_vol = prev_close_and_avg(ticker, day)
    if bars.empty or not prev_close or not avg_vol:
        return None
    flt, flt_fallback = m.fetch_float(ticker, {})
    news = m.fetch_news_items(ticker)
    if news is None:
        print(f"\n  {ticker}: news lookup failed (Yahoo busy); treating as no news")
    news_times = [p[2] for p in news or []]
    float_ok = flt is not None and flt < m.MAX_FLOAT

    cum_vol = bars["Volume"].fillna(0).cumsum()
    flag = flag_no_news = None
    for ts, bar in bars.iterrows():
        price = float(bar["Close"])
        pct = (price / prev_close - 1) * 100
        minutes = ts.hour * 60 + ts.minute
        premarket = minutes < m.MARKET_OPEN_MIN
        vol = float(cum_vol.loc[ts])
        rvol = vol / (avg_vol * m.expected_volume_fraction(ts)) if vol > 0 else None
        rvol_ok = (rvol is None and premarket and m.PASS_UNKNOWN_RVOL_PREMARKET) or \
                  (rvol is not None and rvol >= m.MIN_REL_VOLUME)
        utc = ts.astimezone(timezone.utc)
        news_ok = any(utc - timedelta(hours=m.NEWS_MAX_AGE_HOURS) <= n <= utc for n in news_times)
        four = m.MIN_PRICE <= price <= m.MAX_PRICE and pct >= m.MIN_PCT_CHANGE and rvol_ok and float_ok
        if four and flag_no_news is None:
            flag_no_news = (ts, price, rvol)
        if four and news_ok:
            flag = (ts, price, rvol)
            break

    regular = bars[[(t.hour, t.minute) < REGULAR_CLOSE for t in bars.index]]
    close_4pm = float(regular["Close"].iloc[-1]) if not regular.empty else None

    def outcome(f):
        if f is None:
            return {}
        ts, price, rvol = f
        after = bars[bars.index >= ts]
        after_reg = after[[(t.hour, t.minute) < REGULAR_CLOSE for t in after.index]]
        out = {"flag_time": ts, "flag_price": price, "flag_rvol": rvol}
        if not after_reg.empty:
            peak_i = after_reg["High"].idxmax()
            out.update(peak=float(after_reg["High"].max()), peak_time=peak_i,
                       low=float(after_reg["Low"].min()))
        if close_4pm is not None and (ts.hour, ts.minute) < REGULAR_CLOSE:
            out["close_4pm"] = close_4pm
            out["ret_close"] = (close_4pm / price - 1) * 100
        return out

    return {"ticker": ticker, "prev_close": prev_close, "float": flt, "float_fallback": flt_fallback,
            "day_high": float(bars["High"].max()), "close_4pm": close_4pm,
            "flag": outcome(flag), "flag_no_news": outcome(flag_no_news)}


def summarize(results: List[dict], key: str, label: str) -> None:
    flagged = [r for r in results if r[key]]
    held = [r for r in flagged if "ret_close" in r[key]]
    print(f"\n=== {label} ===")
    if not flagged:
        print("Nothing would have been flagged.")
        return
    print(f"{'Ticker':<6} {'Flagged':>8} {'Entry':>7} {'RVol':>6} {'Peak after':>16} "
          f"{'4PM close':>10} {'Hold to close':>13}")
    for r in sorted(flagged, key=lambda r: r[key]["flag_time"]):
        f = r[key]
        rvol = f"{f['flag_rvol']:.1f}x" if f["flag_rvol"] else "n/a"
        peak = (f"{f['peak']:.2f} (+{(f['peak'] / f['flag_price'] - 1) * 100:.0f}%)"
                if "peak" in f else "-")
        if "ret_close" in f:
            close, ret = f"{f['close_4pm']:.2f}", f"{f['ret_close']:+.1f}% {'WIN' if f['ret_close'] > 0 else 'LOSS'}"
        else:
            close, ret = "-", "after close"
        print(f"{r['ticker']:<6} {f['flag_time']:%I:%M %p} {f['flag_price']:>7.2f} {rvol:>6} "
              f"{peak:>16} {close:>10} {ret:>13}")
    if held:
        wins = [r for r in held if r[key]["ret_close"] > 0]
        rets = [r[key]["ret_close"] for r in held]
        print(f"\nHold-to-close win rate: {len(wins)}/{len(held)} = {len(wins) / len(held) * 100:.0f}%  |  "
              f"average return {sum(rets) / len(rets):+.1f}%  |  median {sorted(rets)[len(rets) // 2]:+.1f}%")
    if len(held) < len(flagged):
        print(f"({len(flagged) - len(held)} flagged after 4:00 PM, so not counted in the win rate.)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a day through the momentum screener.")
    parser.add_argument("--date", help="YYYY-MM-DD (default: most recent session)")
    parser.add_argument("--tickers", help="comma-separated tickers (default: that day's runners)")
    args = parser.parse_args()
    m.status_hook = lambda msg: None  # quiet progress output

    day = date.fromisoformat(args.date) if args.date else None
    if args.tickers:
        tickers = sorted({t.strip().upper() for t in args.tickers.split(",") if t.strip()})
    else:
        if args.date:
            raise SystemExit("For a past date, pass --tickers (runners are only auto-found for today).")
        print("Finding today's runners...")
        tickers = find_runners(None)
    if day is None:
        last = minute_bars("SPY", m.now_et().date())
        day = m.now_et().date() if not last.empty else None
        if day is None:  # weekend/holiday: use the last day SPY traded
            spy = yf.download("SPY", period="5d", interval="1d", progress=False, multi_level_index=False)
            day = pd.to_datetime(spy.index[-1]).date()

    print(f"Replaying {len(tickers)} stocks for {day} (1-minute bars, criteria from momentum.py)...")
    results = []
    for i, t in enumerate(tickers, 1):
        r = m.with_retries(lambda: replay_ticker(t, day), f"{t} replay")
        if r:
            results.append(r)
        print(f"\r  {i}/{len(tickers)} {t:<6}", end="", flush=True)
    print()

    summarize(results, "flag", "Flagged by the screener (all 5 pillars)")
    summarize(results, "flag_no_news", "If the news pillar were dropped (4 pillars)")
    never = [r["ticker"] for r in results if not r["flag"] and not r["flag_no_news"]]
    if never:
        print(f"\nNever flagged ({len(never)}): {', '.join(never)}")
    print("\nEntries assume a fill at the 1-minute close price; real small-cap fills are usually "
          "worse. This is a backtest of one day, not a prediction or investment advice.")


if __name__ == "__main__":
    main()
