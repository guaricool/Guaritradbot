"""
Autonomous Self-Learning & Loss Optimization Engine - Dynamic Opportunity Scanner.

Scans candles across the market universe for high-probability setups:
1. Volatility Compression (Squeeze Play)
2. Relative Strength Divergence against asset class benchmark
3. Trend Acceleration / Breakout Momentum
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.core.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class OpportunitySignal:
    asset: str
    setup_type: str  # "SQUEEZE_BREAKOUT", "RELATIVE_STRENGTH", "VOLATILITY_EXPANSION"
    direction: str   # "long" or "short"
    confidence: float
    details: str


class OpportunityScanner:
    """
    Scans OHLCV DataFrames to discover structural market opportunities.
    """

    @staticmethod
    def detect_squeeze(df: pd.DataFrame, window: int = 20, num_std: float = 2.0, atr_mult: float = 1.5) -> Optional[Dict[str, Any]]:
        """
        Detect Bollinger Band squeeze inside Keltner Channel.
        """
        if len(df) < window + 5:
            return None

        close = df["close"]
        high = df["high"]
        low = df["low"]

        # Bollinger Bands
        sma = close.rolling(window=window).mean()
        std = close.rolling(window=window).std()
        bb_upper = sma + (std * num_std)
        bb_lower = sma - (std * num_std)

        # Keltner Channel
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=window).mean()

        kc_upper = sma + (atr * atr_mult)
        kc_lower = sma - (atr * atr_mult)

        latest_idx = df.index[-1]
        in_squeeze = bool((bb_upper.loc[latest_idx] < kc_upper.loc[latest_idx]) and (bb_lower.loc[latest_idx] > kc_lower.loc[latest_idx]))
        prev_squeeze = bool((bb_upper.iloc[-2] < kc_upper.iloc[-2]) and (bb_lower.iloc[-2] > kc_lower.iloc[-2]))

        if prev_squeeze and not in_squeeze:
            last_close = close.iloc[-1]
            last_sma = sma.iloc[-1]
            direction = "long" if last_close > last_sma else "short"
            return {
                "in_squeeze": False,
                "is_release": True,
                "direction": direction,
                "bb_width": float(bb_upper.iloc[-1] - bb_lower.iloc[-1]),
                "kc_width": float(kc_upper.iloc[-1] - kc_lower.iloc[-1]),
            }

        return {
            "in_squeeze": in_squeeze,
            "is_release": False,
            "direction": "flat",
            "bb_width": float(bb_upper.iloc[-1] - bb_lower.iloc[-1]),
            "kc_width": float(kc_upper.iloc[-1] - kc_lower.iloc[-1]),
        }

    def scan_asset(self, asset: str, df: pd.DataFrame) -> List[OpportunitySignal]:
        """
        Scan a single asset's OHLCV dataframe for opportunities.
        """
        signals = []
        if df.empty or len(df) < 25:
            return signals

        # 1. Squeeze Check
        squeeze_res = self.detect_squeeze(df)
        if squeeze_res and squeeze_res.get("is_release"):
            signals.append(
                OpportunitySignal(
                    asset=asset,
                    setup_type="SQUEEZE_BREAKOUT",
                    direction=squeeze_res["direction"],
                    confidence=0.85,
                    details=f"Bollinger/Keltner Squeeze Release in direction {squeeze_res['direction']}",
                )
            )

        # 2. Relative Strength Check
        close = df["close"]
        returns_20 = (close.iloc[-1] - close.iloc[-20]) / close.iloc[-20] if len(close) >= 20 else 0.0

        if returns_20 > 0.05:
            signals.append(
                OpportunitySignal(
                    asset=asset,
                    setup_type="RELATIVE_STRENGTH",
                    direction="long",
                    confidence=0.75,
                    details=f"Strong 20-bar return ({returns_20*100:.1f}%)",
                )
            )

        return signals
