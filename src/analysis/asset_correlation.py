"""
Sprint 44A — Asset correlation matrix (vs. strategy correlation).

`strategy_correlation.py` correlates strategy returns. THIS module
correlates the UNDERLYING ASSETS (BTC, ETH, SOL, SPY, QQQ, GLD, USO)
so the bot can detect when its "diversified" portfolio is actually
3 correlated bets.

Concrete failure mode this prevents
-----------------------------------
With `max_open_trades=5`, the bot can naively fill all 5 slots with
BTC + ETH + SOL + SPY + QQQ. That's 5 positions but only 2-3
independent bets:
  - crypto bucket: BTC, ETH, SOL → historically 0.7-0.9 correlated
  - equity bucket: SPY, QQQ → ~0.9 correlated (both large cap)
So 5 slots of budget = 2 slots of real risk + 3 slots of illusion.

What this module does
---------------------
  1. Pairwise correlation matrix of daily returns per asset
  2. Average correlation across all pairs (with thresholds)
  3. Per-asset-class grouping (so risk_agent can check concentration)
  4. Convenience: `correlation_between(a, b)` for ad-hoc queries

Data flow
---------
  yfinance (via safe_yf_download) → pct_change → align on common
  dates → Pearson correlation. No external DB needed; the bot already
  caches yfinance responses in market_analyst for other use cases.

Sprint 44A Tier 1 (Bridgewater risk assessment, prompt 3).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.data.yf_safe import safe_yf_download
from src.data.asset_class import get_asset_class, AssetClass


# Threshold: avg correlation above this means "not really diversified".
# 0.5 = "more than half the moves are explained by shared factor".
# 0.7 = "basically the same bet".
DEFAULT_WELL_DIVERSIFIED_THRESHOLD = 0.5


@dataclass
class AssetCorrelationResult:
    """Result of an asset correlation analysis.

    `matrix` is a square correlation matrix, row/col aligned with `assets`.
    `avg_correlation` is the mean of off-diagonal elements.
    `well_diversified` is avg_correlation < threshold.
    `per_asset_class` groups the assets by their AssetClass — used by
        risk_agent to compute sector concentration.
    """
    assets: List[str]
    matrix: List[List[float]]
    avg_correlation: float
    well_diversified: bool
    window_days: int
    per_asset_class: Dict[str, List[str]] = field(default_factory=dict)
    threshold: float = DEFAULT_WELL_DIVERSIFIED_THRESHOLD

    def to_dict(self) -> dict:
        return {
            "assets": list(self.assets),
            "matrix": self.matrix,
            "avg_correlation": self.avg_correlation,
            "well_diversified": self.well_diversified,
            "window_days": self.window_days,
            "per_asset_class": dict(self.per_asset_class),
            "threshold": self.threshold,
        }


# ----------------------------------------------------------------------
# Data fetch
# ----------------------------------------------------------------------

def fetch_returns(
    symbols: Sequence[str],
    window_days: int = 90,
    interval: str = "1d",
) -> Dict[str, pd.Series]:
    """Fetch daily returns for a list of symbols via yfinance.

    Uses safe_yf_download (with curl_cffi + retries) per Sprint 9/43
    fixes. Symbols that fail to download are OMITTED from the result
    (not filled with zeros) to avoid spurious 0.0 correlations between
    "real" and "missing" data.

    Args:
        symbols: tickers to fetch (e.g. ["BTC-USD", "SPY"]).
        window_days: how many days of history (default 90d).
        interval: yfinance interval string ("1d" default).

    Returns:
        Dict {symbol: pd.Series[float]} of daily simple returns.
    """
    period = f"{window_days}d"
    out: Dict[str, pd.Series] = {}
    for sym in symbols:
        try:
            df = safe_yf_download(sym, period=period, interval=interval)
        except Exception:
            # safe_yf_download already returns None on failure, but a
            # raised exception (e.g. import-time error in yfinance) must
            # not break the loop.
            continue
        if df is None or df.empty:
            continue
        price_col = "Adj Close" if "Adj Close" in df.columns else "Close"
        if price_col not in df.columns:
            continue
        try:
            prices = df[price_col].astype(float).dropna()
        except Exception:
            continue
        if len(prices) < 5:
            continue
        ret = prices.pct_change().dropna()
        if ret.empty:
            continue
        out[sym] = ret
    return out


# ----------------------------------------------------------------------
# Alignment + correlation
# ----------------------------------------------------------------------

def _align_returns(
    returns: Dict[str, pd.Series],
) -> Tuple[List[str], np.ndarray]:
    """Align per-asset return series on their common dates.

    Returns (assets, matrix) where matrix is shape (n_assets, n_dates).
    Returns ([], np.zeros((0,0))) for empty input. If overlap is < 3
    dates, returns the asset list and an empty (n,0) matrix — caller
    can detect this and bail.
    """
    if not returns:
        return [], np.zeros((0, 0))
    common_idx: Optional[pd.DatetimeIndex] = None
    for ser in returns.values():
        idx = ser.index
        common_idx = idx if common_idx is None else common_idx.intersection(idx)
    if common_idx is None or len(common_idx) < 3:
        return list(returns.keys()), np.zeros((len(returns), 0))
    common_idx = sorted(common_idx)
    assets = list(returns.keys())
    matrix = np.zeros((len(assets), len(common_idx)))
    for i, sym in enumerate(assets):
        matrix[i, :] = returns[sym].reindex(common_idx).values
    return assets, matrix


def compute_asset_correlation_matrix(
    returns: Dict[str, pd.Series],
) -> np.ndarray:
    """Compute the pairwise Pearson correlation matrix of asset returns.

    Returns:
        Square matrix, shape (n_assets, n_assets). Diagonal is 1.0.
        If only one asset, returns identity matrix of size 1.
        If zero assets, returns empty 0x0.
        If < 3 overlapping dates, returns identity (no signal).
    """
    assets, mat = _align_returns(returns)
    n = len(assets)
    if n == 0:
        return np.zeros((0, 0))
    if n == 1:
        return np.eye(1)
    if mat.shape[1] < 3:
        return np.eye(n)
    corr = np.zeros((n, n))
    for i in range(n):
        for j in range(i, n):
            if i == j:
                corr[i, j] = 1.0
                continue
            a = mat[i]
            b = mat[j]
            sa = np.std(a)
            sb = np.std(b)
            if sa == 0 or sb == 0:
                c = 0.0
            else:
                c = float(np.corrcoef(a, b)[0, 1])
                if not math.isfinite(c):
                    c = 0.0
                c = max(-1.0, min(1.0, c))
            corr[i, j] = c
            corr[j, i] = c
    return corr


def average_correlation(corr: np.ndarray) -> float:
    """Average of off-diagonal elements (a measure of overall similarity)."""
    n = corr.shape[0]
    if n < 2:
        return 0.0
    iu = np.triu_indices(n, k=1)
    vals = corr[iu]
    if vals.size == 0:
        return 0.0
    return float(np.mean(vals))


def group_by_asset_class(symbols: Sequence[str]) -> Dict[str, List[str]]:
    """Group symbols by their AssetClass enum value.

    Unknown symbols fall into the CASH bucket (a separate group that
    doesn't trigger the concentration gate, by design).
    """
    out: Dict[str, List[str]] = {}
    for sym in symbols:
        cls = get_asset_class(sym).value
        out.setdefault(cls, []).append(sym)
    return out


# ----------------------------------------------------------------------
# One-shot API
# ----------------------------------------------------------------------

def analyze_assets(
    symbols: Sequence[str],
    window_days: int = 90,
    interval: str = "1d",
    threshold: float = DEFAULT_WELL_DIVERSIFIED_THRESHOLD,
) -> AssetCorrelationResult:
    """One-shot: fetch returns → align → correlation matrix → group.

    Args:
        symbols: tickers to analyze.
        window_days: lookback window (default 90 = ~3 months of trading days).
        interval: yfinance interval.
        threshold: avg correlation above this is "not diversified".

    Returns:
        AssetCorrelationResult with matrix, avg correlation, and asset
        class groups. If no symbols could be fetched, returns an empty
        result with well_diversified=True (no signal = no warning).
    """
    returns = fetch_returns(symbols, window_days=window_days, interval=interval)
    if not returns:
        return AssetCorrelationResult(
            assets=[],
            matrix=[],
            avg_correlation=0.0,
            well_diversified=True,
            window_days=window_days,
            per_asset_class=group_by_asset_class(symbols),
            threshold=threshold,
        )
    corr = compute_asset_correlation_matrix(returns)
    avg = average_correlation(corr)
    actual_assets = list(returns.keys())
    return AssetCorrelationResult(
        assets=actual_assets,
        matrix=corr.tolist(),
        avg_correlation=avg,
        well_diversified=avg < threshold,
        window_days=window_days,
        per_asset_class=group_by_asset_class(actual_assets),
        threshold=threshold,
    )


def correlation_between(
    returns: Dict[str, pd.Series],
    a: str,
    b: str,
) -> Optional[float]:
    """Pairwise correlation between two specific assets.

    Returns None if either is missing or not enough overlapping data.
    Returns 1.0 if a == b.
    """
    if a not in returns or b not in returns:
        return None
    if a == b:
        return 1.0
    sa = returns[a]
    sb = returns[b]
    common = sa.index.intersection(sb.index)
    if len(common) < 3:
        return None
    a_vals = sa.reindex(common).values
    b_vals = sb.reindex(common).values
    if np.std(a_vals) == 0 or np.std(b_vals) == 0:
        return 0.0
    c = float(np.corrcoef(a_vals, b_vals)[0, 1])
    if not math.isfinite(c):
        return None
    return max(-1.0, min(1.0, c))
