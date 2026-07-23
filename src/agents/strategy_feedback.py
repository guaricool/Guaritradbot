"""
Autonomous Self-Learning & Loss Optimization Engine - Adaptive Strategy Weighting & Feedback Matrix.

Adjusts hypothesis confidence multipliers for strategies based on historical post-mortem performance
per market regime using exponential weight decay (Exp3 Multi-Armed Bandit inspired).
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from typing import Dict, Optional, Tuple

from src.core.logging_setup import get_logger

logger = get_logger(__name__)

MIN_MULTIPLIER = 0.1
MAX_MULTIPLIER = 1.5
DEFAULT_MULTIPLIER = 1.0


class StrategyFeedbackEngine:
    def __init__(self, db_path: Optional[str] = None, decay_factor: float = 0.95) -> None:
        if db_path is None:
            db_dir = os.path.join(os.getcwd(), "data_store")
            os.makedirs(db_dir, exist_ok=True)
            db_path = os.path.join(db_dir, "strategy_feedback.db")
        self.db_path = db_path
        self.decay_factor = decay_factor
        self._init_db()

    def _init_db(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_regime_weights (
                    strategy TEXT,
                    regime TEXT,
                    weight REAL,
                    consecutive_losses INTEGER,
                    total_trades INTEGER,
                    updated_at REAL,
                    PRIMARY KEY (strategy, regime)
                )
                """
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to initialize strategy_feedback DB at {self.db_path}: {e}")

    def update_feedback(self, strategy: str, regime: str, is_win: bool, pnl_pct: float) -> float:
        key_strategy = strategy.lower().strip()
        key_regime = regime.lower().strip()

        weight, losses, total = self._get_raw_weight(key_strategy, key_regime)

        total += 1
        if is_win:
            losses = 0
            reward = max(0.01, min(0.2, pnl_pct * 2.0))
            weight = weight * self.decay_factor + (1.0 + reward) * (1.0 - self.decay_factor)
        else:
            losses += 1
            penalty = max(0.05, min(0.4, abs(pnl_pct) * 3.0 + (losses * 0.05)))
            weight = weight * self.decay_factor + (1.0 - penalty) * (1.0 - self.decay_factor)

        weight = max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, weight))

        self._save_weight(key_strategy, key_regime, weight, losses, total)
        logger.info(
            f"StrategyFeedback updated [{key_strategy} | {key_regime}]: "
            f"win={is_win}, pnl={pnl_pct*100:.2f}%, new_weight={weight:.3f}, consecutive_losses={losses}"
        )
        return weight

    def get_strategy_multiplier(self, strategy: str, regime: str = "ranging") -> float:
        key_strategy = strategy.lower().strip()
        key_regime = regime.lower().strip()
        weight, _, _ = self._get_raw_weight(key_strategy, key_regime)
        return round(weight, 3)

    def _get_raw_weight(self, strategy: str, regime: str) -> Tuple[float, int, int]:
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT weight, consecutive_losses, total_trades FROM strategy_regime_weights WHERE strategy = ? AND regime = ?",
                (strategy, regime),
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                return float(row[0]), int(row[1]), int(row[2])
        except Exception as e:
            logger.error(f"Failed to fetch weight for [{strategy} | {regime}]: {e}")
        return DEFAULT_MULTIPLIER, 0, 0

    def _save_weight(self, strategy: str, regime: str, weight: float, losses: int, total: int) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO strategy_regime_weights
                (strategy, regime, weight, consecutive_losses, total_trades, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (strategy, regime, weight, losses, total, time.time()),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to save weight for [{strategy} | {regime}]: {e}")
