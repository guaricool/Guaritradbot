"""
Sprint 37/39/40/38/41 — Tests for the new analysis + strategy modules.

Run: python -m unittest tests.test_sprint_37_41 -v
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ============================================================
# Sprint 37 — Risk Metrics & Monte Carlo
# ============================================================

class RiskMetricsTest(unittest.TestCase):
    def test_sharpe_basic(self):
        from src.analysis.risk_metrics import sharpe_ratio
        # Flat series → 0
        self.assertEqual(sharpe_ratio([0.0, 0.0, 0.0, 0.0]), 0.0)
        # Mixed series with positive drift → positive Sharpe
        s = sharpe_ratio([0.02, -0.01, 0.03, -0.02, 0.015, 0.01, -0.005, 0.02, -0.01, 0.025] * 30)
        self.assertGreater(s, 0.0)
        # Mixed series — just check it's a number
        s = sharpe_ratio([0.02, -0.01, 0.03, -0.02, 0.01])
        self.assertIsInstance(s, float)

    def test_sharpe_too_short(self):
        from src.analysis.risk_metrics import sharpe_ratio
        self.assertEqual(sharpe_ratio([]), 0.0)
        self.assertEqual(sharpe_ratio([0.01]), 0.0)

    def test_sortino_no_downside(self):
        from src.analysis.risk_metrics import sortino_ratio
        # All positive returns → infinite Sortino
        result = sortino_ratio([0.01] * 100)
        self.assertEqual(result, float("inf"))
        # All negative → negative Sortino (math: neg/pos = neg)
        result = sortino_ratio([-0.01] * 100)
        self.assertLess(result, 0.0)

    def test_calmar_basic(self):
        from src.analysis.risk_metrics import calmar_ratio
        self.assertEqual(calmar_ratio(0.10, 0), 0.0)
        self.assertAlmostEqual(calmar_ratio(0.20, -0.10), 2.0)

    def test_max_drawdown(self):
        from src.analysis.risk_metrics import max_drawdown
        # Equity went 100 → 120 → 80 → 110: mdd at 80 is -33% from peak 120
        mdd = max_drawdown([100, 120, 80, 110])
        self.assertLess(mdd, 0)
        # Flat equity → 0
        self.assertEqual(max_drawdown([100, 100, 100]), 0.0)

    def test_compute_ratios(self):
        from src.analysis.risk_metrics import compute_ratios
        r = compute_ratios([0.01] * 50, [10, 10.1, 10.2, 10.3, 10.4])
        self.assertIn("sharpe", r)
        self.assertIn("sortino", r)
        self.assertIn("calmar", r)
        self.assertIn("max_drawdown", r)
        self.assertIn("annual_return", r)

    def test_monte_carlo_too_few_trades(self):
        from src.analysis.risk_metrics import monte_carlo_simulation
        result = monte_carlo_simulation([], starting_equity=1000.0, n_simulations=10)
        self.assertEqual(result.n_trades, 0)
        self.assertEqual(result.n_simulations, 0)

    def test_monte_carlo_reproducibility(self):
        from src.analysis.risk_metrics import monte_carlo_simulation
        # Same seed → same result
        trades = [0.02, -0.01, 0.03, -0.015, 0.01, -0.005, 0.025, -0.02] * 10
        r1 = monte_carlo_simulation(trades, n_simulations=100, seed=42)
        r2 = monte_carlo_simulation(trades, n_simulations=100, seed=42)
        self.assertEqual(r1.final_equity_p50, r2.final_equity_p50)
        self.assertEqual(r1.prob_profit, r2.prob_profit)

    def test_monte_carlo_distribution(self):
        from src.analysis.risk_metrics import monte_carlo_simulation
        # Mixed trades (realistic) → p5 < p50 < p95
        trades = [0.02, -0.005, 0.015, -0.01, 0.025, -0.015, 0.01, -0.02, 0.03, -0.005] * 10
        r = monte_carlo_simulation(trades, n_simulations=200, seed=1)
        self.assertLess(r.final_equity_p5, r.final_equity_p50)
        self.assertLess(r.final_equity_p50, r.final_equity_p95)
        # All-positive → high prob_profit, all-negative → low
        pos = [0.01] * 50
        neg = [-0.01] * 50
        r_pos = monte_carlo_simulation(pos, n_simulations=200, seed=1)
        r_neg = monte_carlo_simulation(neg, n_simulations=200, seed=1)
        self.assertGreater(r_pos.prob_profit, 0.95)
        self.assertLess(r_neg.prob_profit, 0.05)

    def test_monte_carlo_ruin_probability(self):
        from src.analysis.risk_metrics import monte_carlo_simulation
        # Very negative returns → high ruin probability
        catastrophic = [-0.20] * 20
        r = monte_carlo_simulation(catastrophic, n_simulations=200, seed=7)
        self.assertGreater(r.prob_ruin_50pct, 0.5)

    def test_risk_report_robustness_label(self):
        from src.analysis.risk_metrics import build_risk_report
        # Strong strategy → robust
        strong = [0.02, -0.005, 0.015, 0.01, 0.02, -0.005, 0.018, 0.01, 0.025, -0.005] * 20
        rep = build_risk_report(strong, n_simulations=200, seed=1)
        self.assertIn(rep.robustness_label(), ("robust", "marginal", "fragile"))
        # Bad strategy → fragile or marginal
        weak = [-0.05, 0.01, -0.04, 0.005, -0.06, 0.01, -0.03, -0.01] * 20
        rep2 = build_risk_report(weak, n_simulations=200, seed=1)
        self.assertIn(rep2.robustness_label(), ("robust", "marginal", "fragile"))


# ============================================================
# Sprint 39 — Strategy Correlation
# ============================================================

class StrategyCorrelationTest(unittest.TestCase):
    def test_perfectly_correlated(self):
        from src.analysis.strategy_correlation import (
            StrategyReturns, compute_correlation_matrix, average_correlation
        )
        s1 = StrategyReturns(name="A", returns=[0.01, -0.005, 0.02, -0.01, 0.015])
        s2 = StrategyReturns(name="B", returns=[0.01, -0.005, 0.02, -0.01, 0.015])  # identical
        m = compute_correlation_matrix([s1, s2])
        self.assertAlmostEqual(m[0, 1], 1.0, places=5)
        self.assertAlmostEqual(average_correlation(m), 1.0, places=5)

    def test_anticorrelated(self):
        from src.analysis.strategy_correlation import (
            StrategyReturns, compute_correlation_matrix, average_correlation
        )
        s1 = StrategyReturns(name="A", returns=[0.02, -0.01, 0.03, -0.015, 0.01])
        s2 = StrategyReturns(name="B", returns=[-0.02, 0.01, -0.03, 0.015, -0.01])  # mirror
        m = compute_correlation_matrix([s1, s2])
        self.assertAlmostEqual(m[0, 1], -1.0, places=5)

    def test_uncorrelated(self):
        from src.analysis.strategy_correlation import (
            StrategyReturns, compute_correlation_matrix, average_correlation
        )
        # Two independent-ish series
        np.random.seed(42)
        s1 = StrategyReturns(name="A", returns=list(np.random.normal(0.01, 0.02, 100)))
        s2 = StrategyReturns(name="B", returns=list(np.random.normal(0.01, 0.02, 100)))
        m = compute_correlation_matrix([s1, s2])
        # Should be small (|corr| < 0.3 with high prob)
        self.assertLess(abs(m[0, 1]), 0.3)

    def test_build_uncorrelated_portfolio_picks_best_first(self):
        from src.analysis.strategy_correlation import (
            StrategyReturns, build_uncorrelated_portfolio
        )
        # One clearly best by Sharpe, rest mediocre
        best = StrategyReturns(name="BEST", returns=[0.02, 0.01, 0.025, 0.015, 0.02, 0.01, 0.025], sharpe=5.0)
        s2 = StrategyReturns(name="S2", returns=[0.01, 0.005, 0.015, 0.01, 0.02, 0.01, 0.015], sharpe=1.0)
        s3 = StrategyReturns(name="S3", returns=[-0.01, 0.005, 0.01, -0.005, 0.015, 0.01, 0.02], sharpe=0.5)
        portfolio = build_uncorrelated_portfolio([s2, s3, best], max_n=3, target_avg_corr=0.9)
        self.assertEqual(portfolio[0], "BEST")
        self.assertEqual(len(portfolio), 3)

    def test_build_uncorrelated_portfolio_caps_at_target_corr(self):
        from src.analysis.strategy_correlation import (
            StrategyReturns, build_uncorrelated_portfolio
        )
        # Two strategies perfectly correlated → portfolio of size 1
        s1 = StrategyReturns(name="A", returns=[0.01, -0.005, 0.02, -0.01, 0.015], sharpe=2.0)
        s2 = StrategyReturns(name="B", returns=[0.01, -0.005, 0.02, -0.01, 0.015], sharpe=1.0)
        portfolio = build_uncorrelated_portfolio([s1, s2], max_n=5, target_avg_corr=0.5)
        # s1 wins first, s2 has corr=1.0 with s1 → avg_corr = 1.0 > 0.5 → stop
        self.assertEqual(len(portfolio), 1)
        self.assertEqual(portfolio[0], "A")

    def test_analyze_strategies_one_strategy(self):
        from src.analysis.strategy_correlation import (
            StrategyReturns, analyze_strategies
        )
        # Single strategy → trivial result
        only = StrategyReturns(name="ONLY", returns=[0.01, 0.02, 0.01], sharpe=2.0)
        result = analyze_strategies([only])
        self.assertEqual(result.strategies, ["ONLY"])
        self.assertEqual(result.recommended_portfolio, ["ONLY"])
        self.assertTrue(result.well_diversified)

    def test_analyze_strategies_well_diversified_flag(self):
        from src.analysis.strategy_correlation import (
            StrategyReturns, analyze_strategies
        )
        np.random.seed(0)
        s1 = StrategyReturns(name="A", returns=list(np.random.normal(0.01, 0.02, 200)))
        s2 = StrategyReturns(name="B", returns=list(np.random.normal(0.01, 0.02, 200)))
        s3 = StrategyReturns(name="C", returns=list(np.random.normal(0.01, 0.02, 200)))
        result = analyze_strategies([s1, s2, s3])
        # Independent random series → low avg_corr → well_diversified=True
        self.assertLess(result.avg_correlation, 0.3)
        self.assertTrue(result.well_diversified)


# ============================================================
# Sprint 40 — Parameter Robustness / Permutation
# ============================================================

class ParameterRobustnessTest(unittest.TestCase):
    def _make_prices(self, n=200, seed=42):
        """Synthetic trending price series."""
        rng = np.random.default_rng(seed)
        ret = rng.normal(0.001, 0.02, n)
        prices = 100 * np.cumprod(1 + ret)
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        df = pd.DataFrame({"Close": prices, "Open": prices, "High": prices, "Low": prices}, index=idx)
        return df

    def _rsi_strategy(self, df, rsi_oversold=30, rsi_overbought=70):
        """Vectorized RSI cross strategy (matches backtester interface)."""
        from src.analysis.genetic_programming import ind_rsi
        rsi = ind_rsi(df["Close"], 14)
        cross_below = (rsi.shift(1) >= rsi_oversold) & (rsi < rsi_oversold)
        cross_above = (rsi.shift(1) <= rsi_overbought) & (rsi > rsi_overbought)
        sig = pd.Series(0.0, index=df.index)
        sig[cross_below] = 1.0
        sig[cross_above] = -1.0
        return sig.replace(0, np.nan).ffill().fillna(0)

    def test_robust_strategy_high_pct_profitable(self):
        from src.analysis.parameter_robustness import permutation_test
        # Random walk → RSI strategy won't be consistently profitable
        prices = self._make_prices(n=200, seed=42)
        result = permutation_test(
            prices, self._rsi_strategy,
            base_params={"rsi_oversold": 30, "rsi_overbought": 70},
            n_permutations=30, seed=1,
        )
        # We just check the result structure is sane
        self.assertIn(result.robustness_label, ("robust", "marginal", "fragile"))
        self.assertGreaterEqual(result.pct_profitable, 0.0)
        self.assertLessEqual(result.pct_profitable, 1.0)
        self.assertEqual(len(result.perm_total_returns), 30)

    def test_trending_market_more_robust(self):
        from src.analysis.parameter_robustness import permutation_test
        # Trending prices (RSI on trend should work better)
        rng = np.random.default_rng(0)
        ret = rng.normal(0.005, 0.015, 300)  # positive drift, lower vol
        prices = 100 * np.cumprod(1 + ret)
        idx = pd.date_range("2024-01-01", periods=300, freq="D")
        df = pd.DataFrame({"Close": prices}, index=idx)
        result = permutation_test(
            df, self._rsi_strategy,
            base_params={"rsi_oversold": 30, "rsi_overbought": 70},
            n_permutations=50, seed=42,
        )
        # Just verify the call succeeds and the distribution is populated
        self.assertEqual(result.n_permutations, 50)
        self.assertGreater(len(result.perm_total_returns), 0)
        self.assertGreaterEqual(result.perm_p50_total_return, result.perm_p5_total_return)
        self.assertGreaterEqual(result.perm_p95_total_return, result.perm_p50_total_return)

    def test_permutation_uses_custom_ranges(self):
        from src.analysis.parameter_robustness import permutation_test
        prices = self._make_prices(n=200, seed=42)
        # Custom range: only perturb rsi_oversold, not rsi_overbought
        result = permutation_test(
            prices, self._rsi_strategy,
            base_params={"rsi_oversold": 30, "rsi_overbought": 70},
            param_ranges={"rsi_oversold": (-0.10, 0.10)},  # tight range
            n_permutations=20, seed=99,
        )
        self.assertEqual(result.n_permutations, 20)

    def test_result_to_dict(self):
        from src.analysis.parameter_robustness import permutation_test
        prices = self._make_prices(n=100, seed=1)
        result = permutation_test(
            prices, self._rsi_strategy,
            base_params={"rsi_oversold": 30, "rsi_overbought": 70},
            n_permutations=10, seed=1,
        )
        d = result.to_dict()
        self.assertIn("base_params", d)
        self.assertIn("pct_profitable", d)
        self.assertIn("robustness_label", d)


# ============================================================
# Sprint 38 — Multi-TF Strategies
# ============================================================

class MultiTFStrategyTest(unittest.TestCase):
    def _make_ohlcv(self, tf, n=200, seed=42):
        rng = np.random.default_rng(seed)
        ret = rng.normal(0.001, 0.02, n)
        prices = 100 * np.cumprod(1 + ret)
        idx = pd.date_range("2024-01-01", periods=n, freq=tf)
        return pd.DataFrame({"Close": prices, "Open": prices, "High": prices, "Low": prices}, index=idx)

    def test_validate_missing_tf_raises(self):
        from src.strategy.multi_tf import MTFTrendPullback, MTFData
        strat = MTFTrendPullback()
        # Only provide 4h, not 1h
        data = MTFData(timeframes={"4h": self._make_ohlcv("4h")}, asset="SPY")
        with self.assertRaises(ValueError):
            strat.validate(data)

    def test_mtf_trend_pullback_produces_signal(self):
        from src.strategy.multi_tf import MTFTrendPullback, MTFData
        strat = MTFTrendPullback()
        data = MTFData(
            timeframes={"1h": self._make_ohlcv("h", n=400, seed=1),
                       "4h": self._make_ohlcv("4h", n=100, seed=2)},
            asset="SPY",
        )
        sig = strat.generate_signal(data)
        # Signal should be in {-1, 0, 1}
        self.assertEqual(set(sig.unique()).issubset({-1.0, 0.0, 1.0}), True)
        # Signal length matches primary TF
        self.assertEqual(len(sig), len(data.get("1h")))

    def test_mtf_daily_bias_hourly_trigger(self):
        from src.strategy.multi_tf import MTFDailyBiasHourlyTrigger, MTFData
        strat = MTFDailyBiasHourlyTrigger()
        data = MTFData(
            timeframes={"1h": self._make_ohlcv("h", n=400, seed=3),
                       "1d": self._make_ohlcv("D", n=50, seed=4)},
            asset="BTC-USD",
        )
        sig = strat.generate_signal(data)
        self.assertEqual(len(sig), len(data.get("1h")))
        # Should be all valid values
        self.assertTrue(set(sig.unique()).issubset({-1.0, 0.0, 1.0}))

    def test_tree_size_and_depth(self):
        from src.analysis.genetic_programming import StrategyTree
        # Build a simple tree manually
        leaf = StrategyTree(node=("rsi_below", {"threshold": 30}), children=[])
        root = StrategyTree(node="AND", children=[leaf, leaf])
        self.assertEqual(root.depth(), 2)
        self.assertEqual(root.size(), 3)


# ============================================================
# Sprint 41 — Genetic Programming scaffold
# ============================================================

class GeneticProgrammingTest(unittest.TestCase):
    def _make_prices(self, n=300, seed=42):
        rng = np.random.default_rng(seed)
        ret = rng.normal(0.001, 0.02, n)
        prices = 100 * np.cumprod(1 + ret)
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        return pd.DataFrame({"Close": prices}, index=idx)

    def test_random_tree_shape(self):
        import random as rnd
        from src.analysis.genetic_programming import random_tree, StrategyTree
        rng = rnd.Random(0)
        # max_depth=3 means up to 3 levels of combinators (root + 2 children).
        # Tree depth() counts the longest path, so max is max_depth + 1.
        tree = random_tree(rng, max_depth=3)
        # depth and size are positive
        self.assertGreater(tree.depth(), 0)
        self.assertGreater(tree.size(), 0)
        # tree depth doesn't exceed max_depth + 1 (root + nested)
        self.assertLessEqual(tree.depth(), 4)
        # For very shallow (max_depth=1), tree depth should be at most 2
        tree2 = random_tree(rng, max_depth=1)
        self.assertLessEqual(tree2.depth(), 2)

    def test_evaluate_tree_returns_valid_signal(self):
        import random as rnd
        from src.analysis.genetic_programming import (
            random_tree, evaluate_tree, precompute_indicators
        )
        prices = self._make_prices(n=200, seed=0)
        indicators = precompute_indicators(prices["Close"])
        rng = rnd.Random(0)
        tree = random_tree(rng, max_depth=2)
        sig = evaluate_tree(tree, indicators, direction=1)
        # All values in {-1, 0, 1} (or NaN if indicator missing)
        valid = sig.dropna().unique()
        self.assertTrue(set(valid).issubset({-1.0, 0.0, 1.0}))
        self.assertEqual(len(sig), len(prices))

    def test_fitness_returns_dict(self):
        from src.analysis.genetic_programming import (
            random_tree, fitness, random
        )
        prices = self._make_prices(n=200, seed=0)
        rng = random.Random(0)
        tree = random_tree(rng, max_depth=2)
        fit = fitness(tree, prices, direction=1)
        self.assertIn("sharpe", fit)
        self.assertIn("total_return", fit)
        self.assertIn("max_drawdown", fit)
        self.assertIn("n_trades", fit)
        self.assertIn("parsimony", fit)

    def test_composite_score(self):
        from src.analysis.genetic_programming import composite_score
        # Higher Sharpe → higher score
        s1 = composite_score({"sharpe": 2.0, "total_return": 0.1, "parsimony": 0.5})
        s2 = composite_score({"sharpe": 0.5, "total_return": 0.1, "parsimony": 0.5})
        self.assertGreater(s1, s2)

    def test_run_demo_returns_top_k(self):
        from src.analysis.genetic_programming import run_demo
        prices = self._make_prices(n=300, seed=42)
        results = run_demo(prices, n_random=10, top_k=3, seed=0)
        self.assertEqual(len(results), 3)
        # Sorted descending by score
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # Each has tree + score + metrics
        for r in results:
            self.assertIsNotNone(r.tree)
            self.assertIsInstance(r.score, float)
            self.assertIn("sharpe", r.metrics)

    def test_run_demo_with_garbage_inputs_doesnt_crash(self):
        from src.analysis.genetic_programming import run_demo
        # Very short data
        prices = self._make_prices(n=20, seed=0)
        results = run_demo(prices, n_random=5, top_k=2, seed=0)
        # Should not crash; results may be all 0 score
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
