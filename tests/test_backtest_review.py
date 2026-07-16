"""
Backtest review checklist — review_backtest() in src/optimization/backtester.py.

Gap identified from reviewing external backtest-review guides (look-ahead
bias, overfitting, too-few-trades, missing costs, benchmark comparison):
this pins the rule-based checklist behavior with synthetic data so it
doesn't depend on network/yfinance.
"""
import os
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.optimization.backtester import VectorizedBacktester, review_backtest


def _make_prices(n=250, seed=1, drift=0.0003, vol=0.01):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = 100 * (1 + pd.Series(rets)).cumprod()
    dates = pd.date_range("2024-01-01", periods=n, freq="1D")
    return pd.DataFrame({"Close": close.values}, index=dates)


def _alternating_signal(prices: pd.DataFrame) -> pd.Series:
    """Flips long/flat every 5 bars — guarantees plenty of trades."""
    idx = np.arange(len(prices))
    sig = ((idx // 5) % 2 == 0).astype(int)
    return pd.Series(sig, index=prices.index)


def _never_trade_signal(prices: pd.DataFrame) -> pd.Series:
    return pd.Series(0, index=prices.index)


class ReviewBacktestPassTest(unittest.TestCase):
    def test_healthy_backtest_passes(self):
        prices = _make_prices(seed=7)
        bt = VectorizedBacktester(initial_capital=10_000)
        result = bt.run(prices, _alternating_signal)
        review = review_backtest(result, min_trades=5)
        self.assertIn(review["verdict"], ("PASS", "REVISE"))
        self.assertEqual(review["failed"], [])
        # Structural look-ahead-bias check always passes.
        self.assertTrue(any("look_ahead_bias" in p for p in review["passed"]))


class ReviewBacktestTooFewTradesTest(unittest.TestCase):
    def test_zero_trades_is_rejected(self):
        prices = _make_prices(seed=2)
        bt = VectorizedBacktester()
        result = bt.run(prices, _never_trade_signal)
        review = review_backtest(result, min_trades=30)
        self.assertEqual(review["verdict"], "REJECT")
        self.assertTrue(any("too_few_trades" in f for f in review["failed"]))

    def test_few_trades_is_a_warning_not_a_rejection(self):
        prices = _make_prices(n=40, seed=3)

        def few_trades_signal(p):
            sig = pd.Series(0, index=p.index)
            sig.iloc[10] = 1
            sig.iloc[20] = 0
            return sig

        bt = VectorizedBacktester()
        result = bt.run(prices, few_trades_signal)
        review = review_backtest(result, min_trades=30)
        self.assertIn(review["verdict"], ("REVISE",))
        self.assertTrue(any("too_few_trades" in w for w in review["warnings"]))


class ReviewBacktestOverfitTest(unittest.TestCase):
    def test_overfit_warning_from_walk_forward_rejects(self):
        prices = _make_prices(seed=5)
        bt = VectorizedBacktester()
        result = bt.run(prices, _alternating_signal)
        wf_result = {"overfit_warning": True, "is_vs_oos_ratio": 0.1}
        review = review_backtest(result, walk_forward_result=wf_result, min_trades=5)
        self.assertEqual(review["verdict"], "REJECT")
        self.assertTrue(any("overfitting" in f for f in review["failed"]))

    def test_no_walk_forward_result_is_a_warning(self):
        prices = _make_prices(seed=6)
        bt = VectorizedBacktester()
        result = bt.run(prices, _alternating_signal)
        review = review_backtest(result, min_trades=5)
        self.assertTrue(any("overfitting" in w for w in review["warnings"]))


class ReviewBacktestBenchmarkTest(unittest.TestCase):
    def test_beating_benchmark_passes(self):
        prices = _make_prices(seed=9)
        bt = VectorizedBacktester()
        result = bt.run(prices, _alternating_signal)
        # A benchmark that clearly underperforms the strategy.
        benchmark = pd.Series(-0.5 / len(prices), index=prices.index)
        review = review_backtest(result, benchmark_returns=benchmark, min_trades=5)
        self.assertTrue(any("benchmark" in p for p in review["passed"]))

    def test_missing_benchmark_is_a_warning(self):
        prices = _make_prices(seed=10)
        bt = VectorizedBacktester()
        result = bt.run(prices, _alternating_signal)
        review = review_backtest(result, min_trades=5)
        self.assertTrue(any("benchmark" in w for w in review["warnings"]))


class ReviewBacktestVerdictTest(unittest.TestCase):
    def test_verdict_reject_wins_over_warnings(self):
        prices = _make_prices(seed=11)
        bt = VectorizedBacktester()
        result = bt.run(prices, _never_trade_signal)
        review = review_backtest(result, min_trades=30)
        self.assertEqual(review["verdict"], "REJECT")

    def test_verdict_shape(self):
        prices = _make_prices(seed=12)
        bt = VectorizedBacktester()
        result = bt.run(prices, _alternating_signal)
        review = review_backtest(result, min_trades=5)
        for key in ("passed", "failed", "warnings", "verdict"):
            self.assertIn(key, review)
        self.assertIn(review["verdict"], ("PASS", "REVISE", "REJECT"))


if __name__ == "__main__":
    unittest.main()
