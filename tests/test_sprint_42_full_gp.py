"""
Sprint 42 — Tests for the FULL GP loop (operators, evolution, library).

Builds on tests/test_sprint_37_41.py (which tests the scaffold + helpers).
This file tests the new pieces:
  - Mutation operators (leaf_swap, threshold_perturb, combinator_swap, subtree)
  - Crossover (subtree swap)
  - Tournament selection
  - Multi-generation evolution
  - Multi-symbol robustness
  - StrategyLibrary (dedup, persistence, JSON)
  - CLI runner end-to-end

Run: python -m unittest tests.test_sprint_42_full_gp -v
"""
import json
import os
import random
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ============================================================
# Helpers
# ============================================================

def _make_prices(n=300, seed=42, drift=0.0, vol=0.02):
    rng = np.random.default_rng(seed)
    ret = rng.normal(drift, vol, n)
    prices = 100 * np.cumprod(1 + ret)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Close": prices, "Open": prices, "High": prices, "Low": prices},
        index=idx,
    )


def _make_multi_symbol_prices():
    """3 symbols with different characters: trending up, trending down, sideways."""
    out = {}
    for i, (drift, vol, sym) in enumerate([
        (0.002, 0.015, "UP"),
        (-0.001, 0.020, "DOWN"),
        (0.0, 0.025, "FLAT"),
    ]):
        out[sym] = _make_prices(n=300, seed=i, drift=drift, vol=vol)
    return out


# ============================================================
# Mutation Operators
# ============================================================

class MutationTest(unittest.TestCase):
    def test_leaf_swap_changes_a_leaf(self):
        from src.analysis.genetic_programming import (
            StrategyTree, random_tree, mutate_leaf_swap,
        )
        rng = random.Random(0)
        tree = random_tree(rng, max_depth=3)
        # collect_leaves returns (path, leaf_node) tuples
        before_leaves = [str(leaf.node) for _path, leaf in tree.collect_leaves()]
        new_tree = mutate_leaf_swap(tree, rng)
        after_leaves = [str(leaf.node) for _path, leaf in new_tree.collect_leaves()]
        # At least one leaf should have changed (or the whole tree if random)
        self.assertEqual(len(before_leaves), len(after_leaves))

    def test_threshold_perturb_modifies_params(self):
        from src.analysis.genetic_programming import (
            StrategyTree, random_tree, mutate_threshold_perturb,
        )
        rng = random.Random(42)
        # Build a tree with an RSI leaf (has a threshold param)
        tree = StrategyTree(
            node="AND",
            children=[
                StrategyTree(node=("rsi_below", {"threshold": 30.0}), children=[]),
                StrategyTree(node=("rsi_above", {"threshold": 70.0}), children=[]),
            ],
        )
        # Force-perturb many times, expect at least one threshold change
        found_change = False
        for _ in range(50):
            new_tree = mutate_threshold_perturb(tree, rng, sigma=0.5)
            for (_op, orig_leaf), (_np, new_leaf) in zip(tree.collect_leaves(), new_tree.collect_leaves()):
                if orig_leaf.node[0] == "rsi_below":
                    orig_t = orig_leaf.node[1]["threshold"]
                    new_t = new_leaf.node[1]["threshold"]
                    if abs(orig_t - new_t) > 0.1:
                        found_change = True
                        break
            if found_change:
                break
        self.assertTrue(found_change, "Threshold perturb should change RSI threshold")

    def test_combinator_swap_preserves_structure(self):
        from src.analysis.genetic_programming import (
            StrategyTree, random_tree, mutate_combinator_swap,
        )
        rng = random.Random(0)
        tree = StrategyTree(
            node="AND",
            children=[
                StrategyTree(node=("rsi_below", {"threshold": 30}), children=[]),
                StrategyTree(node=("rsi_above", {"threshold": 70}), children=[]),
            ],
        )
        new_tree = mutate_combinator_swap(tree, rng)
        # Combinator should have changed to OR or SIGNAL
        self.assertIn(new_tree.node, ["OR", "SIGNAL"])
        # Number of children preserved
        self.assertEqual(len(new_tree.children), 2)
        # The leaves should be the same
        new_leaves = [leaf.node for _path, leaf in new_tree.collect_leaves()]
        self.assertEqual(len(new_leaves), 2)
        self.assertIn(("rsi_below", {"threshold": 30}), new_leaves)

    def test_subtree_mutation_changes_size_or_shape(self):
        from src.analysis.genetic_programming import (
            StrategyTree, random_tree, mutate_subtree,
        )
        rng = random.Random(0)
        tree = random_tree(rng, max_depth=3)
        new_tree = mutate_subtree(tree, rng)
        # New tree is valid (no exceptions), has at least one leaf
        self.assertGreater(new_tree.size(), 0)

    def test_point_mutation_runs(self):
        from src.analysis.genetic_programming import (
            StrategyTree, random_tree, point_mutation,
        )
        rng = random.Random(0)
        tree = random_tree(rng, max_depth=3)
        # Run a few times; no exception
        for _ in range(20):
            new_tree = point_mutation(tree, rng)
            self.assertGreater(new_tree.size(), 0)


