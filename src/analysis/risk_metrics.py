"""
Sprint 37 — Risk Metrics & Monte Carlo Simulation.

Borrowed from StrategyQuant (https://strategyquant.com/) — the same
rigorous robustness tools that make their strategies statistically
sound. We add:

  1. **Sharpe / Sortino / Calmar ratios** — used to rank strategies
     beyond simple Net profit. Sortino ignores upside vol (fairer
     for trend strategies). Calmar = return / max drawdown.

  2. **Monte Carlo simulation** — shuffle the order of trades
     N times to estimate the distribution of outcomes:
       - P&L distribution (mean, std, percentiles)
       - Probability of ruin (drawdown > X%)
       - Expected max drawdown
       - 5th / 50th / 95th percentile equity curves

  3. **Aggregate risk report** — combines the above into a single
     `RiskReport` object the dashboard can render.

References:
  - https://en.wikipedia.org/wiki/Sharpe_ratio
  - https://en.wikipedia.org/wiki/Sortino_ratio
  - https://en.wikipedia.org/wiki/Calmar_ratio
  - https://en.wikipedia.org/wiki/Monte_Carlo_methods_in_finance
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Sequence

import numpy as np


# ============================================================
# Risk Ratios
# ============================================================

def sharpe_ratio(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = 365,
) -> float:
    """Annualized Sharpe ratio.

    Args:
        returns: per-period simple returns (e.g. daily %).
        risk_free_rate: annualized risk-free rate (e.g. 0.04 for 4%).
        periods_per_year: how many periods per year (365 daily, 252 trading, etc).

    Returns:
        Sharpe ratio. 0 if std is 0 or input is too short.
    """
    r = np.asarray(returns, dtype=float)
    if len(r) < 2:
        return 0.0
    excess = r - (risk_free_rate / periods_per_year)
    std = float(np.std(excess, ddof=1))
    if std == 0:
        return 0.0
    mean_excess = float(np.mean(excess))
    return float((mean_excess / std) * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = 365,
) -> float:
    """Annualized Sortino ratio — like Sharpe but only penalizes downside vol.

    Returns 0 if no negative returns.
    """
    r = np.asarray(returns, dtype=float)
    if len(r) < 2:
        return 0.0
    excess = r - (risk_free_rate / periods_per_year)
    downside = excess[excess < 0]
    if len(downside) == 0:
        # No losing periods — perfect (or pathological) strategy.
        return float("inf") if np.mean(excess) > 0 else 0.0
    downside_dev = float(np.sqrt(np.mean(downside ** 2)))
    if downside_dev == 0:
        return 0.0
    return float((np.mean(excess) / downside_dev) * math.sqrt(periods_per_year))


def calmar_ratio(
    annual_return: float,
    max_drawdown: float,
) -> float:
    """Calmar = annual_return / |max_drawdown|.

    Higher = better recovery per unit of pain. 0 if max_drawdown == 0.
    """
    if max_drawdown == 0:
        return 0.0
    return float(annual_return / abs(max_drawdown))


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """Max drawdown as a negative number (e.g. -0.25 = 25% drawdown)."""
    eq = np.asarray(equity_curve, dtype=float)
    if len(eq) == 0:
        return 0.0
    peaks = np.maximum.accumulate(eq)
    drawdowns = (eq - peaks) / np.where(peaks == 0, 1.0, peaks)
    return float(np.min(drawdowns))


def compute_ratios(
    returns: Sequence[float],
    equity_curve: Sequence[float],
    periods_per_year: int = 365,
) -> dict:
    """Convenience: return all three ratios + max drawdown + annual return."""
    r = np.asarray(returns, dtype=float)
    if len(r) == 0:
        return {
            "sharpe": 0.0, "sortino": 0.0, "calmar": 0.0,
            "max_drawdown": 0.0, "annual_return": 0.0,
        }
    total_return = float(np.prod(1 + r) - 1) if len(r) > 0 else 0.0
    annual_return = float((1 + total_return) ** (periods_per_year / max(len(r), 1)) - 1)
    mdd = max_drawdown(equity_curve)
    return {
        "sharpe": sharpe_ratio(returns, periods_per_year=periods_per_year),
        "sortino": sortino_ratio(returns, periods_per_year=periods_per_year),
        "calmar": calmar_ratio(annual_return, mdd),
        "max_drawdown": mdd,
        "annual_return": annual_return,
    }


# ============================================================
# Monte Carlo
# ============================================================

@dataclass
class MonteCarloResult:
    """Aggregate output of a Monte Carlo simulation."""
    n_simulations: int
    n_trades: int
    final_equity_p5: float      # 5th percentile final equity
    final_equity_p50: float     # median
    final_equity_p95: float    # 95th percentile
    final_equity_mean: float
    final_equity_std: float
    prob_profit: float          # % of sims that ended profitable
    prob_ruin_50pct: float      # % of sims that hit 50% drawdown
    prob_ruin_75pct: float      # % of sims that hit 75% drawdown
    max_drawdown_p5: float      # 5th percentile of worst drawdown
    max_drawdown_p50: float     # median worst drawdown
    max_drawdown_p95: float     # 95th percentile of worst drawdown
    sample_curves: List[List[float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def monte_carlo_simulation(
    trade_returns: Sequence[float],
    starting_equity: float = 1.0,
    n_simulations: int = 1000,
    ruin_threshold: float = 0.5,
    seed: Optional[int] = None,
    n_sample_curves: int = 50,
) -> MonteCarloResult:
    """Run Monte Carlo on a sequence of per-trade returns.

    Args:
        trade_returns: list of per-trade fractional returns (e.g. 0.02 = +2%,
            -0.01 = -1%). The order in the original list is IGNORED — we
            shuffle randomly to test whether the strategy's edge depends
            on a specific trade sequence.
        starting_equity: initial equity (default 1.0 for normalization).
        n_simulations: how many random orderings to run.
        ruin_threshold: drawdown level considered "ruin" (default 0.5 = 50%).
        seed: random seed for reproducibility (None = non-deterministic).
        n_sample_curves: how many equity curves to keep in the result for
            visualization. The rest are discarded to save memory.

    Returns:
        MonteCarloResult with the distribution statistics.
    """
    if len(trade_returns) < 2:
        # Not enough trades to do anything meaningful.
        return MonteCarloResult(
            n_simulations=0, n_trades=len(trade_returns),
            final_equity_p5=starting_equity, final_equity_p50=starting_equity,
            final_equity_p95=starting_equity, final_equity_mean=starting_equity,
            final_equity_std=0.0, prob_profit=0.0,
            prob_ruin_50pct=0.0, prob_ruin_75pct=0.0,
            max_drawdown_p5=0.0, max_drawdown_p50=0.0, max_drawdown_p95=0.0,
        )
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)
    final_equities = np.empty(n_simulations, dtype=float)
    max_drawdowns = np.empty(n_simulations, dtype=float)
    ruin_50 = 0
    ruin_75 = 0
    sample_curves: List[List[float]] = []
    returns_arr = np.asarray(trade_returns, dtype=float)
    for i in range(n_simulations):
        shuffled = returns_arr.copy()
        np.random.shuffle(shuffled)
        # Build equity curve
        eq = np.empty(len(shuffled) + 1, dtype=float)
        eq[0] = starting_equity
        # Vectorized: cumulative product of (1 + r)
        eq[1:] = starting_equity * np.cumprod(1.0 + shuffled)
        final_equities[i] = float(eq[-1])
        # Max drawdown of this path
        peaks = np.maximum.accumulate(eq)
        dd = (eq - peaks) / np.where(peaks == 0, 1.0, peaks)
        mdd = float(np.min(dd))
        max_drawdowns[i] = mdd
        if mdd <= -ruin_threshold:
            ruin_50 += 1
        if mdd <= -0.75:
            ruin_75 += 1
        # Keep a few sample curves for visualization
        if i < n_sample_curves:
            sample_curves.append([round(x, 6) for x in eq.tolist()])
    return MonteCarloResult(
        n_simulations=n_simulations,
        n_trades=len(trade_returns),
        final_equity_p5=float(np.percentile(final_equities, 5)),
        final_equity_p50=float(np.percentile(final_equities, 50)),
        final_equity_p95=float(np.percentile(final_equities, 95)),
        final_equity_mean=float(np.mean(final_equities)),
        final_equity_std=float(np.std(final_equities)),
        prob_profit=float(np.mean(final_equities > starting_equity)),
        prob_ruin_50pct=float(ruin_50 / n_simulations),
        prob_ruin_75pct=float(ruin_75 / n_simulations),
        max_drawdown_p5=float(np.percentile(max_drawdowns, 5)),
        max_drawdown_p50=float(np.percentile(max_drawdowns, 50)),
        max_drawdown_p95=float(np.percentile(max_drawdowns, 95)),
        sample_curves=sample_curves,
    )


# ============================================================
# Aggregate report
# ============================================================

@dataclass
class RiskReport:
    """Combined risk report — what the dashboard renders per strategy."""
    n_trades: int
    n_simulations: int
    total_return: float
    annual_return: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    # Monte Carlo
    mc_final_equity_p5: float
    mc_final_equity_p50: float
    mc_final_equity_p95: float
    mc_prob_profit: float
    mc_prob_ruin_50pct: float
    mc_prob_ruin_75pct: float
    mc_max_dd_p50: float

    def to_dict(self) -> dict:
        return asdict(self)

    def robustness_label(self) -> str:
        """One-line label: 'robust' / 'marginal' / 'fragile'."""
        if self.mc_prob_ruin_50pct > 0.10:
            return "fragile"
        if self.mc_prob_ruin_50pct > 0.02 or self.sharpe < 0.5:
            return "marginal"
        return "robust"


def build_risk_report(
    trade_returns: Sequence[float],
    equity_curve: Optional[Sequence[float]] = None,
    starting_equity: float = 1.0,
    n_simulations: int = 1000,
    periods_per_year: int = 365,
    seed: Optional[int] = None,
) -> RiskReport:
    """Build a combined risk report: ratios + Monte Carlo in one call."""
    if equity_curve is None:
        # Build equity curve from trade returns (1.0 → ...).
        eq = [starting_equity]
        for r in trade_returns:
            eq.append(eq[-1] * (1.0 + float(r)))
        equity_curve = eq
    ratios = compute_ratios(trade_returns, equity_curve, periods_per_year)
    mc = monte_carlo_simulation(
        trade_returns, starting_equity=starting_equity,
        n_simulations=n_simulations, seed=seed,
    )
    return RiskReport(
        n_trades=len(trade_returns),
        n_simulations=n_simulations,
        total_return=float((equity_curve[-1] / equity_curve[0]) - 1) if equity_curve else 0.0,
        annual_return=ratios["annual_return"],
        sharpe=ratios["sharpe"],
        sortino=ratios["sortino"],
        calmar=ratios["calmar"],
        max_drawdown=ratios["max_drawdown"],
        mc_final_equity_p5=mc.final_equity_p5,
        mc_final_equity_p50=mc.final_equity_p50,
        mc_final_equity_p95=mc.final_equity_p95,
        mc_prob_profit=mc.prob_profit,
        mc_prob_ruin_50pct=mc.prob_ruin_50pct,
        mc_prob_ruin_75pct=mc.prob_ruin_75pct,
        mc_max_dd_p50=mc.max_drawdown_p50,
    )
