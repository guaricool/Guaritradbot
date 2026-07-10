"""
Sprint 38 — Multi-Timeframe (MTF) Strategy Framework.

Borrowed from StrategyQuant's "Multi-Market & Multi-TF strategies"
feature. Today every strategy in our bot is single-TF:
  - SPY/QQQ: RSI on 15m or 1h
  - BTC: MACD on 1h
  - GLD/USO: EMA cross on 4h

In real markets, a single-TF signal is noisy. A signal that aligns
across multiple timeframes is much more robust — e.g. "the 1h trend
is up AND the 15m momentum is bullish" is a stronger signal than
either alone.

This module provides a small framework for MTF strategies. A strategy
subclass declares which TFs it needs and how to combine them. The
framework handles data plumbing.

Example subclass (see below): MTFTrendPullback — go with the 4h trend,
but only enter on 1h pullbacks.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class MTFData:
    """Container: dataframes for each timeframe the strategy needs."""
    timeframes: Dict[str, pd.DataFrame]  # e.g. {"1h": df1, "4h": df4, "1d": df1d}
    asset: str

    def get(self, tf: str) -> Optional[pd.DataFrame]:
        return self.timeframes.get(tf)

    def require(self, tf: str) -> pd.DataFrame:
        """Return the dataframe for ``tf`` or raise if missing."""
        if tf not in self.timeframes:
            raise KeyError(f"MTFData for {self.asset} missing timeframe '{tf}'")
        return self.timeframes[tf]


class MultiTFStrategy(ABC):
    """Base class for multi-timeframe strategies.

    Subclasses declare which timeframes they need via ``required_timeframes``
    and implement ``generate_signal`` to combine them.

    A signal is a pandas Series aligned to the PRIMARY timeframe (the
    first one in ``required_timeframes``) with values in {-1, 0, 1}.
    """
    #: Ordered list of timeframes, primary first. Subclasses MUST set this.
    required_timeframes: List[str] = []
    #: Human-readable name (used in logs / audit).
    name: str = "MTFStrategy"

    def validate(self, data: MTFData) -> None:
        """Raise ValueError if the data is missing required timeframes."""
        missing = [tf for tf in self.required_timeframes if tf not in data.timeframes]
        if missing:
            raise ValueError(
                f"{self.name} requires timeframes {self.required_timeframes} "
                f"but data for {data.asset} only has {list(data.timeframes)}. "
                f"Missing: {missing}"
            )

    @abstractmethod
    def generate_signal(self, data: MTFData) -> pd.Series:
        """Return a Series of positions (-1, 0, 1) aligned to the primary TF.

        Implementations should:
          1. Call ``self.validate(data)`` first
          2. Pull each TF's dataframe from ``data.get(tf)``
          3. Compute per-TF features
          4. Combine them into a single signal Series
        """
        raise NotImplementedError


# ============================================================
# Example strategies (2) — the framework ships with these as a
# reference for future MTF development.
# ============================================================

class MTFTrendPullback(MultiTFStrategy):
    """4h trend + 1h RSI pullback.

    Idea: the 4h EMA cross defines the trend. The 1h RSI identifies
    pullbacks against that trend. Enter when the trend is up AND
    the 1h RSI dips into oversold (mean reversion into the trend).
    Symmetric for shorts (4h death cross + 1h RSI overbought).

    Parameters
    ----------
    rsi_oversold : float
        1h RSI level for long entry (default 30).
    rsi_overbought : float
        1h RSI level for short entry (default 70).
    ema_fast : int
        Fast EMA period on 4h (default 20).
    ema_slow : int
        Slow EMA period on 4h (default 50).
    rsi_period : int
        RSI period on 1h (default 14).
    """
    required_timeframes: List[str] = ["1h", "4h"]
    name: str = "MTFTrendPullback"

    def __init__(
        self,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
    ):
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period

    def generate_signal(self, data: MTFData) -> pd.Series:
        self.validate(data)
        df_1h = data.get("1h")
        df_4h = data.get("4h")
        # 1h RSI (vectorized). If missing, compute via simple diff.
        rsi = _rsi(df_1h["Close"], self.rsi_period)
        # 4h trend: EMA fast vs slow.
        ema_f = df_4h["Close"].ewm(span=self.ema_fast, adjust=False).mean()
        ema_s = df_4h["Close"].ewm(span=self.ema_slow, adjust=False).mean()
        # Sprint 43 H10 fix: reindex the 4h trend to 1h index using
        # NEAREST (label='nearest') AND shift the trend by ONE 4h bar.
        # The previous code used ffill which is correct for forward
        # filling the LAST KNOWN 4h trend into 1h bars (e.g. a 4h
        # bar at 12:00 spans 12:00-16:00, and the trend computed
        # from its CLOSE is only known at 16:00). The audit caught
        # that resample() labels the bar by its START (so the 12:00
        # bar's close at 16:00 is implicitly used to label the
        # 12:00-16:00 segment), which IS a look-ahead. The fix: shift
        # the trend by one 4h period so the 12:00 bar's trend is
        # only applied to 16:00+ 1h bars. This delays the trend
        # signal by 4 hours (the cost of no-look-ahead) but
        # guarantees that no future data leaks into the decision.
        # See also: H12 fix in _resample_ohlcv (drops the
        # in-progress bucket entirely).
        trend_up_raw = (ema_f > ema_s)
        trend_down_raw = (ema_f < ema_s)
        # Shift by 1 4h bar so the trend from bar N applies to
        # 1h bars at and after bar N+1, not bar N.
        trend_up_shifted = trend_up_raw.shift(1).fillna(False)
        trend_down_shifted = trend_down_raw.shift(1).fillna(False)
        # Now reindex the SHIFTED trend to 1h via ffill.
        trend_up = trend_up_shifted.reindex(df_1h.index, method="ffill").fillna(False)
        trend_down = trend_down_shifted.reindex(df_1h.index, method="ffill").fillna(False)
        # Long: trend up AND 1h RSI<oversold. Short: trend down AND RSI>overbought.
        sig = pd.Series(0.0, index=df_1h.index)
        sig[(trend_up) & (rsi < self.rsi_oversold)] = 1.0
        sig[(trend_down) & (rsi > self.rsi_overbought)] = -1.0
        return sig


class MTFDailyBiasHourlyTrigger(MultiTFStrategy):
    """1d bias + 1h trigger.

    The 1d trend (EMA cross) defines the bias. The 1h MACD cross is
    the entry trigger. Both must agree for a position.

    Heavier weight to the 1d means fewer trades, but they line up
    with the dominant trend.
    """
    required_timeframes: List[str] = ["1h", "1d"]
    name: str = "MTFDailyBiasHourlyTrigger"

    def __init__(
        self,
        ema_fast_1d: int = 20,
        ema_slow_1d: int = 50,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
    ):
        self.ema_fast_1d = ema_fast_1d
        self.ema_slow_1d = ema_slow_1d
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal

    def generate_signal(self, data: MTFData) -> pd.Series:
        self.validate(data)
        df_1h = data.get("1h")
        df_1d = data.get("1d")
        # 1d bias
        # Sprint 45 fix (H10): same look-ahead bug the audit found in
        # MTFTrendPullback (Sprint 43 H10) was still present here — a
        # daily bar labeled by resample()'s START means its EMA cross
        # (computed from its CLOSE) isn't actually known until the bar
        # ends. Reindexing the raw (un-shifted) boolean series with
        # ffill made a not-yet-closed daily bar's bias available to
        # every 1h bar from its label timestamp onward. Shifting by
        # one daily bar before reindexing guarantees day N's bias only
        # applies from day N+1 onward — no future data leaks in.
        ema_f = df_1d["Close"].ewm(span=self.ema_fast_1d, adjust=False).mean()
        ema_s = df_1d["Close"].ewm(span=self.ema_slow_1d, adjust=False).mean()
        bias_up_raw = (ema_f > ema_s)
        bias_down_raw = (ema_f < ema_s)
        bias_up_shifted = bias_up_raw.shift(1).fillna(False)
        bias_down_shifted = bias_down_raw.shift(1).fillna(False)
        bias_up = bias_up_shifted.reindex(df_1h.index, method="ffill").fillna(False)
        bias_down = bias_down_shifted.reindex(df_1h.index, method="ffill").fillna(False)
        # 1h MACD trigger
        macd_line = _ema(df_1h["Close"], self.macd_fast) - _ema(df_1h["Close"], self.macd_slow)
        sig_line = _ema(macd_line, self.macd_signal)
        macd_bull = (macd_line > sig_line)
        macd_bear = (macd_line < sig_line)
        sig = pd.Series(0.0, index=df_1h.index)
        sig[(bias_up) & macd_bull] = 1.0
        sig[(bias_down) & macd_bear] = -1.0
        return sig


# ============================================================
# Helpers (small subset of what's in alpha_zoo.py — kept local
# so the MTF module is self-contained and doesn't pull all 48
# alpha features).
# ============================================================

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Vectorized RSI — classic Wilder smoothing via EWM."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)