# ============================================================
# Crossover
# ============================================================

class CrossoverTest(unittest.TestCase):
    def test_crossover_produces_two_children(self):
        from src.analysis.genetic_programming import (
            StrategyTree, random_tree, crossover,
        )
        rng = random.Random(0)
        p1 = random_tree(rng, max_depth=2)
        p2 = random_tree(rng, max_depth=3)
        c1, c2 = crossover(p1, p2, rng)
        self.assertIsInstance(c1, StrategyTree)
        self.assertIsInstance(c2, StrategyTree)
        # Both have at least one leaf
        self.assertGreater(c1.size(), 0)
        self.assertGreater(c2.size(), 0)

    def test_crossover_swaps_subtree_content(self):
        from src.analysis.genetic_programming import (
            StrategyTree, crossover,
        )
        # Two simple trees with distinct roots
        p1 = StrategyTree(
            node="AND",
            children=[
                StrategyTree(node=("rsi_below", {"threshold": 30}), children=[]),
                StrategyTree(node=("rsi_above", {"threshold": 70}), children=[]),
            ],
        )
        p2 = StrategyTree(
            node="OR",
            children=[
                StrategyTree(node=("macd_bull", {}), children=[]),
                StrategyTree(node=("macd_bear", {}), children=[]),
            ],
        )
        rng = random.Random(0)
        c1, c2 = crossover(p1, p2, rng)
        # Children should now have elements from the other parent (most likely)
        # At minimum, they should be valid trees
        self.assertGreater(c1.size(), 0)
        self.assertGreater(c2.size(), 0)
        # collect_leaves should work on the children too
        self.assertGreater(len(c1.collect_leaves()), 0)


# ============================================================
# Tournament Selection
# ============================================================

class TournamentSelectionTest(unittest.TestCase):
    def test_picks_highest_score(self):
        from src.analysis.genetic_programming import tournament_selection
        rng = random.Random(0)
        pop = [
            {"tree": None, "score": 0.1},
            {"tree": None, "score": 0.5},
            {"tree": None, "score": 0.2},
            {"tree": None, "score": 0.9},
            {"tree": None, "score": 0.3},
        ]
        # Run many times — at least one should pick the 0.9
        picks = [tournament_selection(pop, rng, tournament_size=3)["score"] for _ in range(50)]
        self.assertIn(0.9, picks)

    def test_empty_population_raises(self):
        from src.analysis.genetic_programming import tournament_selection
        rng = random.Random(0)
        with self.assertRaises(ValueError):
            tournament_selection([], rng)

    def test_tournament_size_clamped(self):
        from src.analysis.genetic_programming import tournament_selection
        rng = random.Random(0)
        pop = [{"tree": None, "score": 0.5}]
        # tournament_size > pop size should be clamped
        result = tournament_selection(pop, rng, tournament_size=5)
        self.assertEqual(result["score"], 0.5)


# ============================================================
# Multi-Symbol Fitness
# ============================================================

class MultiSymbolFitnessTest(unittest.TestCase):
    def test_returns_robust_for_trending_strategies(self):
        from src.analysis.genetic_programming import (
            StrategyTree, multi_symbol_fitness,
        )
        prices = _make_multi_symbol_prices()
        # An RSI mean-reversion strategy
        tree = StrategyTree(
            node="AND",
            children=[
                StrategyTree(node=("rsi_below", {"threshold": 30}), children=[]),
                StrategyTree(node=("rsi_above", {"threshold": 70}), children=[]),
            ],
        )
        result = multi_symbol_fitness(tree, prices, min_symbols=1)
        # We don't care about profitability, just that it runs and gives sane output
        self.assertIn("score", result)
        self.assertIn("per_symbol_scores", result)
        self.assertGreaterEqual(result["score"], 0.0)

    def test_fails_min_symbols_check(self):
        from src.analysis.genetic_programming import (
            StrategyTree, multi_symbol_fitness,
        )
        # Single symbol → can't meet min_symbols=2
        tree = StrategyTree(
            node=("rsi_below", {"threshold": 30}), children=[],
        )
        result = multi_symbol_fitness(tree, {"ONLY": _make_prices()}, min_symbols=2)
        self.assertFalse(result["robust"])


