"""
Bug fix: EpochScheduler.run_reoptimization() imported `walk_forward_split`
as if it validated results, but never actually called it (or
`walk_forward_validate`) -- it ran an in-sample-only grid search via
`HyperoptManager.optimize()` and applied whatever it found directly to
the live StrategyAgent, with no out-of-sample check and no comparison
against the params already in use. It also only ever tested
`self.assets[0]`, silently ignoring the other 4 assets main.py
actually configures.

Fixed to use `walk_forward_validate` (already existed, fully built,
just never called from here) and a promotion gate (`_should_promote`)
that requires: no overfit warning, enough assets with usable data, and
a real out-of-sample Sharpe improvement over the CURRENT params
evaluated through the identical walk-forward mechanics.

Run: python -m unittest tests.test_scheduler_reoptimization_walk_forward -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.execution.scheduler import (
    EpochScheduler,
    _should_promote,
    _write_strategy_params_override,
)
from src.agents.strategy_agent import DEFAULT_STRATEGY_PARAMS


class ShouldPromoteTest(unittest.TestCase):
    def test_promotes_when_clear_improvement_no_overfit(self):
        should, reason = _should_promote(
            candidate_oos_sharpes=[1.0, 1.2, 0.9],
            baseline_oos_sharpes=[0.5, 0.6, 0.4],
            any_overfit=False,
        )
        self.assertTrue(should)
        self.assertIn("promoted", reason)

    def test_does_not_promote_when_overfit_warning(self):
        should, reason = _should_promote(
            candidate_oos_sharpes=[2.0, 2.0, 2.0],
            baseline_oos_sharpes=[0.1, 0.1, 0.1],
            any_overfit=True,
        )
        self.assertFalse(should)
        self.assertIn("overfit", reason)

    def test_does_not_promote_when_improvement_too_small(self):
        should, reason = _should_promote(
            candidate_oos_sharpes=[0.51, 0.52, 0.50],
            baseline_oos_sharpes=[0.50, 0.50, 0.50],
            any_overfit=False,
        )
        self.assertFalse(should)
        self.assertIn("insufficient_improvement", reason)

    def test_does_not_promote_when_candidate_is_worse(self):
        should, reason = _should_promote(
            candidate_oos_sharpes=[0.1, 0.2],
            baseline_oos_sharpes=[0.8, 0.9],
            any_overfit=False,
        )
        self.assertFalse(should)

    def test_does_not_promote_with_too_few_assets(self):
        should, reason = _should_promote(
            candidate_oos_sharpes=[2.0],
            baseline_oos_sharpes=[0.1],
            any_overfit=False,
            min_assets=2,
        )
        self.assertFalse(should)
        self.assertIn("insufficient_data", reason)

    def test_custom_min_improvement_threshold_respected(self):
        should, _ = _should_promote(
            candidate_oos_sharpes=[0.65, 0.65],
            baseline_oos_sharpes=[0.50, 0.50],
            any_overfit=False,
            min_improvement=0.10,
        )
        self.assertTrue(should)
        should, _ = _should_promote(
            candidate_oos_sharpes=[0.65, 0.65],
            baseline_oos_sharpes=[0.50, 0.50],
            any_overfit=False,
            min_improvement=0.20,
        )
        self.assertFalse(should)


class WriteStrategyParamsOverrideTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "strategy_params_override.json")

    def test_writes_new_params_and_history_entry(self):
        _write_strategy_params_override(
            self.path,
            old_params={"rsi_oversold": 30, "rsi_overbought": 70},
            new_params={"rsi_oversold": 25, "rsi_overbought": 75},
            reason="test promotion",
        )
        with open(self.path, encoding="utf-8") as f:
            data = json.loads(f.read())
        self.assertEqual(data["rsi_oversold"], 25)
        self.assertEqual(data["rsi_overbought"], 75)
        self.assertEqual(len(data["_history"]), 1)
        self.assertEqual(data["_history"][0]["reason"], "test promotion")
        self.assertEqual(data["_history"][0]["old"]["rsi_oversold"], 30)

    def test_history_accumulates_across_calls(self):
        for i in range(3):
            _write_strategy_params_override(
                self.path, {"rsi_oversold": 30}, {"rsi_oversold": 30 + i}, f"round {i}",
            )
        with open(self.path, encoding="utf-8") as f:
            data = json.loads(f.read())
        self.assertEqual(len(data["_history"]), 3)
        self.assertEqual(data["rsi_oversold"], 32)

    def test_history_capped_at_20(self):
        for i in range(25):
            _write_strategy_params_override(
                self.path, {"rsi_oversold": 30}, {"rsi_oversold": 30 + i}, f"round {i}",
            )
        with open(self.path, encoding="utf-8") as f:
            data = json.loads(f.read())
        self.assertEqual(len(data["_history"]), 20)
        # Most recent entries kept, not the oldest.
        self.assertEqual(data["_history"][-1]["reason"], "round 24")

    def test_corrupt_existing_file_does_not_crash(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not valid json{{{")
        _write_strategy_params_override(
            self.path, {"rsi_oversold": 30}, {"rsi_oversold": 25}, "recovers",
        )
        with open(self.path, encoding="utf-8") as f:
            data = json.loads(f.read())
        self.assertEqual(data["rsi_oversold"], 25)
        self.assertEqual(len(data["_history"]), 1)


def _make_trending_ohlc(n=500, seed=1, trend=0.0006, noise=0.01):
    """Synthetic daily OHLC with a mild trend + noise -- enough
    structure for RSI mean-reversion crossovers to fire repeatedly and
    differently across different oversold/overbought thresholds, so
    walk_forward_validate has something real to discriminate between."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(trend, noise, n)
    close = 100 * np.cumprod(1 + rets)
    idx = pd.date_range("2022-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": 1_000_000,
    }, index=idx)


