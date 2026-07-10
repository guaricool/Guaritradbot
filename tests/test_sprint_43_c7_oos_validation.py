"""
Sprint 43 C7 fix tests — out-of-sample validation in the GP loop.

The audit's claim: Sprint 42's GP evolved strategies using the same
price data for both training (in-sample, IS) and selection
(final population, library addition). The "robustness" check was
multi-symbol consistency, not OOS. Overfit strategies could enter
the library and be used to trade with real money.

The fix:
  1. `evolve()` accepts a new `oos_fraction` parameter (0.0-1.0).
     When set, the last `oos_fraction` rows of each symbol's data
     are held out. The GP evolves on the first `(1 - oos_fraction)`.
  2. After evolution, the best_ever is re-evaluated on the OOS
     window. The result carries `score_is`, `score_oos`, and
     `is_oos_ratio`.
  3. `add_from_evolution()` accepts a new `min_oos_ratio` parameter.
     When > 0 and the best_ever's OOS/IS ratio is below the
     threshold, the strategy is rejected as overfit. Default 0.0
     preserves backward compatibility.

These tests verify:
  - oos_fraction=0.0 (default): backward compatible
  - oos_fraction=0.3: IS/OOS split happens
  - OOS score is computed and exposed on the result
  - add_from_evolution rejects overfit strategies when min_oos_ratio > 0
  - add_from_evolution accepts them when min_oos_ratio = 0
"""
import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.analysis.genetic_programming import (
    evolve,
    StrategyLibrary,
)


