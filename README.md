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
5. `app.py` is a Streamlit dashboard that reads `results/latest.csv` and lets
   you filter/sort the screen.
6. `.github/workflows/update.yml` runs `run.py` on a schedule (weekdays after
   market close) and commits the refreshed results back to the repo — so the
   dashboard updates itself with no manual redeploy.

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
