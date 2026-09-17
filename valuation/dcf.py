"""A deliberately simple discounted-cash-flow model.

This is a rough estimate, not a research-grade valuation: it projects free
cash flow to the firm at a capped historical growth rate, discounts at a
CAPM-based WACC, and adds a Gordon-growth terminal value. It's meant to give
a directional "fair value" to compare against market price, not a precise
number to bet a position size on.
"""

from __future__ import annotations

RISK_FREE_RATE = 0.045   # ~current 10-year Treasury yield, update periodically
EQUITY_RISK_PREMIUM = 0.05
DEFAULT_BETA = 1.0
COST_OF_DEBT = 0.05
TAX_RATE = 0.21
TERMINAL_GROWTH = 0.025  # long-run GDP-ish growth
PROJECTION_YEARS = 5
MIN_GROWTH = 0.0
MAX_GROWTH = 0.20
MIN_BETA = 0.4   # yfinance occasionally reports near-zero betas (thin/odd
MAX_BETA = 2.0   # measurement windows) that make cost of equity meaningless
# A DCF's terminal value explodes as WACC approaches terminal growth (it's
# dividing by (wacc - terminal_growth)), so require a real spread rather
# than trusting whatever WACC the CAPM inputs happen to produce.
MIN_WACC_SPREAD = 0.04


def estimate_fcf_growth(fcf_history: list[float]) -> float | None:
    """CAGR of free cash flow over the available history, clipped to a sane
    range. Returns None if there isn't enough clean data to estimate from.
    """
    history = [v for v in fcf_history if v is not None]
    if len(history) < 2 or history[-1] <= 0:
        return None
    # fcf_history is most-recent-first; oldest is the last usable entry.
    newest, oldest = history[0], history[-1]
    years = len(history) - 1
    if oldest <= 0 or newest <= 0:
        return None
    cagr = (newest / oldest) ** (1 / years) - 1
    return max(MIN_GROWTH, min(MAX_GROWTH, cagr))


def estimate_wacc(market_cap: float, total_debt: float, beta: float | None) -> float | None:
    if not market_cap or market_cap <= 0:
        return None
    beta = beta if beta is not None else DEFAULT_BETA
    beta = max(MIN_BETA, min(MAX_BETA, beta))
    cost_of_equity = RISK_FREE_RATE + beta * EQUITY_RISK_PREMIUM
    total_capital = market_cap + max(total_debt, 0)
    equity_weight = market_cap / total_capital
    debt_weight = 1 - equity_weight
    wacc = equity_weight * cost_of_equity + debt_weight * COST_OF_DEBT * (1 - TAX_RATE)
    return wacc


def dcf_value_per_share(fundamentals: dict) -> float | None:
    fcf_history = fundamentals.get("fcf_history") or []
    if not fcf_history or fcf_history[0] is None or fcf_history[0] <= 0:
        return None

    growth = estimate_fcf_growth(fcf_history)
    if growth is None:
        growth = 0.03  # conservative fallback when history is too thin/noisy

    wacc = estimate_wacc(
        fundamentals.get("market_cap"),
        fundamentals.get("total_debt") or 0,
        fundamentals.get("beta"),
    )
    if wacc is None or wacc - TERMINAL_GROWTH < MIN_WACC_SPREAD:
        return None

    shares = fundamentals.get("shares_outstanding")
    if not shares or shares <= 0:
        return None

    base_fcf = fcf_history[0]
    projected = [base_fcf * (1 + growth) ** yr for yr in range(1, PROJECTION_YEARS + 1)]
    discounted = sum(fcf / (1 + wacc) ** yr for yr, fcf in enumerate(projected, start=1))

    terminal_value = projected[-1] * (1 + TERMINAL_GROWTH) / (wacc - TERMINAL_GROWTH)
    discounted_terminal = terminal_value / (1 + wacc) ** PROJECTION_YEARS

    enterprise_value = discounted + discounted_terminal
    equity_value = enterprise_value - fundamentals.get("net_debt", 0)
    if equity_value <= 0:
        return None

    return equity_value / shares
