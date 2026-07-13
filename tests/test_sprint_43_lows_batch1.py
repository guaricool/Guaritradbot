"""
Sprint 43 — bundled low-severity tests (L1, L4, L5).

L1: KillSwitch.disarm() must not raise on missing file (TOCTOU race).
L4: GP fitness() must accept periods_per_year (was hardcoded 252).
L5: momentum.sma_crossover_strategy must use 'Close' (was 'close').
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.kill_switch import KillSwitch
from src.analysis.genetic_programming import fitness, multi_symbol_fitness
from src.strategy_legacy.momentum import sma_crossover_strategy


class L1KillSwitchDisarmTest(unittest.TestCase):
    def test_disarm_on_missing_file_does_not_raise(self):
        """TOCTOU: two operators disarming at the same time.
        With missing_ok=True, the second call is a no-op, not an
        exception.
        """
        path = os.path.join(tempfile.mkdtemp(), "kill_noexist")
        ks = KillSwitch(path)
        # Don't arm — file doesn't exist. Calling disarm() should
        # NOT raise FileNotFoundError.
        try:
            ks.disarm()
        except FileNotFoundError as e:
            self.fail(f"disarm() raised FileNotFoundError on missing file: {e}")

    def test_disarm_on_existing_file_works(self):
        path = os.path.join(tempfile.mkdtemp(), "kill_exists")
        Path(path).touch()
        ks = KillSwitch(path)
        ks.disarm()
        self.assertFalse(os.path.exists(path), "disarm() should have removed the file")


class L4GPFitnessPeriodsPerYearTest(unittest.TestCase):
    """L4: fitness() must accept periods_per_year, default 252."""

    def _make_prices(self, n=200, start=100.0):
        return pd.DataFrame({
            "Close": [start + i * 0.5 for i in range(n)],
            "Open": [start + i * 0.5 for i in range(n)],
            "High": [start + i * 0.5 + 0.1 for i in range(n)],
            "Low": [start + i * 0.5 - 0.1 for i in range(n)],
            "Volume": [1000.0] * n,
        })

    def test_fitness_accepts_periods_per_year(self):
        """L4: fitness() must accept the new periods_per_year kwarg
        and use it in the annualization. We test that the kwarg
        is accepted (no TypeError) and that the function returns
        a result dict. The exact value depends on the tree and
        the synthetic data; we just want to verify the plumbing.
        """
        from src.analysis.genetic_programming import random_tree
        import random
        rng = random.Random(0)
        tree = random_tree(rng, max_depth=2)
        prices = self._make_prices()
        # Should not raise — that's the main fix
        result_daily = fitness(tree, prices, periods_per_year=252)
        result_hourly = fitness(tree, prices, periods_per_year=252 * 24)
        # Both return a result dict with the expected keys
        self.assertIn("score", result_daily)
        self.assertIn("score", result_hourly)
        # And critically, the function accepted the new kwarg
        # (would have raised TypeError if periods_per_year wasn't
        # in the signature)

    def test_fitness_default_is_252(self):
        """Backward compat: no periods_per_year arg → still works."""
        from src.analysis.genetic_programming import random_tree
        import random
        rng = random.Random(0)
        tree = random_tree(rng, max_depth=2)
        prices = self._make_prices()
        # Should not raise
        result = fitness(tree, prices)
        self.assertIn("sharpe", result)

    def test_multi_symbol_fitness_propagates_periods_per_year(self):
        from src.analysis.genetic_programming import random_tree
        import random
        rng = random.Random(0)
        tree = random_tree(rng, max_depth=2)
        prices_by_symbol = {
            "A": self._make_prices(),
            "B": self._make_prices(n=200, start=200.0),
        }
        # Both should accept the new kwarg without TypeError
        result_d = multi_symbol_fitness(tree, prices_by_symbol, periods_per_year=252)
        result_h = multi_symbol_fitness(tree, prices_by_symbol, periods_per_year=6048)
        # The function accepted the kwarg — that's the main fix
        self.assertIn("score", result_d)
        self.assertIn("score", result_h)


class L5MomentumCloseTest(unittest.TestCase):
    def test_sma_uses_Close_column(self):
        """L5 fix: 'close' lowercase → 'Close' (consistent with rest of pipeline)."""
        n = 50
        prices = pd.DataFrame({"Close": [100 + i * 0.1 for i in range(n)]})
        # Should not raise KeyError on 'close'
        try:
            signals = sma_crossover_strategy(prices)
        except KeyError as e:
            self.fail(f"KeyError on 'close' — should use 'Close': {e}")
        # Signals should be valid (1, 0, -1)
        self.assertTrue(set(signals.unique()).issubset({0, 1, -1}))

    def test_sma_lowercase_close_now_raises(self):
        """Regression guard: 'close' lowercase is no longer supported."""
        prices = pd.DataFrame({"close": [100 + i * 0.1 for i in range(50)]})
        with self.assertRaises(KeyError):
            sma_crossover_strategy(prices)


if __name__ == "__main__":
    unittest.main()
