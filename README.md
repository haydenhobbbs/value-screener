# Value Screener

Screens the Russell 3000 (~2,600 US stocks) for stocks where the current
market price sits well below an estimated fair value, as a starting point
for value-investing research.

**This is not investment advice.** It's a rough fundamentals model with a lot
of simplifying assumptions (see "Methodology and limitations" below) — treat
flagged stocks as candidates for further research, not buy signals.

## How it works

1. `run.py` pulls fundamentals for every Russell 3000 ticker via `yfinance`.
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

## What DCF actually means

DCF stands for **Discounted Cash Flow**. The core idea: a business is worth
the cash it will hand its owners in the future — but a dollar you get in 5
years is worth less than a dollar today, so you shrink ("discount") each
future dollar down to what it's worth right now, then add them all up.

Two reasons a future dollar is worth less than a today-dollar:
1. **Opportunity cost** - a dollar today could be invested and grow, so a
   future dollar has to beat that to be worth waiting for.
2. **Risk** - the further out and the riskier the business, the less certain
   that cash actually shows up, so you discount it more.

Here's the actual math `valuation/dcf.py` runs, step by step, using AAPL's
real numbers from testing this model:

1. **Start with free cash flow (FCF)** - cash the business generates from
   operations, minus what it has to spend just maintaining itself (capital
   expenditures). This is the cash that's actually available to hand to
   owners. AAPL's most recent year: ~$98.7B.
2. **Project it forward 5 years** at some growth rate, estimated from the
   company's own FCF history (capped 0-20%, since extreme extrapolations
   aren't trustworthy). AAPL's FCF was flat over the lookback window, so
   growth = 0% - the same $98.7B projected for each of the next 5 years.
3. **Pick a discount rate** (WACC - weighted average cost of capital): the
   return an investor would demand to accept the risk of *this specific*
   business, based on how volatile the stock is (beta) and how much debt vs.
   equity it uses. AAPL came out to about 9.8%.
4. **Discount each projected year back to today** - divide year 1's cash
   flow by (1.098)¹, year 2's by (1.098)², etc. Money further in the future
   gets shrunk more.
5. **Add a "terminal value"** - everything past year 5, assumed to grow
   slowly forever (2.5%, roughly GDP growth), also discounted back to today.
   This is usually the biggest chunk of the total, since "forever" is a lot
   of cash flow.
6. **Add it all up** = Enterprise Value - what the whole business (debt +
   equity together) is worth.
7. **Subtract net debt** (debt minus cash) = Equity Value - what's left over
   for shareholders specifically.
8. **Divide by shares outstanding** = DCF value per share. For AAPL this
   came out to **$83.56**, against an actual price of $332 - a huge gap,
   which is exactly the case flagged elsewhere in this doc as the model
   being too conservative on mega-cap compounders (flat near-term growth
   plus a 5-year cutoff badly undersells a company that keeps compounding
   and buying back stock for decades).

That per-share number is what gets compared against the actual stock price -
if DCF says a stock is worth more than it's trading for, that's one of the
two signals (alongside relative valuation) the screener looks for agreement
on.

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

- **A DCF value exists and, together with the relative-value estimate, both
  say the stock is underpriced** - not just the blended average. This rules
  out Financial Services and Real Estate (DCF-excluded by design, see
  above) and any stock whose DCF failed its own sanity checks. The track
  record showed this matters: no-DCF picks ran 14% win rate / -2.74% avg
  return vs 30% / -1.91% for picks with a DCF - a lone relative-value
  estimate has nothing to cross-check it against.
- Revenue isn't declining, the company is profitable, free cash flow is
  positive, debt is at a sane level, and (with enough analyst coverage) Wall
  Street's own independent consensus target price also sees upside.
- At least 3 of those quality checks had enough data to actually run.
- The stock has a known sector, so the relative-valuation cross-check had a
  real peer group to compare against.

`top_picks.csv` is uncapped - every stock that clears every check ships. In
practice this quality bar alone doesn't discriminate much among Russell 3000 blue
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
- Universe is the Russell 3000 (effectively all investable US equities) - see valuation/universe.py for the fallback chain - limited to companies with market cap above
  $2B, to avoid illiquid names skewing the sector-median comparisons.