class RunReoptimizationIntegrationTest(unittest.TestCase):
    """End-to-end through the real walk_forward_validate/backtester
    machinery (no mocking of the math itself) with synthetic OHLC data
    standing in for MarketAnalystAgent.fetch_one."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.override_path = os.path.join(self.tmpdir, "strategy_params_override.json")
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))

        self.strategy_agent = MagicMock()
        self.strategy_agent.params = dict(DEFAULT_STRATEGY_PARAMS)

        self.market_analyst = MagicMock()
        # Two assets, each with the SAME synthetic series so the test
        # is deterministic regardless of which asset "wins" the mode
        # vote for candidate params.
        df = _make_trending_ohlc()
        self.market_analyst.fetch_one.return_value = df

        self.scheduler = EpochScheduler(
            engine=MagicMock(),
            workflow_data={"id": "test", "steps": []},
            market_analyst=self.market_analyst,
            strategy_agent=self.strategy_agent,
            hyperopt=MagicMock(),  # unused now -- walk_forward_validate builds its own
            audit=self.audit,
            assets=("BTC-USD", "SPY"),
            strategy_params_override_path=self.override_path,
        )

    def test_tests_every_configured_asset_not_just_the_first(self):
        self.scheduler.run_reoptimization()
        # Bug fix regression guard: fetch_one must be called once per
        # configured asset (2), not just once for assets[0].
        self.assertEqual(self.market_analyst.fetch_one.call_count, 2)
        called_assets = [c.args[0] for c in self.market_analyst.fetch_one.call_args_list]
        self.assertIn("BTC-USD", called_assets)
        self.assertIn("SPY", called_assets)

    def test_emits_start_and_a_decision_event(self):
        self.scheduler.run_reoptimization()
        types = [e[0] for e in self.audit_events]
        self.assertIn("REOPT_START", types)
        self.assertTrue(
            "REOPT_NEW_PARAMS" in types or "REOPT_NOT_PROMOTED" in types,
            f"expected a promotion decision event, got: {types}",
        )

    def test_promotion_writes_override_file_when_promoted(self):
        self.scheduler.run_reoptimization()
        types = [e[0] for e in self.audit_events]
        if "REOPT_NEW_PARAMS" in types:
            self.assertTrue(os.path.exists(self.override_path))
            with open(self.override_path, encoding="utf-8") as f:
                data = json.loads(f.read())
            self.assertIn("rsi_oversold", data)
            self.assertEqual(len(data["_history"]), 1)
        else:
            # Not promoted this run (legitimate outcome with random
            # synthetic data) -- override file must NOT be written.
            self.assertFalse(os.path.exists(self.override_path))

    def test_insufficient_data_is_skipped_not_crashed(self):
        self.market_analyst.fetch_one.return_value = pd.DataFrame({"Close": [1, 2, 3]})
        self.scheduler.run_reoptimization()
        types = [e[0] for e in self.audit_events]
        self.assertIn("REOPT_SKIPPED", types)
        self.assertFalse(os.path.exists(self.override_path))

    def test_market_analyst_none_is_a_no_op(self):
        self.scheduler.market_analyst = None
        self.scheduler.run_reoptimization()  # must not raise
        self.assertEqual(self.audit_events, [])


class PromotionSurvivesStrategyAgentPerCycleResetTest(unittest.TestCase):
    """Bug fix: StrategyAgent.evaluate_strategies() resets `self.params`
    from `self._live_params` at the top of every call (paper-vs-live
    profile switching -- see strategy_agent.py's paper_params_overrides
    docstring). A promotion that only set `.params` would get silently
    wiped on the very next evaluate_strategies() call; this test uses a
    REAL StrategyAgent (not a MagicMock) so that per-cycle reset
    actually runs, and confirms the promotion survives it."""

    def setUp(self):
        from src.agents.strategy_agent import StrategyAgent

        self.tmpdir = tempfile.mkdtemp()
        self.override_path = os.path.join(self.tmpdir, "strategy_params_override.json")
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: None
        self.strategy_agent = StrategyAgent()
        self.market_analyst = MagicMock()
        self.market_analyst.fetch_one.return_value = _make_trending_ohlc()
        self.scheduler = EpochScheduler(
            engine=MagicMock(),
            workflow_data={"id": "test", "steps": []},
            market_analyst=self.market_analyst,
            strategy_agent=self.strategy_agent,
            hyperopt=MagicMock(),
            audit=self.audit,
            assets=("BTC-USD", "SPY"),
            strategy_params_override_path=self.override_path,
        )

    def test_promoted_params_survive_a_subsequent_evaluate_strategies_call(self):
        # Force promotion deterministically -- this test is about the
        # mechanical interaction between a promotion and StrategyAgent's
        # per-cycle reset, not about whether THIS random synthetic data
        # happens to produce a winning candidate.
        with patch("src.execution.scheduler._should_promote", return_value=(True, "forced for test")):
            self.scheduler.run_reoptimization()
        self.assertTrue(os.path.exists(self.override_path))
        promoted_oversold = self.strategy_agent.params["rsi_oversold"]
        promoted_overbought = self.strategy_agent.params["rsi_overbought"]

        # Simulate the next trading cycle -- this used to reset
        # .params back to the stale DEFAULT_STRATEGY_PARAMS, silently
        # discarding the promotion.
        self.strategy_agent.evaluate_strategies({}, {"analyze_market": {"market_data": {}}})

        self.assertEqual(self.strategy_agent.params["rsi_oversold"], promoted_oversold)
        self.assertEqual(self.strategy_agent.params["rsi_overbought"], promoted_overbought)
        self.assertEqual(self.strategy_agent._live_params["rsi_oversold"], promoted_oversold)


if __name__ == "__main__":
    unittest.main()
