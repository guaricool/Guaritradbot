"""
Sprint 39 — Strategy correlation & portfolio builder.

Borrowed from StrategyQuant's portfolio builder. When you have N
candidate strategies, blindly running all of them is wasteful if many
of them are correlated (e.g. all "buy SPY on RSI oversold"). The
right portfolio maximizes return per unit of correlation.

We compute:
  1. **Pairwise correlation matrix** of per-trade returns across strategies
  2. **Average correlation** (a portfolio with avg_corr < 0.3 is well-diversified)
  3. **Greedy uncorrelated portfolio builder** — pick strategies that add
     diversity (lowest correlation to already-selected) until N are chosen
     or a target correlation threshold is hit.

Why it matters: with a $20-$200 account, the bot can only run a handful
of strategies. Choosing 5 uncorrelated strategies > 5 correlated ones.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence

import numpy as np


@dataclass
class StrategyReturns:
    """Container: a strategy's name and its per-trade returns."""
    name: str
    returns: Sequence[float]
    # Optional metadata for ranking
    sharpe: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    n_trades: int = 0


@dataclass
class CorrelationResult:
    """Output of a correlation analysis."""
    strategies: List[str]
    matrix: List[List[float]]     # square matrix, row/col aligned with strategies
    avg_correlation: float
    well_diversified: bool       # avg_correlation < 0.3
    recommended_portfolio: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "strategies": self.strategies,
            "matrix": self.matrix,
            "avg_correlation": self.avg_correlation,
            "well_diversified": self.well_diversified,
            "recommended_portfolio": self.recommended_portfolio,
        }


def _align_returns(strategies: List[StrategyReturns]) -> np.ndarray:
    """Build a (n_strategies, max_len) matrix, padding with NaN.

    NaN padding lets us compute correlation only on overlapping trades.
    """
    n = len(strategies)
    if n == 0:
        return np.zeros((0, 0))
    max_len = max(len(s.returns) for s in strategies)
    out = np.full((n, max_len), np.nan)
    for i, s in enumerate(strategies):
        for j, r in enumerate(s.returns):
            out[i, j] = float(r)
    return out


def compute_correlation_matrix(strategies: List[StrategyReturns]) -> np.ndarray:
    """Compute pairwise Pearson correlation between strategies' per-trade returns.

    Only considers indices where BOTH strategies have non-NaN values
    (i.e. overlapping trades). If two strategies have zero overlap, the
    correlation is set to 0 (undefined → neutral).
    """
    if len(strategies) < 2:
        n = len(strategies)
        return np.eye(n) if n > 0 else np.zeros((0, 0))
    aligned = _align_returns(strategies)
    n = aligned.shape[0]
    corr = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            row_i = aligned[i]
            row_j = aligned[j]
            # Mask: both non-NaN
            mask = ~np.isnan(row_i) & ~np.isnan(row_j)
            n_overlap = int(np.sum(mask))
            if n_overlap < 3:
                # Not enough overlap to be statistically meaningful.
                val = 0.0
            else:
                a = row_i[mask]
                b = row_j[mask]
                if np.std(a) == 0 or np.std(b) == 0:
                    val = 0.0
                else:
                    val = float(np.corrcoef(a, b)[0, 1])
                    # Clamp to [-1, 1] in case of numerical drift
                    val = max(-1.0, min(1.0, val))
            corr[i, j] = val
            corr[j, i] = val
    return corr


def average_correlation(corr: np.ndarray) -> float:
    """Average of the off-diagonal elements (a measure of overall similarity)."""
    n = corr.shape[0]
    if n < 2:
        return 0.0
    # Sum of upper triangle (excluding diagonal), divided by #pairs.
    iu = np.triu_indices(n, k=1)
    vals = corr[iu]
    return float(np.mean(vals))


def build_uncorrelated_portfolio(
    strategies: List[StrategyReturns],
    max_n: int = 5,
    target_avg_corr: float = 0.4,
    ranking_metric: str = "sharpe",
) -> List[str]:
    """Greedy: pick the best strategy first, then keep adding strategies
    that maximize portfolio diversity (lowest correlation to already-selected).

    Stops when ``max_n`` reached OR adding any remaining strategy would
    push the average correlation above ``target_avg_corr``.

    Args:
        strategies: candidate strategies (each with a `name`).
        max_n: maximum portfolio size.
        target_avg_corr: don't add strategies that would push avg above this.
        ranking_metric: which metric ranks the first pick — "sharpe",
            "total_return", or "n_trades" (default "sharpe").

    Returns:
        List of strategy names forming the recommended portfolio (in
        selection order). May be empty if no candidates.
    """
    if not strategies:
        return []
    # First pick: best by ranking metric.
    if ranking_metric == "total_return":
        first_idx = int(np.argmax([s.total_return for s in strategies]))
    elif ranking_metric == "n_trades":
        first_idx = int(np.argmax([s.n_trades for s in strategies]))
    else:  # default sharpe
        first_idx = int(np.argmax([s.sharpe for s in strategies]))
    selected = [first_idx]
    remaining = set(range(len(strategies))) - {first_idx}
    while len(selected) < max_n and remaining:
        # Find the strategy that minimizes correlation to current selection.
        best_candidate = None
        best_avg_corr = float("inf")
        for cand in remaining:
            # Average correlation of `cand` with currently selected.
            cand_corrs = []
            for sel in selected:
                # We don't have the corr matrix yet — compute inline.
                aligned = _align_returns([strategies[cand], strategies[sel]])
                if aligned.shape[1] < 3:
                    cand_corrs.append(0.0)
                    continue
                mask = ~np.isnan(aligned[0]) & ~np.isnan(aligned[1])
                if np.sum(mask) < 3:
                    cand_corrs.append(0.0)
                    continue
                a = aligned[0][mask]
                b = aligned[1][mask]
                if np.std(a) == 0 or np.std(b) == 0:
                    cand_corrs.append(0.0)
                else:
                    cand_corrs.append(float(np.corrcoef(a, b)[0, 1]))
            avg = float(np.mean(cand_corrs)) if cand_corrs else 0.0
            if avg < best_avg_corr:
                best_avg_corr = avg
                best_candidate = cand
        if best_candidate is None or best_avg_corr > target_avg_corr:
            break
        selected.append(best_candidate)
        remaining.discard(best_candidate)
    return [strategies[i].name for i in selected]


def analyze_strategies(
    strategies: List[StrategyReturns],
    max_portfolio: int = 5,
    target_avg_corr: float = 0.4,
) -> CorrelationResult:
    """One-shot: compute matrix + avg correlation + recommended portfolio."""
    names = [s.name for s in strategies]
    if len(strategies) < 2:
        return CorrelationResult(
            strategies=names,
            matrix=[[1.0]] if strategies else [],
            avg_correlation=0.0,
            well_diversified=len(strategies) <= 1,
            recommended_portfolio=names[:max_portfolio],
        )
    corr = compute_correlation_matrix(strategies)
    avg = average_correlation(corr)
    portfolio = build_uncorrelated_portfolio(
        strategies, max_n=max_portfolio, target_avg_corr=target_avg_corr,
    )
    return CorrelationResult(
        strategies=names,
        matrix=corr.tolist(),
        avg_correlation=avg,
        well_diversified=avg < 0.3,
        recommended_portfolio=portfolio,
    )
