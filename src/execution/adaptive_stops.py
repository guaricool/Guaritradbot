"""
Autonomous Self-Learning & Loss Optimization Engine - Adaptive Stop/Target Tuner.

Auto-tunes ATR Stop-Loss and Take-Profit multipliers using historical MAE (Maximum Adverse Excursion)
and STOP_HUNT_NOISE post-mortem analytics, while keeping USD risk-per-trade 100% constant.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

from src.analysis.post_mortem import LossCategory, PostMortemEngine
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

# Standard default ATR multipliers
DEFAULT_SL_ATR_MULT = 2.0
DEFAULT_TP_ATR_MULT = 3.0

# Safe boundaries for dynamic adjustment
MIN_SL_ATR_MULT = 1.5
MAX_SL_ATR_MULT = 3.0
MIN_TP_ATR_MULT = 2.0
MAX_TP_ATR_MULT = 5.0


class AdaptiveStopTuner:
    """
    Evaluates MAE distributions and post-mortem noise records to optimize SL/TP distance.
    """

    def __init__(self, post_mortem_engine: Optional[PostMortemEngine] = None) -> None:
        self.pm_engine = post_mortem_engine or PostMortemEngine()

    def get_optimal_stop_target_mults(
        self, asset: str, strategy: str
    ) -> Tuple[float, float]:
        """
        Returns (sl_atr_mult, tp_atr_mult) tuned based on historical trade post-mortems.
        """
        records = self.pm_engine.get_recent_post_mortems(limit=100)

        # Filter by strategy or asset if sufficient data exists
        filtered = [
            r for r in records if r.strategy == strategy or r.asset == asset
        ]
        if len(filtered) < 5:
            # Not enough sample history, return standard defaults
            return DEFAULT_SL_ATR_MULT, DEFAULT_TP_ATR_MULT

        total_losses = [r for r in filtered if r.pnl_usd < 0 or r.pnl_pct < 0]
        if not total_losses:
            return DEFAULT_SL_ATR_MULT, DEFAULT_TP_ATR_MULT

        noise_losses = [
            r for r in total_losses if r.loss_category == LossCategory.STOP_HUNT_NOISE.value
        ]

        noise_ratio = len(noise_losses) / len(total_losses)

        # Calculate average MAE on losing trades
        avg_mae = sum(r.mae_pct for r in total_losses) / len(total_losses)

        sl_mult = DEFAULT_SL_ATR_MULT
        tp_mult = DEFAULT_TP_ATR_MULT

        # If more than 30% of losses were STOP_HUNT_NOISE, widen SL multiplier
        if noise_ratio > 0.30:
            expansion = min(1.0, noise_ratio * 1.5)
            sl_mult = min(MAX_SL_ATR_MULT, DEFAULT_SL_ATR_MULT + expansion)
            logger.info(
                f"AdaptiveStopTuner [{asset} | {strategy}]: High noise ratio ({noise_ratio*100:.1f}%), widening SL ATR mult to {sl_mult:.2f}"
            )
        elif avg_mae < 0.01 and len(total_losses) >= 10:
            # MAE is very low before stop out, can tighten SL slightly to increase Risk:Reward
            sl_mult = max(MIN_SL_ATR_MULT, DEFAULT_SL_ATR_MULT - 0.3)

        # Calculate average MFE on winning trades for TP tuning
        winning = [r for r in filtered if r.pnl_usd > 0 or r.pnl_pct > 0]
        if len(winning) >= 5:
            avg_mfe = sum(r.mfe_pct for r in winning) / len(winning)
            if avg_mfe > 0.04:
                tp_mult = min(MAX_TP_ATR_MULT, DEFAULT_TP_ATR_MULT + 0.5)

        return round(sl_mult, 2), round(tp_mult, 2)
