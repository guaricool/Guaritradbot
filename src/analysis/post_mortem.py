"""
Autonomous Self-Learning & Loss Optimization Engine - Post-Mortem & Loss Taxonomy Module.

Evaluates completed trades to diagnose root causes of losses, calculate MAE/MFE,
and record trade fingerprints in an SQLite database for feedback loops.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from src.core.logging_setup import get_logger

logger = get_logger(__name__)


class LossCategory(str, Enum):
    REGIME_MISMATCH = "REGIME_MISMATCH"
    STOP_HUNT_NOISE = "STOP_HUNT_NOISE"
    SLIPPAGE_SPREAD_DRAG = "SLIPPAGE_SPREAD_DRAG"
    CORRELATION_CLUSTERING = "CORRELATION_CLUSTERING"
    MACRO_SHOCK = "MACRO_SHOCK"
    NORMAL_LOSS = "NORMAL_LOSS"
    WINNING_TRADE = "WINNING_TRADE"


@dataclass
class TradePostMortemRecord:
    trade_id: str
    asset: str
    strategy: str
    direction: str
    entry_price: float
    close_price: float
    pnl_usd: float
    pnl_pct: float
    entry_ts: float
    close_ts: float
    regime: str
    mae_pct: float
    mfe_pct: float
    loss_category: str
    explanation: str


class PostMortemEngine:
    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            db_dir = os.path.join(os.getcwd(), "data_store")
            os.makedirs(db_dir, exist_ok=True)
            db_path = os.path.join(db_dir, "post_mortem.db")
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_post_mortems (
                    trade_id TEXT PRIMARY KEY,
                    asset TEXT,
                    strategy TEXT,
                    direction TEXT,
                    entry_price REAL,
                    close_price REAL,
                    pnl_usd REAL,
                    pnl_pct REAL,
                    entry_ts REAL,
                    close_ts REAL,
                    regime TEXT,
                    mae_pct REAL,
                    mfe_pct REAL,
                    loss_category TEXT,
                    explanation TEXT,
                    created_at REAL
                )
                """
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to initialize post_mortem DB at {self.db_path}: {e}")

    def analyze_trade(
        self,
        trade: Dict[str, Any],
        price_series: Optional[List[float]] = None,
        regime: str = "ranging",
        adx_value: float = 20.0,
        spread_pct: float = 0.0005,
    ) -> TradePostMortemRecord:
        trade_id = str(trade.get("trade_id") or trade.get("id") or f"trade_{int(time.time()*1000)}")
        asset = str(trade.get("asset", "UNKNOWN"))
        strategy = str(trade.get("strategy", "unknown"))
        direction = str(trade.get("direction", "long")).lower()
        entry_price = float(trade.get("entry_price", 1.0))
        close_price = float(trade.get("close_price", entry_price))
        pnl_usd = float(trade.get("pnl_usd", 0.0))
        
        pnl_pct = float(trade.get("pnl_pct", 0.0))
        if pnl_pct == 0.0 and entry_price > 0:
            if direction == "long":
                pnl_pct = (close_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - close_price) / entry_price

        entry_ts = float(trade.get("entry_ts", time.time()))
        close_ts = float(trade.get("close_ts", time.time()))

        mae_pct = 0.0
        mfe_pct = 0.0
        if price_series and len(price_series) > 0:
            ps = [float(p) for p in price_series]
            if direction == "long":
                min_price = min(ps)
                max_price = max(ps)
                mae_pct = max(0.0, (entry_price - min_price) / entry_price)
                mfe_pct = max(0.0, (max_price - entry_price) / entry_price)
            else:
                min_price = min(ps)
                max_price = max(ps)
                mae_pct = max(0.0, (max_price - entry_price) / entry_price)
                mfe_pct = max(0.0, (entry_price - min_price) / entry_price)
        else:
            mae_pct = abs(min(0.0, pnl_pct))
            mfe_pct = max(0.0, pnl_pct)

        loss_category = LossCategory.WINNING_TRADE
        explanation = "Trade closed with positive return."

        if pnl_usd < 0 or pnl_pct < 0:
            sl = float(trade.get("stop_loss", 0.0))
            tp = float(trade.get("target_price", 0.0))

            is_mean_reversion = any(k in strategy.lower() for k in ["rsi", "stoch", "bollinger", "reversion"])
            if is_mean_reversion and (adx_value > 30 or regime == "trending"):
                loss_category = LossCategory.REGIME_MISMATCH
                explanation = f"Mean reversion strategy '{strategy}' traded against strong trend (ADX={adx_value:.1f}, regime={regime})."

            elif sl > 0 and tp > 0 and mfe_pct > (mae_pct * 0.8):
                loss_category = LossCategory.STOP_HUNT_NOISE
                explanation = f"Stop-loss hit by market noise before price reached MFE ({mfe_pct*100:.2f}%)."

            elif spread_pct > abs(pnl_pct) * 0.4 and spread_pct > 0.001:
                loss_category = LossCategory.SLIPPAGE_SPREAD_DRAG
                explanation = f"Spread/slippage drag ({spread_pct*100:.2f}%) consumed trade potential."

            elif mae_pct > 0.05:
                loss_category = LossCategory.MACRO_SHOCK
                explanation = f"Large adverse movement (MAE {mae_pct*100:.2f}%) indicates high volatility shock."

            else:
                loss_category = LossCategory.NORMAL_LOSS
                explanation = "Standard loss within strategy variance parameters."

        record = TradePostMortemRecord(
            trade_id=trade_id,
            asset=asset,
            strategy=strategy,
            direction=direction,
            entry_price=entry_price,
            close_price=close_price,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            entry_ts=entry_ts,
            close_ts=close_ts,
            regime=regime,
            mae_pct=mae_pct,
            mfe_pct=mfe_pct,
            loss_category=loss_category.value,
            explanation=explanation,
        )

        self.record_post_mortem(record)
        return record

    def record_post_mortem(self, record: TradePostMortemRecord) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO trade_post_mortems (
                    trade_id, asset, strategy, direction, entry_price, close_price,
                    pnl_usd, pnl_pct, entry_ts, close_ts, regime, mae_pct, mfe_pct,
                    loss_category, explanation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.trade_id,
                    record.asset,
                    record.strategy,
                    record.direction,
                    record.entry_price,
                    record.close_price,
                    record.pnl_usd,
                    record.pnl_pct,
                    record.entry_ts,
                    record.close_ts,
                    record.regime,
                    record.mae_pct,
                    record.mfe_pct,
                    record.loss_category,
                    record.explanation,
                    time.time(),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to record post-mortem for {record.trade_id}: {e}")

    def get_recent_post_mortems(self, limit: int = 100) -> List[TradePostMortemRecord]:
        out = []
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT trade_id, asset, strategy, direction, entry_price, close_price,
                       pnl_usd, pnl_pct, entry_ts, close_ts, regime, mae_pct, mfe_pct,
                       loss_category, explanation
                FROM trade_post_mortems
                ORDER BY close_ts DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            for r in rows:
                out.append(
                    TradePostMortemRecord(
                        trade_id=r["trade_id"],
                        asset=r["asset"],
                        strategy=r["strategy"],
                        direction=r["direction"],
                        entry_price=r["entry_price"],
                        close_price=r["close_price"],
                        pnl_usd=r["pnl_usd"],
                        pnl_pct=r["pnl_pct"],
                        entry_ts=r["entry_ts"],
                        close_ts=r["close_ts"],
                        regime=r["regime"],
                        mae_pct=r["mae_pct"],
                        mfe_pct=r["mfe_pct"],
                        loss_category=r["loss_category"],
                        explanation=r["explanation"],
                    )
                )
            conn.close()
        except Exception as e:
            logger.error(f"Failed to fetch post-mortems: {e}")
        return out

    def get_loss_breakdown_by_strategy(self, strategy: str) -> Dict[str, Any]:
        result = {"total_trades": 0, "wins": 0, "losses": 0, "by_category": {}}
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT loss_category, COUNT(*) FROM trade_post_mortems WHERE strategy = ? GROUP BY loss_category",
                (strategy,),
            )
            for cat, count in cursor.fetchall():
                result["by_category"][cat] = count
                result["total_trades"] += count
                if cat == LossCategory.WINNING_TRADE.value:
                    result["wins"] += count
                else:
                    result["losses"] += count
            conn.close()
        except Exception as e:
            logger.error(f"Failed to fetch breakdown for strategy {strategy}: {e}")
        return result
