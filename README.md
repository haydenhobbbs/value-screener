# Value Screener

Screens the S&P 500 for stocks where the current market price sits well below
an estimated fair value, as a starting point for value-investing research.

**This is not investment advice.** It's a rough fundamentals model with a lot
of simplifying assumptions (see "Methodology and limitations" below) — treat
flagged stocks as candidates for further research, not buy signals.

## How it works

1. `run.py` pulls fundamentals for every S&P 500 ticker via `yfinance`.
2. For each stock it computes two independent fair-value estimates:
   - **DCF** (`valuation/dcf.py`): projects free cash flow at a capped
     historical growth rate, discounts at a CAPM-based WACC, adds a
     Gordon-growth terminal value.
   - **Relative valuation** (`valuation/relative.py`): what the stock would
     be worth at its sector's median P/E and EV/EBITDA multiples.
3. The two estimates are blended (60% DCF / 40% relative) into a single
   "fair value," compared against the current price to get a **margin of
   safety**. Stocks priced ≥20% below fair value are flagged as undervalued.
4. Results are written to `results/latest.csv` (and archived daily under
   `results/history/`).
5. `track.py` reconstructs the actual top-N pick list from each day's
   archived snapshot and compares entry price to current price, writing
   `results/performance.csv` — a real track record of whether past picks
   panned out, not just today's scores.
6. `app.py` is a Streamlit dashboard that reads all of the above and lets
   you filter/sort the screen and see the track record.
7. `.github/workflows/update.yml` runs `run.py` then `track.py` on a schedule
   (weekdays after market close) and commits the refreshed results back to
   the repo — so the dashboard updates itself with no manual redeploy.

## Running it locally

```bash
pip install -r requirements.txt
python run.py        # populates results/latest.csv
streamlit run app.py
```

## Deploying so it updates itself

1. Push this repo to GitHub (see below).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with
   GitHub, and create a new app pointing at this repo's `app.py` on the
   `main` branch.
3. That's it — the GitHub Action refreshes `results/latest.csv` on its
   schedule, Streamlit Community Cloud auto-redeploys on every push to
   `main`, and you never touch a terminal again.

You can also trigger a refresh manually any time from the repo's **Actions**
tab ("Update valuations" → "Run workflow").

## "High-confidence picks" (`results/top_picks.csv`)

The full screen ranks 500 stocks by margin of safety, which is noisy on its
own - a huge gap between price and fair value is just as likely to mean the
model's assumptions are off as it is to mean the stock is a bargain. The
picks tier (`valuation/quality.py`, wired into `run.py`) narrows that down to
stocks where every applicable signal agrees:

- Every valuation method that could be computed (DCF, relative) says the
  stock is underpriced - not just the blended average.
- Revenue isn't declining, the company is profitable, free cash flow is
  positive, debt is at a sane level, and (with enough analyst coverage) Wall
  Street's own independent consensus target price also sees upside.
- At least 3 of those quality checks had enough data to actually run.
- The stock has a known sector, so the relative-valuation cross-check had a
  real peer group to compare against.

`top_picks.csv` is uncapped - every stock that clears every check ships. In
practice this quality bar alone doesn't discriminate much among S&P 500 blue
chips (most of them are profitable with sane debt), so in a broad market
pullback the list can run to 100+ names rather than a tidy handful. An empty
`top_picks.csv` on a given day is a normal, correct outcome of a strict
filter, not a bug.

It's ranked by `valuation_gap_pct` (how closely the DCF and relative-multiple
estimates agree with each other), not by margin of safety. `track.py`'s
history backs this up empirically: entry-day margin of safety had ~zero
correlation with subsequent returns (-0.04), while the gap between the two
valuation methods correlated at -0.21 - the strongest single signal found so
far. That tracks with the theory: two independently-built estimates landing
near the same number is real corroboration; a huge margin of safety is often
just the DCF (the more assumption-sensitive of the two methods) having gone
noisy on that particular stock, not the market handing out a rare bargain.
Margin of safety still gates inclusion (`MIN_EDGE` in `run.py`) - it's just
not what orders the list anymore.

This still isn't a buy signal. A recent spinoff, divestiture, or accounting
one-off can distort a company's trailing financials enough to fool every
check above at once (e.g. FIS's Worldpay divestiture skews its FCF and
earnings history) - always read why a pick is cheap before acting on it.

## Methodology and limitations

- Growth and discount-rate assumptions are simplified (flat equity risk
  premium, flat cost of debt, capped historical FCF growth) — this is a
  screen, not a research-grade DCF.
- Financials and REITs (`Financial Services`, `Real Estate` sectors) skip the
  DCF entirely and rely on relative valuation only. Their operating cash flow
  is dominated by float, trading positions, deposits, and reserve movements
  rather than owner earnings, so an FCF-based DCF produces nonsense (banks
  and insurers came out at 5-10x their real price in testing).
- The DCF's short 5-year horizon and flat 2.5% terminal growth tend to
  undervalue steady mega-cap compounders (e.g. Apple, Microsoft) relative to
  how the market actually prices their durability and buybacks. Treat a
  "DCF says overvalued, relative multiple says fairly valued" split as a
  signal to look closer, not as a clean overvalued call — and feel free to
  retune the constants in `valuation/dcf.py` (WACC inputs, terminal growth,
  DCF/relative weighting in `run.py`) if you disagree with the defaults.
- yfinance data can be incomplete or lag; tickers with insufficient data are
  silently skipped rather than guessed at.
- No quality/safety filters yet (e.g. excluding balance-sheet red flags,
  earnings quality, one-off items) — a stock can look statistically
  "undervalued" and still be a value trap. Read the filings before acting.
- Universe is limited to the S&P 500 and to companies with market cap above
  $2B, to avoid illiquid names skewing the sector-median comparisons.
