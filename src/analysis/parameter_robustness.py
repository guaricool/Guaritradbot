"""
Sprint 40 — Parameter Robustness / Permutation Test.

Borrowed from StrategyQuant's "System Parameter Permutation" (SPP)
methodology. The idea: a real-edge strategy should be robust to small
parameter changes. If you shift RSI_oversold from 30 to 28, the
strategy should still make money. If small perturbations destroy the
P&L, the strategy is curve-fit.

Algorithm:
  1. Define base parameters (e.g. ``{"rsi_oversold": 30, "rsi_overbought": 70}``)
  2. Define a perturbation range per parameter (e.g. ±20%)
  3. For each of N permutations, randomly perturb each parameter
     and run the strategy on the price data
  4. Compute a robustness score: % of permutations where the
     strategy remained profitable
  5. Optional: also compare median P&L of permutations to base P&L

Heuristic interpretation:
  - >70% permutations profitable: robust (real edge)
  - 40-70%: marginal (use with caution, retest often)
  - <40%: fragile (overfit, don't trust out-of-sample)

The user provides a ``strategy_func`` that takes (df, **params) and
returns a Series of positions (1 = long, -1 = short, 0 = flat). We
backtest the resulting equity curve with the existing
VectorizedBacktester.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.optimization.backtester import VectorizedBacktester


@dataclass
class PermutationResult:
    """Output of a parameter permutation test."""
    base_params: Dict[str, float]
    n_permutations: int
    n_profitable: int
    n_unprofitable: int
    pct_profitable: float          # robustness score, 0..1
    base_total_return: float
    base_sharpe: float
    perm_total_returns: List[float]  # for histogram
    perm_sharpes: List[float]
    perm_p5_total_return: float
    perm_p50_total_return: float
    perm_p95_total_return: float
    perm_p5_sharpe: float
    perm_p50_sharpe: float
    perm_p95_sharpe: float
    robustness_label: str           # "robust" / "marginal" / "fragile"

    def to_dict(self) -> dict:
        return asdict(self)


def _perturb_params(
    base: Dict[str, float],
    ranges: Dict[str, Tuple[float, float]],
    rng: np.random.Generator,
) -> Dict[str, float]:
    """Apply multiplicative perturbation per param.

    For each key in ``base`` that's also in ``ranges``, draw a uniform
    multiplier in ``[1+low, 1+high]`` and multiply the base value. E.g.
    base rsi_oversold=30, range (-0.2, +0.2) → uniform in [24, 36].
    """
    out = dict(base)
    for k, (low, high) in ranges.items():
        if k not in out:
            continue
        try:
            v = float(out[k])
            mult = rng.uniform(1.0 + low, 1.0 + high)
            out[k] = v * mult
        except (TypeError, ValueError):
            # If base value can't be coerced to float (e.g. enum), skip.
            continue
    return out


def permutation_test(
    prices: pd.DataFrame,
    strategy_func: Callable[[pd.DataFrame, Any], pd.Series],
    base_params: Dict[str, Any],
    param_ranges: Optional[Dict[str, Tuple[float, float]]] = None,
    n_permutations: int = 100,
    perturbation_pct: float = 0.20,
    initial_capital: float = 10000.0,
    periods_per_year: int = 365,
    seed: Optional[int] = None,
) -> PermutationResult:
    """Run a parameter permutation test for robustness.

    Args:
        prices: OHLCV dataframe with at least a "Close" column.
        strategy_func: callable(df, **params) → Series of positions
            (1, 0, -1). Same interface as the existing backtester
            signal_func (e.g. ``StrategyAgent.generate_vectorized_signals``).
        base_params: baseline parameters (e.g. ``{"rsi_oversold": 30}``).
        param_ranges: dict mapping param name → (low_pct, high_pct).
            If a param is in base but NOT in this dict, it's held fixed.
            If None, defaults to ±``perturbation_pct`` for all numeric
            base params.
        n_permutations: how many perturbed variants to test.
        perturbation_pct: default ±pct for any base param not in
            ``param_ranges`` (0.20 = ±20%).
        initial_capital: starting capital for each backtest run.
        periods_per_year: for Sharpe annualization.
        seed: random seed (None = non-deterministic).

    Returns:
        PermutationResult with the distribution and a robustness label.
    """
    rng = np.random.default_rng(seed)
    backtester = VectorizedBacktester(
        initial_capital=initial_capital,
        periods_per_year=periods_per_year,
    )
    # Build the effective param_ranges (default ±20% for any numeric base param).
    if param_ranges is None:
        param_ranges = {}
    effective_ranges: Dict[str, Tuple[float, float]] = {}
    for k, v in base_params.items():
        if k in param_ranges:
            effective_ranges[k] = param_ranges[k]
        else:
            try:
                float(v)
                effective_ranges[k] = (-perturbation_pct, perturbation_pct)
            except (TypeError, ValueError):
                # Non-numeric param (e.g. enum string): don't perturb.
                continue
    # Base run.
    base_signal = strategy_func(prices, **base_params)
    base_result = backtester.run(prices, lambda df: base_signal)
    base_metrics = base_result["metrics"]
    base_total_return = float(base_metrics["total_return"])
    base_sharpe = float(base_metrics["sharpe_ratio"])
    # Permutation runs.
    perm_returns: List[float] = []
    perm_sharpes: List[float] = []
    for i in range(n_permutations):
        new_params = _perturb_params(base_params, effective_ranges, rng)
        try:
            perm_signal = strategy_func(prices, **new_params)
            perm_result = backtester.run(prices, lambda df: perm_signal)
            pm = perm_result["metrics"]
            perm_returns.append(float(pm["total_return"]))
            perm_sharpes.append(float(pm["sharpe_ratio"]))
        except Exception:
            # Bad perturbation (e.g. zero-period param). Skip.
            continue
    perm_returns_arr = np.array(perm_returns, dtype=float) if perm_returns else np.array([0.0])
    perm_sharpes_arr = np.array(perm_sharpes, dtype=float) if perm_sharpes else np.array([0.0])
    n_profit = int(np.sum(perm_returns_arr > 0))
    n_total = len(perm_returns_arr)
    pct_profit = float(n_profit / n_total) if n_total > 0 else 0.0
    if pct_profit > 0.70:
        label = "robust"
    elif pct_profit > 0.40:
        label = "marginal"
    else:
        label = "fragile"
    return PermutationResult(
        base_params=dict(base_params),
        n_permutations=n_total,
        n_profitable=n_profit,
        n_unprofitable=n_total - n_profit,
        pct_profitable=pct_profit,
        base_total_return=base_total_return,
        base_sharpe=base_sharpe,
        perm_total_returns=[round(float(x), 6) for x in perm_returns_arr.tolist()],
        perm_sharpes=[round(float(x), 6) for x in perm_sharpes_arr.tolist()],
        perm_p5_total_return=float(np.percentile(perm_returns_arr, 5)),
        perm_p50_total_return=float(np.percentile(perm_returns_arr, 50)),
        perm_p95_total_return=float(np.percentile(perm_returns_arr, 95)),
        perm_p5_sharpe=float(np.percentile(perm_sharpes_arr, 5)),
        perm_p50_sharpe=float(np.percentile(perm_sharpes_arr, 50)),
        perm_p95_sharpe=float(np.percentile(perm_sharpes_arr, 95)),
        robustness_label=label,
    )
