"""
Sprint 46G — Qlib-inspired alpha factors + signal-quality (IC) evaluation.

Carlos asked for a deep dive into Microsoft's Qlib
(https://github.com/microsoft/qlib) to find concrete things worth
porting into guaritradbot. Two things stood out as directly portable
without needing Qlib's infrastructure (point-in-time DB, MLflow
experiment tracking, RL execution — none of that applies to a
single-process multi-agent bot like this one):

1. **Alpha158's factor formulas** (`qlib/contrib/data/loader.py`) — ~30
   declarative technical factors, each a simple, scale-free (price- or
   volume-normalized) rolling computation. This module ports a curated
   subset — the ones computable from the OHLCV columns
   MarketAnalystAgent already produces (Open/High/Low/Close/Volume) —
   using the EXACT formulas from Qlib's source, just renamed to match
   this codebase's column conventions.

2. **IC / Rank-IC** (`qlib/contrib/eva/alpha.py::calc_ic`) — Qlib's
   standard way to score a signal's raw predictive edge, INDEPENDENT of
   exit mechanics (stop-loss/take-profit choices). Qlib computes this
   cross-sectionally (per date, across many instruments in a panel).
   guaritradbot doesn't have that multi-asset panel — StrategyAgent and
   the GP evolve strategies per-symbol, one time series at a time — so
   `calc_ic` here is a time-series analogue: correlation between a
   signal and the forward return, pooled across the whole series for
   one symbol. It answers the same underlying question ("does a higher
   signal value predict a higher forward return?") without needing
   Qlib's panel infrastructure.

Nothing in this module is wired into any existing hot path by itself —
see `strategy_agent.py`'s new Alpha-Factor hypothesis block and
`genetic_programming.py`'s `run_evolution_cli` IC reporting for the
call sites.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

_EPS = 1e-12


# ============================================================
# KBAR — candle-shape ratios (Alpha158 "kbar" group)
# All scale-free: normalized by Open (or High-Low range) so they're
# comparable across assets/price levels.
# ============================================================

def kbar_kmid(df: pd.DataFrame) -> pd.Series:
    """(Close-Open)/Open — body size & direction of the candle."""
    return (df["Close"] - df["Open"]) / df["Open"].replace(0.0, np.nan)


def kbar_klen(df: pd.DataFrame) -> pd.Series:
    """(High-Low)/Open — total candle range."""
    return (df["High"] - df["Low"]) / df["Open"].replace(0.0, np.nan)


def kbar_kup(df: pd.DataFrame) -> pd.Series:
    """(High - max(Open,Close))/Open — upper wick length (rejection of highs)."""
    upper_body = pd.concat([df["Open"], df["Close"]], axis=1).max(axis=1)
    return (df["High"] - upper_body) / df["Open"].replace(0.0, np.nan)


def kbar_klow(df: pd.DataFrame) -> pd.Series:
    """(min(Open,Close) - Low)/Open — lower wick length (rejection of lows)."""
    lower_body = pd.concat([df["Open"], df["Close"]], axis=1).min(axis=1)
    return (lower_body - df["Low"]) / df["Open"].replace(0.0, np.nan)


def kbar_ksft(df: pd.DataFrame) -> pd.Series:
    """(2*Close-High-Low)/Open — where the close settled within the bar's range."""
    return (2 * df["Close"] - df["High"] - df["Low"]) / df["Open"].replace(0.0, np.nan)


# ============================================================
# Rolling price factors (Alpha158 "rolling" group, close-only subset)
# ============================================================

def roc(close: pd.Series, window: int) -> pd.Series:
    """Rate of change: Ref(close, window)/close. ~1.0 = flat;
    >1.0 = price was higher `window` bars ago (downtrend since)."""
    return close.shift(window) / close.replace(0.0, np.nan)


def ma_ratio(close: pd.Series, window: int) -> pd.Series:
    """Mean(close, window)/close — trend-following moving-average ratio."""
    return close.rolling(window).mean() / close.replace(0.0, np.nan)


def std_ratio(close: pd.Series, window: int) -> pd.Series:
    """Std(close, window)/close — scale-free realized volatility."""
    return close.rolling(window).std() / close.replace(0.0, np.nan)


def rsv(df: pd.DataFrame, window: int) -> pd.Series:
    """(Close - Min(Low, window)) / (Max(High, window) - Min(Low, window)).
    Price position within its recent range — 0 = at the low, 1 = at the
    high. Same idea as %K in Stochastic, but over High/Low instead of
    Close-only min/max."""
    hi = df["High"].rolling(window).max()
    lo = df["Low"].rolling(window).min()
    return (df["Close"] - lo) / (hi - lo + _EPS)


def cntd(close: pd.Series, window: int) -> pd.Series:
    """Mean(close>prev, window) - Mean(close<prev, window): momentum
    breadth — the fraction of up-days minus the fraction of down-days
    over the window. +1 = every bar was an up-bar, -1 = every bar down."""
    up = (close > close.shift(1)).astype(float)
    down = (close < close.shift(1)).astype(float)
    return up.rolling(window).mean() - down.rolling(window).mean()


# ============================================================
# Volume factors (Alpha158 "volume"/"rolling" groups)
# ============================================================

def vma_ratio(volume: pd.Series, window: int) -> pd.Series:
    """Mean(volume, window)/volume — volume relative to its recent average."""
    return volume.rolling(window).mean() / (volume + _EPS)


