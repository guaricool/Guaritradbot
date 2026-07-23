"""
Unit tests for PostMortemEngine.
"""
import os
import tempfile
import time
import unittest

from src.analysis.post_mortem import LossCategory, PostMortemEngine


class TestPostMortem(unittest.TestCase):
    def test_post_mortem_classification_win(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_pm.db")
            engine = PostMortemEngine(db_path=db_path)

            trade = {
                "trade_id": "t1",
                "asset": "BTC-USD",
                "strategy": "RSI_Oversold",
                "direction": "long",
                "entry_price": 50000.0,
                "close_price": 52000.0,
                "pnl_usd": 400.0,
                "entry_ts": time.time() - 3600,
                "close_ts": time.time(),
            }

            rec = engine.analyze_trade(trade, price_series=[50000, 51000, 52000])
            self.assertEqual(rec.loss_category, LossCategory.WINNING_TRADE.value)
            self.assertEqual(rec.pnl_usd, 400.0)
            self.assertGreater(rec.mfe_pct, 0.03)

    def test_post_mortem_regime_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_pm.db")
            engine = PostMortemEngine(db_path=db_path)

            trade = {
                "trade_id": "t2",
                "asset": "ETH-USD",
                "strategy": "RSI_Oversold_Bounce",
                "direction": "long",
                "entry_price": 3000.0,
                "close_price": 2850.0,
                "pnl_usd": -150.0,
                "entry_ts": time.time() - 3600,
                "close_ts": time.time(),
            }

            rec = engine.analyze_trade(trade, price_series=[3000, 2900, 2850], adx_value=35.0)
            self.assertEqual(rec.loss_category, LossCategory.REGIME_MISMATCH.value)

    def test_post_mortem_db_retrieval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_pm.db")
            engine = PostMortemEngine(db_path=db_path)

            trade = {
                "trade_id": "t3",
                "asset": "SPY",
                "strategy": "EMA_Cross",
                "direction": "long",
                "entry_price": 500.0,
                "close_price": 510.0,
                "pnl_usd": 200.0,
            }
            engine.analyze_trade(trade)
            history = engine.get_recent_post_mortems(limit=10)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].trade_id, "t3")
            self.assertEqual(history[0].asset, "SPY")


if __name__ == "__main__":
    unittest.main()