def _make_trending_data(n=300, start=100.0, trend_per_step=0.5, seed=42):
    """Build a synthetic OHLCV series that trends upward.

    The trend gives the GP something to find. Adding noise so the
    strategies can't perfectly fit. For OOS validation we want
    a noticeable difference between IS and OOS regimes.
    """
    rng = np.random.default_rng(seed)
    closes = [start]
    for _ in range(n - 1):
        # Trending up with noise
        step = trend_per_step + rng.normal(0, 1.0)
        closes.append(closes[-1] + step)
    df = pd.DataFrame({
        "open": closes,
        "high": [c + 1.0 for c in closes],
        "low": [c - 1.0 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
    })
    return df


def _make_prices(n_symbols=2, n=300):
    """Build prices dict with N trending symbols."""
    return {
        f"SYM_{i}": _make_trending_data(n=n, start=100.0 + i * 10, trend_per_step=0.4 + i * 0.1)
        for i in range(n_symbols)
    }


class BackwardCompatNoOOSTest(unittest.TestCase):
    """When oos_fraction=0 (default), behavior is identical to before."""

    def test_default_evolve_does_not_split(self):
        prices = _make_prices(n_symbols=2, n=300)
        result = evolve(
            prices_by_symbol=prices,
            population_size=6,
            n_generations=2,
            seed=42,
        )
        # OOS fields are None when oos_fraction=0
        self.assertIsNone(result.best_score_is)
        self.assertIsNone(result.best_score_oos)
        self.assertIsNone(result.is_oos_ratio)


class OOSSplitTest(unittest.TestCase):
    """When oos_fraction>0, IS/OOS split happens and result carries both."""

    def test_oos_fields_populated(self):
        prices = _make_prices(n_symbols=2, n=300)
        result = evolve(
            prices_by_symbol=prices,
            population_size=6,
            n_generations=2,
            seed=42,
            oos_fraction=0.3,
        )
        # OOS fields must be populated
        self.assertIsNotNone(result.best_score_is, "best_score_is should be set with oos_fraction>0")
        self.assertIsNotNone(result.best_score_oos, "best_score_oos should be set with oos_fraction>0")
        self.assertIsNotNone(result.is_oos_ratio, "is_oos_ratio should be set with oos_fraction>0")
        # IS and OOS scores must be finite
        self.assertTrue(np.isfinite(result.best_score_is))
        self.assertTrue(np.isfinite(result.best_score_oos))
        # Ratio must be computed correctly
        expected_ratio = result.best_score_oos / result.best_score_is if result.best_score_is != 0 else 0.0
        self.assertAlmostEqual(result.is_oos_ratio, expected_ratio, places=5)

    def test_to_dict_includes_oos_fields(self):
        prices = _make_prices(n_symbols=2, n=300)
        result = evolve(
            prices_by_symbol=prices,
            population_size=6,
            n_generations=2,
            seed=42,
            oos_fraction=0.3,
        )
        d = result.to_dict()
        self.assertIn("best_score_is", d)
        self.assertIn("best_score_oos", d)
        self.assertIn("is_oos_ratio", d)
        self.assertIsNotNone(d["best_score_is"])


class AddFromEvolutionOverfitRejectionTest(unittest.TestCase):
    """The library must reject overfit strategies when min_oos_ratio>0."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.lib = StrategyLibrary(os.path.join(self.tmpdir, "library.json"))

    def test_filter_rejects_low_oos_ratio(self):
        """
        Direct unit test of the overfit filter using a synthetic
        EvolutionResult. The candidate has is_oos_ratio=0.1, which
        is below min_oos_ratio=0.5, so it must be rejected.
        """
        from src.analysis.genetic_programming import random_tree
        from src.analysis.genetic_programming import EvolutionResult
        import random
        rng = random.Random(0)
        tree = random_tree(rng, max_depth=2)
        result = EvolutionResult(
            best_tree=tree,
            best_score=0.5,
            best_metrics={"sharpe": 1.0},
            best_per_symbol={},
            n_generations=2,
            population_size=4,
            final_population=[
                {
                    "tree": tree,
                    "score": 0.5,
                    "metrics": {"sharpe": 1.0},
                    "per_symbol": {},
                    "n_profitable": 1,
                    "sharpe_std": 0.1,
                    "score_is": 0.5,
                    "score_oos": 0.05,  # ratio = 0.1
                    "is_oos_ratio": 0.1,
                }
            ],
            history=[],
            elapsed_seconds=1.0,
            seed=0,
            best_score_is=0.5,
            best_score_oos=0.05,
            is_oos_ratio=0.1,
            best_ever={
                "tree": tree,
                "score": 0.5,
                "metrics": {"sharpe": 1.0},
                "per_symbol": {},
                "n_profitable": 1,
                "sharpe_std": 0.1,
                "score_is": 0.5,
                "score_oos": 0.05,
                "is_oos_ratio": 0.1,
            },
        )
        added = self.lib.add_from_evolution(result, top_k=5, min_oos_ratio=0.5)
        self.assertEqual(added, 0, f"Overfit (ratio=0.1) should be rejected with min_oos_ratio=0.5")

    def test_filter_accepts_high_oos_ratio(self):
        """
        Same setup as above but ratio=0.8, which is above min_oos_ratio=0.5.
        The candidate should be accepted.
        """
        from src.analysis.genetic_programming import random_tree
        from src.analysis.genetic_programming import EvolutionResult
        import random
        rng = random.Random(0)
        tree = random_tree(rng, max_depth=2)
        result = EvolutionResult(
            best_tree=tree,
            best_score=0.5,
            best_metrics={"sharpe": 1.0},
            best_per_symbol={},
            n_generations=2,
            population_size=4,
            final_population=[
                {
                    "tree": tree,
                    "score": 0.5,
                    "metrics": {"sharpe": 1.0},
                    "per_symbol": {},
                    "n_profitable": 1,
                    "sharpe_std": 0.1,
                    "score_is": 0.5,
                    "score_oos": 0.4,  # ratio = 0.8
                    "is_oos_ratio": 0.8,
                }
            ],
            history=[],
            elapsed_seconds=1.0,
            seed=0,
            best_score_is=0.5,
            best_score_oos=0.4,
            is_oos_ratio=0.8,
            best_ever={
                "tree": tree,
                "score": 0.5,
                "metrics": {"sharpe": 1.0},
                "per_symbol": {},
                "n_profitable": 1,
                "sharpe_std": 0.1,
                "score_is": 0.5,
                "score_oos": 0.4,
                "is_oos_ratio": 0.8,
            },
        )
        added = self.lib.add_from_evolution(result, top_k=5, min_oos_ratio=0.5)
        self.assertEqual(added, 1, f"Good OOS (ratio=0.8) should be accepted with min_oos_ratio=0.5")

    def test_default_min_oos_ratio_zero_does_not_filter(self):
        """
        Backward compat: min_oos_ratio=0 (default) doesn't apply
        the OOS filter, even if the candidate has a low ratio.
        """
        from src.analysis.genetic_programming import random_tree
        from src.analysis.genetic_programming import EvolutionResult
        import random
        rng = random.Random(0)
        tree = random_tree(rng, max_depth=2)
        result = EvolutionResult(
            best_tree=tree,
            best_score=0.5,
            best_metrics={"sharpe": 1.0},
            best_per_symbol={},
            n_generations=2,
            population_size=4,
            final_population=[
                {
                    "tree": tree,
                    "score": 0.5,
                    "metrics": {"sharpe": 1.0},
                    "per_symbol": {},
                    "n_profitable": 1,
                    "sharpe_std": 0.1,
                    "score_is": 0.5,
                    "score_oos": 0.0,
                    "is_oos_ratio": 0.0,
                }
            ],
            history=[],
            elapsed_seconds=1.0,
            seed=0,
            best_score_is=0.5,
            best_score_oos=0.0,
            is_oos_ratio=0.0,
            best_ever={},
        )
        added = self.lib.add_from_evolution(result, top_k=5, min_oos_ratio=0.0)
        self.assertEqual(added, 1, "With min_oos_ratio=0 (default), candidate should be accepted even with bad OOS")

    def test_candidate_without_oos_ratio_is_accepted(self):
        """
        If a candidate in final_population has NO is_oos_ratio
        (e.g. it wasn't individually OOS-evaluated), the filter
        must not reject it. Only OOS-evaluated candidates are
        subject to the filter.
        """
        from src.analysis.genetic_programming import random_tree
        from src.analysis.genetic_programming import EvolutionResult
        import random
        rng = random.Random(0)
        tree = random_tree(rng, max_depth=2)
        result = EvolutionResult(
            best_tree=tree,
            best_score=0.5,
            best_metrics={"sharpe": 1.0},
            best_per_symbol={},
            n_generations=2,
            population_size=4,
            final_population=[
                {
                    "tree": tree,
                    "score": 0.5,
                    "metrics": {"sharpe": 1.0},
                    "per_symbol": {},
                    "n_profitable": 1,
                    "sharpe_std": 0.1,
                    # NO is_oos_ratio — never OOS-evaluated
                }
            ],
            history=[],
            elapsed_seconds=1.0,
            seed=0,
            best_ever={},
        )
        added = self.lib.add_from_evolution(result, top_k=5, min_oos_ratio=0.99)
        self.assertEqual(added, 1, "Non-OOS-evaluated candidates must not be filtered")


class SafeRatioHelperTest(unittest.TestCase):
    """_safe_ratio handles division by zero and signs correctly."""

    def test_normal_ratio(self):
        from src.analysis.genetic_programming import _safe_ratio
        self.assertAlmostEqual(_safe_ratio(2.0, 4.0), 0.5)

    def test_zero_denominator_zero_num(self):
        from src.analysis.genetic_programming import _safe_ratio
        self.assertEqual(_safe_ratio(0.0, 0.0), 0.0)

    def test_zero_denominator_positive_num(self):
        from src.analysis.genetic_programming import _safe_ratio
        self.assertEqual(_safe_ratio(1.0, 0.0), float("inf"))


if __name__ == "__main__":
    unittest.main()