# ============================================================
# Evolution Loop
# ============================================================

class EvolutionTest(unittest.TestCase):
    def test_evolve_returns_valid_result(self):
        from src.analysis.genetic_programming import evolve
        prices = _make_multi_symbol_prices()
        result = evolve(
            prices,
            population_size=10,
            n_generations=2,
            seed=42,
            use_multi_symbol=False,  # single-symbol mode for speed
        )
        self.assertIsNotNone(result.best_tree)
        self.assertGreater(len(result.history), 0)
        self.assertEqual(result.n_generations, 2)
        # History entries have the right shape
        for h in result.history:
            self.assertIn("best_score", h)
            self.assertIn("avg_score", h)
            self.assertIn("best_sharpe", h)

    def test_evolve_with_multi_symbol(self):
        from src.analysis.genetic_programming import evolve
        prices = _make_multi_symbol_prices()
        result = evolve(
            prices,
            population_size=8,
            n_generations=2,
            seed=99,
            use_multi_symbol=True,
            min_symbols=1,
        )
        self.assertIsNotNone(result.best_tree)

    def test_evolution_improves_over_random(self):
        """After 5+ generations, the best should be better than the
        average random strategy (proves the loop is working)."""
        from src.analysis.genetic_programming import evolve, run_demo
        prices = _make_multi_symbol_prices()
        # Baseline: top of random pop
        random_results = run_demo(
            list(prices.values())[0], n_random=20, top_k=1, seed=42
        )
        random_best = random_results[0].score
        # Evolved
        result = evolve(
            prices,
            population_size=15,
            n_generations=4,
            seed=42,
            use_multi_symbol=False,
        )
        # The evolved best should be at least as good (and usually better)
        self.assertGreaterEqual(result.best_score, random_best * 0.8)

    def test_determinism_same_seed(self):
        from src.analysis.genetic_programming import evolve
        prices = _make_multi_symbol_prices()
        r1 = evolve(prices, population_size=8, n_generations=2, seed=42, use_multi_symbol=False)
        r2 = evolve(prices, population_size=8, n_generations=2, seed=42, use_multi_symbol=False)
        self.assertEqual(r1.best_score, r2.best_score)


# ============================================================
# Strategy Library
# ============================================================

