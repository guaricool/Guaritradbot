"""
Sprint 44B — Portfolio-level tail risk: CVaR (Expected Shortfall).

`sisk_metrics.py` already has per-strategy ratios + Monte Carlo. THIS
module adds portfolio-level tail risk — the metric that answers
"if things go bad, how bad do they go on average?".

Two related but distinct concepts
----------------------------------
  - **Value at Risk (VaR)**: the threshold loss such that there's
    only X% probability of losing more than that. VaR 95% = -3% means
    "5% of the time we lose more than 3%".

  - **Conditional Value at Risk (CVaR / Expected Shortfall)**: the
    EXPECTED loss given that we're already in the worst X% of cases.
    CVaR 95% is always ≥ VaR 95% (it's the average of the worst 5%).
    CVaR is what regulators and risk managers prefer because VaR
    ignores the shape of the tail (a -100% blow-up still has the
    same VaR 99% as a -10% drop, but very different CVaR).

Why this is separate from risk_metrics.py
-----------------------------------------
`risk_metrics.py` operates on per-strategy trade returns. CVaR here
operates on per-asset daily returns aggregated at the PORTFOLIO
level, weighted by current exposure. Different domain, different
metric. They complement each other.

Sprint 44B Tier 2 (BlackRock #4, Bridgewater #8).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from src.data.yf_safe import safe_yf_download
from src.analysis.asset_correlation import fetch_returns


# ----------------------------------------------------------------------
# Result type
# ----------------------------------------------------------------------

@dataclass
class TailRiskResult:
    """Portfolio-level tail risk summary."""
    assets: List[str]
    weights: List[float]                # notional_weight per asset, sums to 1
    n_observations: int                 # aligned daily returns
    var_95: float                       # VaR at 95% confidence (negative)
    var_99: float                       # VaR at 99% confidence (negative)
    cvar_95: float                      # CVaR / Expected Shortfall @ 95% (negative)
    cvar_99: float                      # CVaR / Expected Shortfall @ 99% (negative)
    mean_daily_return: float
    std_daily_return: float
    annual_volatility: float
    worst_single_day: float             # most negative simulated day

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------
# Core math
# ----------------------------------------------------------------------

def _portfolio_returns(
    per_asset_returns: Dict[str, pd.Series],
    weights: Dict[str, float],
) -> pd.Series:
    """Build a portfolio return series from per-asset returns and weights.

    Returns are aligned on the common date index. Weights are normalized
    to sum to 1 across the supplied assets (callers may pass raw
    notionals — we normalize).

    Date alignment matters: we use intersection, not union, so we don't
    fabricate 0 returns for assets that didn't trade on a given day
    (which would artificially deflate the portfolio's variance).
    """
    if not per_asset_returns:
        return pd.Series(dtype=float)
    # Filter weights to assets we actually have returns for.
    actual = {s: w for s, w in weights.items() if s in per_asset_returns and w > 0}
    if not actual:
        return pd.Series(dtype=float)
    total = sum(actual.values())
    if total <= 0:
        return pd.Series(dtype=float)
    norm = {s: w / total for s, w in actual.items()}
    # Common date index.
    common: Optional[pd.DatetimeIndex] = None
    for ser in per_asset_returns.values():
        common = ser.index if common is None else common.intersection(ser.index)
    if common is None or len(common) == 0:
        return pd.Series(dtype=float)
    common = sorted(common)
    weighted = pd.Series(0.0, index=common)
    for sym, w in norm.items():
        aligned = per_asset_returns[sym].reindex(common).fillna(0.0)
        weighted = weighted.add(aligned * w, fill_value=0.0)
    return weighted


def value_at_risk(returns: Sequence[float], confidence: float = 0.95) -> float:
    """Historical VaR at the given confidence level.

    Returns a NEGATIVE number representing the loss threshold. Example:
    VaR 95% = -0.03 means "in the worst 5% of days, we lose more than 3%".

    Uses the empirical quantile (no parametric assumption).
    """
    r = np.asarray(returns, dtype=float)
    if len(r) == 0:
        return 0.0
    # 95% VaR = 5th percentile of the return distribution (negative tail).
    q = 1.0 - confidence
    return float(np.percentile(r, q * 100.0))


def conditional_value_at_risk(
    returns: Sequence[float],
    confidence: float = 0.95,
) -> float:
    """Historical CVaR (Expected Shortfall) at the given confidence level.

    Returns a NEGATIVE number representing the AVERAGE loss in the worst
    (1 - confidence) fraction of cases. Always ≤ VaR (more negative or
    equal) because it's the mean of the tail, not the threshold.

    CVaR is the right metric for "expected damage in a bad scenario"
    because it captures the SHAPE of the tail, not just the boundary.
    """
    r = np.asarray(returns, dtype=float)
    if len(r) == 0:
        return 0.0
    q = 1.0 - confidence
    threshold = np.percentile(r, q * 100.0)
    tail = r[r <= threshold]
    if len(tail) == 0:
        return float(threshold)
    return float(np.mean(tail))


# ----------------------------------------------------------------------
# One-shot API
# ----------------------------------------------------------------------

def compute_portfolio_tail_risk(
    asset_weights: Dict[str, float],
    window_days: int = 180,
    interval: str = "1d",
) -> TailRiskResult:
    """Compute portfolio tail risk for the given asset weights.

    Args:
        asset_weights: dict of {symbol: weight}. Weights are interpreted
            as notional proportions (e.g. {"BTC-USD": 60, "SPY": 40}
            means 60% BTC / 40% SPY). The bot's Position objects expose
            .notional_usd; the caller converts to a weight dict.
        window_days: lookback for returns (default 180d = 6 months).
        interval: yfinance interval.

    Returns:
        TailRiskResult with VaR/CVaR at 95% and 99% plus the
        distribution summary. If no data can be fetched, returns a
        zeroed result (no signal).
    """
    symbols = list(asset_weights.keys())
    if not symbols:
        return TailRiskResult(
            assets=[], weights=[], n_observations=0,
            var_95=0.0, var_99=0.0, cvar_95=0.0, cvar_99=0.0,
            mean_daily_return=0.0, std_daily_return=0.0,
            annual_volatility=0.0, worst_single_day=0.0,
        )
    rets = fetch_returns(symbols, window_days=window_days, interval=interval)
    if not rets:
        return TailRiskResult(
            assets=symbols, weights=[asset_weights[s] for s in symbols],
            n_observations=0,
            var_95=0.0, var_99=0.0, cvar_95=0.0, cvar_99=0.0,
            mean_daily_return=0.0, std_daily_return=0.0,
            annual_volatility=0.0, worst_single_day=0.0,
        )
    # Drop weights for assets we couldn't fetch (proportional to the rest).
    actual_weights = {s: asset_weights[s] for s in rets.keys() if s in asset_weights}
    port_ret = _portfolio_returns(rets, actual_weights)
    if port_ret.empty or len(port_ret) < 5:
        return TailRiskResult(
            assets=list(rets.keys()),
            weights=[actual_weights.get(s, 0.0) for s in rets.keys()],
            n_observations=len(port_ret),
            var_95=0.0, var_99=0.0, cvar_95=0.0, cvar_99=0.0,
            mean_daily_return=0.0, std_daily_return=0.0,
            annual_volatility=0.0, worst_single_day=0.0,
        )
    vals = port_ret.values
    return TailRiskResult(
        assets=list(rets.keys()),
        weights=[actual_weights.get(s, 0.0) for s in rets.keys()],
        n_observations=len(vals),
        var_95=value_at_risk(vals, confidence=0.95),
        var_99=value_at_risk(vals, confidence=0.99),
        cvar_95=conditional_value_at_risk(vals, confidence=0.95),
        cvar_99=conditional_value_at_risk(vals, confidence=0.99),
        mean_daily_return=float(np.mean(vals)),
        std_daily_return=float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0,
        annual_volatility=float(np.std(vals, ddof=1) * math.sqrt(252)) if len(vals) > 1 else 0.0,
        worst_single_day=float(np.min(vals)),
    )


def cvar_summary_text(result: TailRiskResult) -> str:
    """Human-readable one-liner for the dashboard.

    Example: "Daily CVaR 95%: -2.3% | CVaR 99%: -4.1% | worst day: -7.5%".
    """
    if result.n_observations == 0:
        return "tail_risk: no_data"
    return (
        f"CVaR 95% {result.cvar_95 * 100:.2f}% | "
        f"CVaR 99% {result.cvar_99 * 100:.2f}% | "
        f"worst_day {result.worst_single_day * 100:.2f}%"
    )