def vstd_ratio(volume: pd.Series, window: int) -> pd.Series:
    """Std(volume, window)/volume — volume volatility, scale-free."""
    return volume.rolling(window).std() / (volume + _EPS)


def wvma(df: pd.DataFrame, window: int) -> pd.Series:
    """Volume-weighted price-change volatility:
    Std(|ret|*volume, window) / (Mean(|ret|*volume, window) + eps).
    High WVMA = big, volume-confirmed moves are unusually erratic
    relative to typical volume-weighted moves (can flag exhaustion/
    capitulation-type conditions)."""
    ret = (df["Close"] / df["Close"].shift(1) - 1).abs()
    weighted = ret * df["Volume"]
    return weighted.rolling(window).std() / (weighted.rolling(window).mean() + _EPS)


def corr_price_volume(df: pd.DataFrame, window: int) -> pd.Series:
    """Corr(Close, log(Volume+1), window) — do price and volume move
    together? Positive = rallies are volume-confirmed; negative can
    flag low-conviction moves."""
    log_vol = np.log(df["Volume"] + 1.0)
    return df["Close"].rolling(window).corr(log_vol)


# ============================================================
# Aggregate helpers
# ============================================================

_DEFAULT_WINDOWS: Tuple[int, ...] = (5, 10, 20)


def compute_alpha_factors(
    df: pd.DataFrame,
    windows: Tuple[int, ...] = _DEFAULT_WINDOWS,
) -> Dict[str, pd.Series]:
    """Compute the full curated Alpha158-style factor set for one
    symbol's OHLCV DataFrame. Requires Open/High/Low/Close/Volume
    columns (MarketAnalystAgent's raw fetch always has these).

    Returns a flat dict of {factor_name: pd.Series}, index-aligned to
    `df`. Callers typically only need the LAST value per factor — see
    `latest_alpha_snapshot` below for that shortcut.
    """
    out: Dict[str, pd.Series] = {}
    required = {"Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(set(df.columns)):
        return out

    out["KMID"] = kbar_kmid(df)
    out["KLEN"] = kbar_klen(df)
    out["KUP"] = kbar_kup(df)
    out["KLOW"] = kbar_klow(df)
    out["KSFT"] = kbar_ksft(df)

    close = df["Close"]
    volume = df["Volume"]
    for w in windows:
        out[f"ROC{w}"] = roc(close, w)
        out[f"MA{w}"] = ma_ratio(close, w)
        out[f"STD{w}"] = std_ratio(close, w)
        out[f"RSV{w}"] = rsv(df, w)
        out[f"CNTD{w}"] = cntd(close, w)
        out[f"VMA{w}"] = vma_ratio(volume, w)
        out[f"VSTD{w}"] = vstd_ratio(volume, w)
        out[f"WVMA{w}"] = wvma(df, w)
        out[f"CORR{w}"] = corr_price_volume(df, w)

    return out


def latest_alpha_snapshot(
    df: pd.DataFrame,
    windows: Tuple[int, ...] = _DEFAULT_WINDOWS,
) -> Dict[str, float]:
    """Same factors as `compute_alpha_factors`, but only the latest
    (most recent bar) value of each — what a strategy hypothesis block
    actually needs to make an entry decision `now`. NaN values (not
    enough history yet for a given window) are dropped from the result.
    """
    factors = compute_alpha_factors(df, windows=windows)
    snapshot: Dict[str, float] = {}
    for name, series in factors.items():
        if series is None or len(series) == 0:
            continue
        val = series.iloc[-1]
        if val is None or (isinstance(val, float) and np.isnan(val)):
            continue
        snapshot[name] = float(val)
    return snapshot


# ============================================================
# IC / Rank-IC — signal quality, ported from
# qlib/contrib/eva/alpha.py::calc_ic (adapted for a single time series
# instead of a (datetime, instrument) cross-sectional panel — see this
# module's docstring for why).
# ============================================================

def calc_ic(signal: pd.Series, forward_return: pd.Series) -> Tuple[Optional[float], Optional[float]]:
    """Pearson IC and Spearman Rank-IC between a signal and the return
    that FOLLOWS it (caller is responsible for the shift — pass
    `forward_return` already aligned so `forward_return[t]` is the
    return realized after `signal[t]` was observed, e.g.
    `close.pct_change().shift(-1)`).

    Returns (ic, rank_ic), both None if there isn't enough overlapping,
    non-constant data to compute a correlation (e.g. all-NaN, a
    constant signal, or fewer than 3 valid points) — never raises.

    Interpretation (same as Qlib): |IC| ~0.02-0.05 is a real, usable
    edge in most quant contexts; this is a DIAGNOSTIC, not a fitness
    function — it says nothing about position sizing, stops, or
    drawdown, only "does this signal point the right way more often
    than not."
    """
    try:
        df = pd.DataFrame({"signal": signal, "ret": forward_return}).dropna()
        if len(df) < 3:
            return None, None
        if df["signal"].nunique() < 2 or df["ret"].nunique() < 2:
            return None, None
        ic = df["signal"].corr(df["ret"], method="pearson")
        rank_ic = df["signal"].corr(df["ret"], method="spearman")
        ic_val = float(ic) if ic is not None and not np.isnan(ic) else None
        rank_ic_val = float(rank_ic) if rank_ic is not None and not np.isnan(rank_ic) else None
        return ic_val, rank_ic_val
    except Exception:
        return None, None