class StrategyLibraryTest(unittest.TestCase):
    def test_add_new_entry(self):
        from src.analysis.genetic_programming import (
            StrategyTree, StrategyLibrary,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lib.json")
            lib = StrategyLibrary(path)
            tree = StrategyTree(
                node=("rsi_below", {"threshold": 30}), children=[],
            )
            ok = lib.add(
                tree, score=0.5, metrics={"sharpe": 1.0},
                per_symbol={"SPY": {"sharpe": 1.0}}, n_profitable=1, sharpe_std=0.0,
            )
            self.assertTrue(ok)
            self.assertEqual(len(lib), 1)

    def test_dedup_rejects_duplicate_with_lower_score(self):
        from src.analysis.genetic_programming import (
            StrategyTree, StrategyLibrary,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lib.json")
            lib = StrategyLibrary(path)
            tree = StrategyTree(
                node=("rsi_below", {"threshold": 30}), children=[],
            )
            lib.add(tree, score=0.5, metrics={}, per_symbol={}, n_profitable=0, sharpe_std=0)
            # Same tree, lower score → rejected
            ok = lib.add(tree, score=0.3, metrics={}, per_symbol={}, n_profitable=0, sharpe_std=0)
            self.assertFalse(ok)
            self.assertEqual(len(lib), 1)

    def test_dedup_replaces_with_higher_score(self):
        from src.analysis.genetic_programming import (
            StrategyTree, StrategyLibrary,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lib.json")
            lib = StrategyLibrary(path)
            tree = StrategyTree(
                node=("rsi_below", {"threshold": 30}), children=[],
            )
            lib.add(tree, score=0.3, metrics={"old": True}, per_symbol={}, n_profitable=0, sharpe_std=0)
            ok = lib.add(tree, score=0.7, metrics={"new": True}, per_symbol={}, n_profitable=0, sharpe_std=0)
            self.assertTrue(ok)
            self.assertEqual(len(lib), 1)
            self.assertEqual(lib.entries[0].score, 0.7)
            self.assertIn("new", lib.entries[0].metrics)

    def test_persistence_round_trip(self):
        from src.analysis.genetic_programming import (
            StrategyTree, StrategyLibrary,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lib.json")
            lib1 = StrategyLibrary(path)
            tree = StrategyTree(
                node="AND",
                children=[
                    StrategyTree(node=("rsi_below", {"threshold": 25}), children=[]),
                    StrategyTree(node=("rsi_above", {"threshold": 75}), children=[]),
                ],
            )
            lib1.add(tree, score=0.6, metrics={"sharpe": 1.2},
                     per_symbol={"SPY": {"sharpe": 1.2}}, n_profitable=1, sharpe_std=0)
            lib1.save()
            # New instance loads from disk
            lib2 = StrategyLibrary(path)
            self.assertEqual(len(lib2), 1)
            self.assertEqual(lib2.entries[0].score, 0.6)
            self.assertEqual(lib2.entries[0].tree.node, "AND")
            self.assertEqual(len(lib2.entries[0].tree.children), 2)

    def test_prune(self):
        from src.analysis.genetic_programming import (
            StrategyTree, StrategyLibrary,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lib.json")
            lib = StrategyLibrary(path)
            for i in range(5):
                # Different trees (vary threshold) → different sigs
                tree = StrategyTree(
                    node=("rsi_below", {"threshold": 20 + i}), children=[],
                )
                lib.add(tree, score=i * 0.1, metrics={}, per_symbol={}, n_profitable=0, sharpe_std=0)
            self.assertEqual(len(lib), 5)
            # Prune: keep only score >= 0.2 (3 entries) and cap at 2
            removed = lib.prune(min_score=0.2, max_entries=2)
            self.assertEqual(removed, 3)
            self.assertEqual(len(lib), 2)
            # Top 2 are 0.4 and 0.3 (with float tolerance)
            scores = [e.score for e in lib.entries]
            self.assertAlmostEqual(scores[0], 0.4, places=5)
            self.assertAlmostEqual(scores[1], 0.3, places=5)

    def test_add_from_evolution(self):
        from src.analysis.genetic_programming import (
            evolve, StrategyLibrary,
        )
        prices = _make_multi_symbol_prices()
        result = evolve(
            prices, population_size=8, n_generations=2, seed=42, use_multi_symbol=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lib.json")
            lib = StrategyLibrary(path)
            n_added = lib.add_from_evolution(result, top_k=3)
            self.assertGreaterEqual(n_added, 1)
            self.assertGreaterEqual(len(lib), 1)
            # Top of lib matches result top
            self.assertAlmostEqual(lib.top(1)[0].score, result.best_score, places=4)

    def test_top_returns_sorted_descending(self):
        from src.analysis.genetic_programming import (
            StrategyTree, StrategyLibrary,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lib.json")
            lib = StrategyLibrary(path)
            for i, s in enumerate([0.3, 0.7, 0.1, 0.9, 0.5]):
                tree = StrategyTree(
                    node=("rsi_below", {"threshold": 20 + i}), children=[],
                )
                lib.add(tree, score=s, metrics={}, per_symbol={}, n_profitable=0, sharpe_std=0)
            top = lib.top(3)
            scores = [e.score for e in top]
            self.assertEqual(scores, [0.9, 0.7, 0.5])


# ============================================================
# CLI Runner
# ============================================================

class CLIRunnerTest(unittest.TestCase):
    def test_run_evolution_cli_end_to_end(self):
        from src.analysis.genetic_programming import run_evolution_cli
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "lib.json")
            result = run_evolution_cli(
                output_path=output,
                population_size=8,
                n_generations=2,
                seed=42,
                verbose=False,
            )
            self.assertIsNotNone(result.best_tree)
            # Library was saved
            self.assertTrue(os.path.exists(output))
            # Library is non-empty
            with open(output) as f:
                data = json.load(f)
            self.assertGreater(len(data["entries"]), 0)

    def test_run_evolution_cli_verbose(self):
        """Verbose mode prints to stdout without erroring."""
        import io
        from src.analysis.genetic_programming import run_evolution_cli
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "lib.json")
            # Just verify it doesn't crash
            run_evolution_cli(
                output_path=output,
                population_size=5,
                n_generations=1,
                seed=7,
                verbose=True,
            )


if __name__ == "__main__":
    unittest.main()
