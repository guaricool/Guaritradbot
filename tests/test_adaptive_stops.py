"""
Unit tests for AdaptiveStopTuner.
"""
import os
import tempfile
import unittest

from src.analysis.post_mortem import LossCategory, PostMortemEngine
from src.execution.adaptive_stops import AdaptiveStopTuner, DEFAULT_SL_ATR_MULT


class TestAdaptiveStops(unittest.TestCase):
    def test_adaptive_stop_tuner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_stops.db")
            pm = PostMortemEngine(db_path=db_path)
            tuner = AdaptiveStopTuner(post_mortem_engine=pm)

            sl, tp = tuner.get_optimal_stop_target_mults("BTC-USD", "RSI_Oversold")
            self.assertEqual(sl, DEFAULT_SL_ATR_MULT)

            for i in range(10):
                pm.record_post_mortem(
                    pm.analyze_trade(
                        {
                            "trade_id": f"t_{i}",
                            "asset": "BTC-USD",
                            "strategy": "RSI_Oversold",
                            "direction": "long",
                            "entry_price": 50000,
                            "close_price": 49000,
                            "pnl_usd": -100,
                            "stop_loss": 49500,
                            "target_price": 52000,
                        },
                        price_series=[50000, 49200, 51500],
                    )
                )

            sl_new, tp_new = tuner.get_optimal_stop_target_mults("BTC-USD", "RSI_Oversold")
            self.assertGreater(sl_new, DEFAULT_SL_ATR_MULT)


if __name__ == "__main__":
    unittest.main()
