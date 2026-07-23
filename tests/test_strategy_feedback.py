"""
Unit tests for StrategyFeedbackEngine.
"""
import os
import tempfile
import unittest

from src.agents.strategy_feedback import StrategyFeedbackEngine, MIN_MULTIPLIER, MAX_MULTIPLIER


class TestStrategyFeedback(unittest.TestCase):
    def test_strategy_feedback_decay_and_penalties(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_feedback.db")
            engine = StrategyFeedbackEngine(db_path=db_path, decay_factor=0.90)

            initial_w = engine.get_strategy_multiplier("rsi_oversold", "ranging")
            self.assertEqual(initial_w, 1.0)

            w = 1.0
            # Simulate 5 consecutive losing trades
            for _ in range(5):
                w = engine.update_feedback("rsi_oversold", "ranging", is_win=False, pnl_pct=-0.02)

            self.assertLess(w, 1.0)
            self.assertGreaterEqual(w, MIN_MULTIPLIER)

            # Simulate 5 winning trades
            w_win = w
            for _ in range(5):
                w_win = engine.update_feedback("rsi_oversold", "ranging", is_win=True, pnl_pct=0.03)

            self.assertGreater(w_win, w)


if __name__ == "__main__":
    unittest.main()
