"""
Sprint 44B — Tier 2: recession stress test, tail risk (CVaR), allocation policy.

Tests for:
  - src/analysis/stress_test.py   (scenarios, position stress, portfolio stress, worst case)
  - src/analysis/tail_risk.py      (VaR, CVaR, portfolio aggregation, one-shot)
  - src/data/asset_allocation.py   (policy schema, drift, gate, integration with risk_agent)
  - src/agents/risk_agent.py       (new _check_allocation method)

Run: python -m unittest tests.test_sprint_44b_tier2 -v
"""
import sys
import os
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ============================================================
# Helpers
# ============================================================

class _FakePos:
    """Tiny position stand-in for stress_test and asset_allocation tests."""
    def __init__(self, asset: str, notional_usd: float):
        self.asset = asset
        self._notional = notional_usd

    @property
    def notional_usd(self) -> float:
        return self._notional


# ============================================================
# Sprint 44B — Stress test scenarios
# ============================================================

class StressScenariosTest(unittest.TestCase):
    def test_three_scenarios_defined(self):
        from src.analysis.stress_test import DEFAULT_SCENARIOS
        self.assertEqual(len(DEFAULT_SCENARIOS), 3)
        names = {s.name for s in DEFAULT_SCENARIOS}
        self.assertEqual(
            names,
            {"2008_GFC", "2020_COVID", "2022_RATE_HIKES"},
        )

    def test_each_scenario_has_all_asset_classes(self):
        """Each scenario must specify a shock for every asset class — never
        rely on a default of 0 for an unknown class."""
        from src.analysis.stress_test import DEFAULT_SCENARIOS
        from src.data.asset_class import AssetClass
        for s in DEFAULT_SCENARIOS:
            for cls in AssetClass:
                self.assertIn(
                    cls.value, s.shocks,
                    f"Scenario {s.name} missing shock for {cls.value}",
                )

    def test_equity_shocks_are_negative_in_all_scenarios(self):
        from src.analysis.stress_test import DEFAULT_SCENARIOS
        for s in DEFAULT_SCENARIOS:
            self.assertLess(
                s.shocks["equity_growth"], 0,
                f"{s.name} equity_growth shock should be negative",
            )
            self.assertLess(
                s.shocks["equity_value"], 0,
                f"{s.name} equity_value shock should be negative",
            )

    def test_crypto_shocks_are_large_negative(self):
        """Crypto is the riskiest class; every crisis scenario should hit
        it with at least -30%."""
        from src.analysis.stress_test import DEFAULT_SCENARIOS
        for s in DEFAULT_SCENARIOS:
            self.assertLessEqual(
                s.shocks["crypto"], -0.30,
                f"{s.name} crypto shock should be ≤ -30%",
            )


class StressPositionTest(unittest.TestCase):
    def test_single_position_equity_crash(self):
        from src.analysis.stress_test import stress_position, SCENARIO_2008_GFC
        ps = stress_position("SPY", notional_usd=100.0, scenario=SCENARIO_2008_GFC)
        self.assertEqual(ps.asset, "SPY")
        self.assertEqual(ps.asset_class, "equity_growth")
        self.assertAlmostEqual(ps.shock_pct, -0.38)
        self.assertAlmostEqual(ps.stressed_value_usd, 62.0, places=4)
        self.assertAlmostEqual(ps.pnl_usd, -38.0, places=4)

    def test_crypto_position_under_gfc(self):
        from src.analysis.stress_test import stress_position, SCENARIO_2008_GFC
        ps = stress_position("BTC-USD", notional_usd=50.0, scenario=SCENARIO_2008_GFC)
        self.assertEqual(ps.asset_class, "crypto")
        # 2008 GFC: synthetic crypto shock is -50%.
        self.assertAlmostEqual(ps.stressed_value_usd, 25.0, places=4)

    def test_cash_position_unchanged(self):
        from src.analysis.stress_test import stress_position, SCENARIO_2008_GFC
        ps = stress_position("USDT", notional_usd=100.0, scenario=SCENARIO_2008_GFC)
        self.assertEqual(ps.asset_class, "cash")
        self.assertEqual(ps.shock_pct, 0.0)
        self.assertEqual(ps.pnl_usd, 0.0)


class StressPortfolioTest(unittest.TestCase):
    def test_empty_portfolio(self):
        from src.analysis.stress_test import stress_portfolio, SCENARIO_2008_GFC
        r = stress_portfolio([], SCENARIO_2008_GFC)
        self.assertEqual(r.original_portfolio_usd, 0.0)
        self.assertEqual(r.stressed_portfolio_usd, 0.0)
        self.assertEqual(r.drawdown_pct, 0.0)
        self.assertEqual(r.per_position, [])

    def test_mixed_portfolio_under_gfc(self):
        from src.analysis.stress_test import stress_portfolio, SCENARIO_2008_GFC
        positions = [
            _FakePos("BTC-USD", 50.0),
            _FakePos("ETH-USD", 30.0),
            _FakePos("SPY", 100.0),
            _FakePos("GLD", 20.0),
        ]
        r = stress_portfolio(positions, SCENARIO_2008_GFC)
        # Expected per-asset stress:
        #   BTC: 50 * (1 + -0.50) = 25 → pnl -25
        #   ETH: 30 * (1 + -0.50) = 15 → pnl -15
        #   SPY: 100 * (1 + -0.38) = 62 → pnl -38
        #   GLD: 20 * (1 + -0.10) = 18 → pnl -2
        # Total original = 200, stressed = 120, pnl = -80, dd = -40%.
        self.assertAlmostEqual(r.original_portfolio_usd, 200.0)
        self.assertAlmostEqual(r.stressed_portfolio_usd, 120.0, places=2)
        self.assertAlmostEqual(r.total_pnl_usd, -80.0, places=2)
        self.assertAlmostEqual(r.drawdown_pct, -0.40, places=4)
        # Per-class pnl: crypto=-40, equity_growth=-38, commodity_safe=-2.
        self.assertAlmostEqual(r.per_asset_class_pnl["crypto"], -40.0, places=2)
        self.assertAlmostEqual(r.per_asset_class_pnl["equity_growth"], -38.0, places=2)
        self.assertAlmostEqual(r.per_asset_class_pnl["commodity_safe"], -2.0, places=2)

    def test_2022_rate_hikes_hits_crypto_hardest(self):
        from src.analysis.stress_test import stress_portfolio, SCENARIO_2022_RATE_HIKES
        positions = [
            _FakePos("BTC-USD", 50.0),
            _FakePos("QQQ", 50.0),
        ]
        r = stress_portfolio(positions, SCENARIO_2022_RATE_HIKES)
        # 2022: BTC -64%, QQQ -33% (worst year since 2008 for growth).
        # BTC: 50 * 0.36 = 18. QQQ: 50 * 0.67 = 33.5. Total pnl = -48.5.
        self.assertAlmostEqual(r.total_pnl_usd, -48.5, places=2)

    def test_skips_zero_notional(self):
        from src.analysis.stress_test import stress_portfolio, SCENARIO_2008_GFC
        positions = [
            _FakePos("BTC-USD", 0.0),
            _FakePos("SPY", 100.0),
        ]
        r = stress_portfolio(positions, SCENARIO_2008_GFC)
        self.assertEqual(len(r.per_position), 1)
        self.assertEqual(r.per_position[0].asset, "SPY")


class StressAllScenariosTest(unittest.TestCase):
    def test_runs_all_three_scenarios(self):
        from src.analysis.stress_test import stress_portfolio_all_scenarios
        positions = [_FakePos("BTC-USD", 50.0), _FakePos("SPY", 50.0)]
        results = stress_portfolio_all_scenarios(positions)
        self.assertEqual(len(results), 3)
        names = {r.scenario_name for r in results}
        self.assertEqual(names, {"2008_GFC", "2020_COVID", "2022_RATE_HIKES"})

    def test_worst_case_returns_most_negative(self):
        from src.analysis.stress_test import (
            stress_portfolio_all_scenarios, worst_case_drawdown,
        )
        positions = [_FakePos("BTC-USD", 60.0), _FakePos("QQQ", 40.0)]
        results = stress_portfolio_all_scenarios(positions)
        worst = worst_case_drawdown(results)
        # 2022 scenario: BTC -64% + QQQ -33% should be the worst.
        self.assertEqual(worst.scenario_name, "2022_RATE_HIKES")
        self.assertLess(worst.drawdown_pct, -0.40)


# ============================================================
# Sprint 44B — Tail risk (CVaR)
# ============================================================

class ValueAtRiskTest(unittest.TestCase):
    def test_basic_var(self):
        from src.analysis.tail_risk import value_at_risk
        # 100 returns with 10 extreme negatives. With 10/100 = 10% in
        # the tail, VaR 95% (5th percentile) clearly falls inside the
        # -0.10 cluster, not at the interpolation boundary.
        rets = [0.01] * 90 + [-0.10] * 10
        var = value_at_risk(rets, confidence=0.95)
        # VaR should equal the threshold of the worst 5% (the -0.10).
        self.assertLessEqual(var, -0.05)

    def test_var_99_more_extreme_than_95(self):
        from src.analysis.tail_risk import value_at_risk
        rets = list(np.linspace(-0.10, 0.05, 200))  # symmetric distribution
        var_95 = value_at_risk(rets, confidence=0.95)
        var_99 = value_at_risk(rets, confidence=0.99)
        # VaR 99% is at the 1st percentile, more extreme than VaR 95% (5th).
        self.assertLessEqual(var_99, var_95)

    def test_empty_returns(self):
        from src.analysis.tail_risk import value_at_risk
        self.assertEqual(value_at_risk([]), 0.0)


class ConditionalValueAtRiskTest(unittest.TestCase):
    def test_cvar_more_extreme_than_var(self):
        """CVaR is always ≤ VaR (more negative or equal)."""
        from src.analysis.tail_risk import value_at_risk, conditional_value_at_risk
        # Skewed distribution: lots of small positives, rare large negatives.
        rets = [0.01] * 90 + [-0.05] * 7 + [-0.20] * 3
        var = value_at_risk(rets, confidence=0.95)
        cvar = conditional_value_at_risk(rets, confidence=0.95)
        self.assertLessEqual(cvar, var)

    def test_cvar_99_captures_deeper_tail(self):
        from src.analysis.tail_risk import conditional_value_at_risk
        rets = [0.01] * 95 + [-0.10] * 4 + [-0.50] * 1
        cvar_95 = conditional_value_at_risk(rets, confidence=0.95)
        cvar_99 = conditional_value_at_risk(rets, confidence=0.99)
        # The -50% outlier should pull CVaR 99 deeper than CVaR 95.
        self.assertLess(cvar_99, cvar_95)

    def test_empty_returns(self):
        from src.analysis.tail_risk import conditional_value_at_risk
        self.assertEqual(conditional_value_at_risk([]), 0.0)


class PortfolioReturnsTest(unittest.TestCase):
    def test_equal_weights(self):
        from src.analysis.tail_risk import _portfolio_returns
        idx = pd.date_range("2025-01-01", periods=10, freq="D")
        a = pd.Series([0.01] * 10, index=idx)
        b = pd.Series([0.03] * 10, index=idx)
        port = _portfolio_returns({"A": a, "B": b}, {"A": 0.5, "B": 0.5})
        # 0.5 * 0.01 + 0.5 * 0.03 = 0.02 each day.
        for v in port.values:
            self.assertAlmostEqual(v, 0.02, places=9)

    def test_weights_normalized(self):
        """Non-normalized weights get normalized to sum to 1."""
        from src.analysis.tail_risk import _portfolio_returns
        idx = pd.date_range("2025-01-01", periods=5, freq="D")
        a = pd.Series([0.02] * 5, index=idx)
        b = pd.Series([0.0] * 5, index=idx)
        # Weights 60/40 sum to 100 → already normalized.
        port = _portfolio_returns({"A": a, "B": b}, {"A": 60, "B": 40})
        for v in port.values:
            self.assertAlmostEqual(v, 0.02 * 0.6, places=9)

    def test_assets_with_no_data_excluded(self):
        from src.analysis.tail_risk import _portfolio_returns
        idx = pd.date_range("2025-01-01", periods=5, freq="D")
        a = pd.Series([0.02] * 5, index=idx)
        port = _portfolio_returns({"A": a}, {"A": 0.5, "MISSING": 0.5})
        # The missing asset is dropped; 100% weight goes to A.
        for v in port.values:
            self.assertAlmostEqual(v, 0.02, places=9)

    def test_empty_input(self):
        from src.analysis.tail_risk import _portfolio_returns
        self.assertEqual(len(_portfolio_returns({}, {})), 0)


class ComputePortfolioTailRiskTest(unittest.TestCase):
    @patch("src.analysis.tail_risk.fetch_returns")
    def test_with_mocked_data(self, mock_fetch):
        from src.analysis.tail_risk import compute_portfolio_tail_risk
        # 60 days of returns: 2 assets, slightly different.
        idx = pd.date_range("2025-01-01", periods=60, freq="D")
        a = pd.Series(np.random.default_rng(0).normal(0.001, 0.02, 60), index=idx)
        b = pd.Series(np.random.default_rng(1).normal(0.001, 0.03, 60), index=idx)
        mock_fetch.return_value = {"BTC-USD": a, "SPY": b}
        result = compute_portfolio_tail_risk(
            {"BTC-USD": 60, "SPY": 40}, window_days=60,
        )
        self.assertEqual(set(result.assets), {"BTC-USD", "SPY"})
        self.assertGreater(result.n_observations, 0)
        # VaR and CVaR are negative (losses).
        self.assertLessEqual(result.var_95, 0.0)
        self.assertLessEqual(result.cvar_95, result.var_95)  # CVaR ≤ VaR
        self.assertLessEqual(result.cvar_99, result.cvar_95)  # CVaR 99 ≤ CVaR 95
        # Annual vol is positive.
        self.assertGreater(result.annual_volatility, 0.0)

    @patch("src.analysis.tail_risk.fetch_returns")
    def test_no_data_returns_zero(self, mock_fetch):
        from src.analysis.tail_risk import compute_portfolio_tail_risk
        mock_fetch.return_value = {}
        result = compute_portfolio_tail_risk({"BTC-USD": 100}, window_days=60)
        self.assertEqual(result.n_observations, 0)
        self.assertEqual(result.var_95, 0.0)
        self.assertEqual(result.cvar_95, 0.0)

    def test_empty_assets(self):
        from src.analysis.tail_risk import compute_portfolio_tail_risk
        result = compute_portfolio_tail_risk({})
        self.assertEqual(result.assets, [])
        self.assertEqual(result.n_observations, 0)


class CVaRSummaryTextTest(unittest.TestCase):
    def test_summary_includes_key_metrics(self):
        from src.analysis.tail_risk import TailRiskResult, cvar_summary_text
        r = TailRiskResult(
            assets=["BTC-USD", "SPY"], weights=[60.0, 40.0], n_observations=180,
            var_95=-0.020, var_99=-0.040, cvar_95=-0.030, cvar_99=-0.060,
            mean_daily_return=0.001, std_daily_return=0.02,
            annual_volatility=0.32, worst_single_day=-0.075,
        )
        text = cvar_summary_text(r)
        self.assertIn("-3.00%", text)  # CVaR 95
        self.assertIn("-6.00%", text)  # CVaR 99
        self.assertIn("-7.50%", text)  # worst day
        self.assertIn("CVaR 95%", text)

    def test_no_data_summary(self):
        from src.analysis.tail_risk import TailRiskResult, cvar_summary_text
        r = TailRiskResult(
            assets=[], weights=[], n_observations=0,
            var_95=0.0, var_99=0.0, cvar_95=0.0, cvar_99=0.0,
            mean_daily_return=0.0, std_daily_return=0.0,
            annual_volatility=0.0, worst_single_day=0.0,
        )
        self.assertEqual(cvar_summary_text(r), "tail_risk: no_data")


# ============================================================
# Sprint 44B — Allocation policy
# ============================================================

class AllocationPolicyTest(unittest.TestCase):
    def test_valid_policy(self):
        from src.data.asset_allocation import AllocationPolicy
        p = AllocationPolicy(
            targets={"crypto": 0.5, "equity_growth": 0.5},
            drift_tolerance_pct=5.0,
        )
        self.assertAlmostEqual(p.target_for("crypto"), 0.5)
        self.assertAlmostEqual(p.target_for("equity_growth"), 0.5)
        self.assertEqual(p.target_for("commodity_safe"), 0.0)

    def test_targets_must_sum_to_one(self):
        from src.data.asset_allocation import AllocationPolicy
        with self.assertRaises(ValueError):
            AllocationPolicy(targets={"crypto": 0.5, "equity_growth": 0.3})

    def test_negative_target_rejected(self):
        from src.data.asset_allocation import AllocationPolicy
        with self.assertRaises(ValueError):
            AllocationPolicy(targets={"crypto": -0.1, "equity_growth": 1.1})
        # Single-class policy: a negative value is still rejected even
        # if the sum would otherwise be 1.0 with absolute value.
        with self.assertRaises(ValueError):
            AllocationPolicy(targets={"crypto": -1.0})

    def test_drift_tolerance_bounds(self):
        from src.data.asset_allocation import AllocationPolicy
        with self.assertRaises(ValueError):
            AllocationPolicy(
                targets={"crypto": 1.0},
                drift_tolerance_pct=-1.0,
            )
        with self.assertRaises(ValueError):
            AllocationPolicy(
                targets={"crypto": 1.0},
                drift_tolerance_pct=60.0,  # > 50% max
            )

    def test_cap_and_floor(self):
        from src.data.asset_allocation import AllocationPolicy
        p = AllocationPolicy(
            targets={"crypto": 0.50, "equity_growth": 0.50},
            drift_tolerance_pct=10.0,
        )
        self.assertAlmostEqual(p.cap_for("crypto"), 0.60)
        self.assertAlmostEqual(p.floor_for("crypto"), 0.40)

    def test_cap_clamped_to_one(self):
        """If target + drift > 1.0, cap should clamp to 1.0."""
        from src.data.asset_allocation import AllocationPolicy
        p = AllocationPolicy(
            targets={"crypto": 0.95, "cash": 0.05},
            drift_tolerance_pct=10.0,
        )
        self.assertEqual(p.cap_for("crypto"), 1.0)

    def test_default_policy_balanced(self):
        """DEFAULT_POLICY must sum to 1.0 and be reasonable for the bot's universe."""
        from src.data.asset_allocation import DEFAULT_POLICY
        self.assertTrue(DEFAULT_POLICY.enabled)
        self.assertAlmostEqual(sum(DEFAULT_POLICY.targets.values()), 1.0)
        # All target classes are mapped to the bot's known symbols.
        self.assertEqual(
            set(DEFAULT_POLICY.targets.keys()),
            {"crypto", "equity_growth", "commodity_safe", "commodity_energy"},
        )


class CurrentActualWeightsTest(unittest.TestCase):
    def test_empty_book(self):
        from src.data.asset_allocation import current_actual_weights
        self.assertEqual(current_actual_weights([]), {})

    def test_single_class(self):
        from src.data.asset_allocation import current_actual_weights
        positions = [_FakePos("BTC-USD", 60.0), _FakePos("ETH-USD", 40.0)]
        w = current_actual_weights(positions)
        # 100% crypto, normalized.
        self.assertAlmostEqual(w["crypto"], 1.0)
        self.assertNotIn("equity_growth", w)

    def test_mixed_classes(self):
        from src.data.asset_allocation import current_actual_weights
        positions = [
            _FakePos("BTC-USD", 30.0),
            _FakePos("SPY", 50.0),
            _FakePos("GLD", 20.0),
        ]
        w = current_actual_weights(positions)
        self.assertAlmostEqual(w["crypto"], 0.30)
        self.assertAlmostEqual(w["equity_growth"], 0.50)
        self.assertAlmostEqual(w["commodity_safe"], 0.20)
        self.assertAlmostEqual(sum(w.values()), 1.0)

    def test_zero_notional_skipped(self):
        from src.data.asset_allocation import current_actual_weights
        positions = [_FakePos("BTC-USD", 0.0), _FakePos("SPY", 100.0)]
        w = current_actual_weights(positions)
        self.assertNotIn("crypto", w)
        self.assertAlmostEqual(w["equity_growth"], 1.0)


class DriftReportTest(unittest.TestCase):
    def test_no_drift(self):
        from src.data.asset_allocation import (
            AllocationPolicy, compute_drift,
        )
        policy = AllocationPolicy(
            targets={"crypto": 0.5, "equity_growth": 0.5},
        )
        report = compute_drift(
            {"crypto": 0.5, "equity_growth": 0.5},
            policy,
        )
        self.assertTrue(report.within_tolerance)
        self.assertAlmostEqual(report.max_abs_drift_pct, 0.0, places=6)

    def test_drift_over_cap(self):
        from src.data.asset_allocation import (
            AllocationPolicy, compute_drift,
        )
        policy = AllocationPolicy(
            targets={"crypto": 0.40, "equity_growth": 0.60},
            drift_tolerance_pct=5.0,
        )
        report = compute_drift(
            {"crypto": 0.50, "equity_growth": 0.50},
            policy,
        )
        self.assertFalse(report.within_tolerance)
        # crypto is at 50% vs target 40% — drift of 10% > 5% tolerance.
        self.assertIn("crypto", report.classes_over_cap)
        self.assertAlmostEqual(report.drifts["crypto"], 0.10, places=6)

    def test_drift_within_tolerance(self):
        from src.data.asset_allocation import (
            AllocationPolicy, compute_drift,
        )
        policy = AllocationPolicy(
            targets={"crypto": 0.40, "equity_growth": 0.60},
            drift_tolerance_pct=10.0,
        )
        report = compute_drift(
            {"crypto": 0.45, "equity_growth": 0.55},
            policy,
        )
        self.assertTrue(report.within_tolerance)
        # crypto drift 5% < 10% tolerance.

    def test_under_floor_detected(self):
        """If a target class is underweight vs its floor, it's flagged."""
        from src.data.asset_allocation import (
            AllocationPolicy, compute_drift,
        )
        policy = AllocationPolicy(
            targets={"crypto": 0.40, "equity_growth": 0.60},
            drift_tolerance_pct=5.0,
        )
        # crypto is at 30% (target 40%, floor 35% — under).
        report = compute_drift(
            {"crypto": 0.30, "equity_growth": 0.70},
            policy,
        )
        self.assertFalse(report.within_tolerance)
        self.assertIn("crypto", report.classes_under_floor)


class CheckTradeAgainstPolicyTest(unittest.TestCase):
    def _policy(self):
        from src.data.asset_allocation import AllocationPolicy
        return AllocationPolicy(
            targets={"crypto": 0.40, "equity_growth": 0.40,
                     "commodity_safe": 0.10, "commodity_energy": 0.10},
            drift_tolerance_pct=10.0,
        )

    def setUp(self):
        # Module-level imports shared by all tests in this class.
        from src.data.asset_allocation import check_trade_against_policy
        self.check_trade_against_policy = check_trade_against_policy

    def test_disabled_policy_allows_everything(self):
        from src.data.asset_allocation import (
            AllocationPolicy, check_trade_against_policy,
        )
        policy = AllocationPolicy(enabled=False)
        ok, reason = self.check_trade_against_policy(
            "BTC-USD", proposed_notional_usd=1000.0,
            current_positions=[], policy=policy,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "policy_disabled")

    def test_empty_book_allows_first_trade(self):
        policy = self._policy()
        ok, reason = self.check_trade_against_policy(
            "BTC-USD", proposed_notional_usd=100.0,
            current_positions=[], policy=policy,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "empty_book")

    def test_cash_class_skipped(self):
        policy = self._policy()
        ok, reason = self.check_trade_against_policy(
            "USDT", proposed_notional_usd=50.0,
            current_positions=[_FakePos("BTC-USD", 100.0)],
            policy=policy,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "cash_class_skipped")

    def test_zero_notional_skipped(self):
        policy = self._policy()
        ok, reason = self.check_trade_against_policy(
            "BTC-USD", proposed_notional_usd=0.0,
            current_positions=[_FakePos("SPY", 50.0)],
            policy=policy,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "zero_notional_skipped")

    def test_within_policy_allows(self):
        """Adding to a class that stays under its cap is allowed."""
        policy = self._policy()
        positions = [
            _FakePos("BTC-USD", 30.0),
            _FakePos("SPY", 70.0),
        ]
        # Adding 10 of ETH (crypto). Pre: 30/100 = 30%. Post: 40/110 = 36.4%.
        # Cap is 50% → well under. Allowed.
        ok, reason = self.check_trade_against_policy(
            "ETH-USD", proposed_notional_usd=10.0,
            current_positions=positions, policy=policy,
        )
        self.assertTrue(ok, f"got {reason}")

    def test_breach_rejects(self):
        """Adding to a class that pushes it above the cap is rejected."""
        policy = self._policy()
        # Pre: 30 crypto, 70 equity (out of policy already, but the cap is
        # the cap). Post-adding 30 more crypto: 60 crypto / 130 = 46.2%.
        # Still under 50% cap... let me make it bigger.
        positions = [
            _FakePos("BTC-USD", 30.0),
            _FakePos("SPY", 50.0),
        ]
        # Adding 30 SOL: crypto=60, total=110, crypto%=54.5% > 50% cap.
        ok, reason = self.check_trade_against_policy(
            "SOL-USD", proposed_notional_usd=30.0,
            current_positions=positions, policy=policy,
        )
        self.assertFalse(ok)
        self.assertIn("allocation_policy_crypto", reason)
        self.assertIn("exceeds_50pct_cap", reason)

    def test_diversifying_trade_allowed(self):
        """Adding a different class to a crypto-heavy book is always OK."""
        policy = self._policy()
        positions = [
            _FakePos("BTC-USD", 50.0),
            _FakePos("ETH-USD", 50.0),
        ]
        # crypto is 100% (already blown). Adding GLD (commodity_safe) →
        # crypto=100/120=83%, commodity_safe=20/120=17% (>10% cap!).
        # Hmm, GLD itself would breach. Let me pick something that doesn't.
        # Tweak: target_commodity_safe 0.20, drift 10 → cap 0.30.
        # 20/120 = 16.7% < 30% cap. OK.
        policy2 = policy.__class__(
            targets={"crypto": 0.30, "equity_growth": 0.30,
                     "commodity_safe": 0.20, "commodity_energy": 0.20},
            drift_tolerance_pct=10.0,
        )
        ok, reason = self.check_trade_against_policy(
            "GLD", proposed_notional_usd=20.0,
            current_positions=positions, policy=policy2,
        )
        self.assertTrue(ok, f"got {reason}")


# ============================================================
# Sprint 44B — risk_agent allocation gate integration
# ============================================================

class AllocationGateIntegrationTest(unittest.TestCase):
    def _make_risk(self, opens, policy=None):
        from src.agents.risk_agent import RiskManagerAgent
        from src.data_store.positions import PositionRepository
        from src.data.asset_allocation import DEFAULT_POLICY
        import tempfile
        import dataclasses
        tmpdir = tempfile.mkdtemp()
        repo = PositionRepository(path=os.path.join(tmpdir, "positions.json"))
        for p in opens:
            repo.positions.append(p)
        # IMPORTANT: copy DEFAULT_POLICY if no policy passed, so tests that
        # mutate `agent.allocation_policy.enabled = False` don't poison
        # the global singleton for other tests.
        if policy is None:
            policy = dataclasses.replace(DEFAULT_POLICY)
        return RiskManagerAgent(
            position_repo=repo,
            min_order_usd=10.0,
            max_open_trades=5,
            max_capital_per_trade_pct=50,
            risk_per_trade_pct=1.0,
            asset_concentration_check=False,  # isolate the new gate
            allocation_policy=policy,
        )

    def test_default_policy_used_when_none_passed(self):
        """Constructor with no policy should use DEFAULT_POLICY (enabled)."""
        from src.agents.risk_agent import RiskManagerAgent
        from src.data.asset_allocation import DEFAULT_POLICY
        agent = RiskManagerAgent(position_repo=None)
        self.assertIsNotNone(agent.allocation_policy)
        self.assertTrue(agent.allocation_policy.enabled)

    def test_check_allocation_returns_policy_decision(self):
        from src.data_store.positions import Position
        import time
        opens = [
            Position(
                asset="BTC-USD", direction="long",
                entry_price=50000, stop_loss=49000, take_profit=52000,
                qty=0.001, risk_usd=10, entry_ts=time.time(), strategy="test",
            ),
            Position(
                asset="SPY", direction="long",
                entry_price=400, stop_loss=395, take_profit=420,
                qty=0.075, risk_usd=4, entry_ts=time.time(), strategy="test",
            ),
        ]
        risk = self._make_risk(opens=opens)
        # crypto=50, total=80, crypto%=62.5%. Adding SOL $20 → 70/100=70%.
        # Default policy: crypto target 40%, drift 10% → cap 50%.
        # 70% > 50% → reject.
        ok, reason = risk._check_allocation("SOL-USD", proposed_notional_usd=20.0)
        self.assertFalse(ok)
        self.assertIn("allocation_policy_crypto", reason)
        self.assertIn("exceeds_50pct_cap", reason)

    def test_check_allocation_disabled(self):
        risk = self._make_risk(opens=[])
        risk.allocation_policy.enabled = False
        ok, reason = risk._check_allocation("BTC-USD", proposed_notional_usd=100.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "policy_disabled")

    def test_allocation_runs_before_concentration(self):
        """If both gates are enabled, allocation is the primary signal.
        A trade can pass the 44A concentration cap (60%) but fail the
        44B allocation drift (50% with 40% target)."""
        from src.data_store.positions import Position
        import time
        from src.agents.risk_agent import RiskManagerAgent
        from src.data_store.positions import PositionRepository
        from src.data.asset_allocation import AllocationPolicy
        import tempfile
        tmpdir = tempfile.mkdtemp()
        repo = PositionRepository(path=os.path.join(tmpdir, "positions.json"))
        # 2 crypto positions, 1 equity — total 90, crypto 60 (67%).
        repo.positions.append(Position(
            asset="BTC-USD", direction="long",
            entry_price=50000, stop_loss=49000, take_profit=52000,
            qty=0.0006, risk_usd=6, entry_ts=time.time(), strategy="test",
        ))
        repo.positions.append(Position(
            asset="ETH-USD", direction="long",
            entry_price=3000, stop_loss=2900, take_profit=3200,
            qty=0.01, risk_usd=10, entry_ts=time.time(), strategy="test",
        ))
        repo.positions.append(Position(
            asset="SPY", direction="long",
            entry_price=400, stop_loss=395, take_profit=420,
            qty=0.075, risk_usd=4, entry_ts=time.time(), strategy="test",
        ))
        # Tight policy: crypto 30%, drift 5% → cap 35%.
        policy = AllocationPolicy(
            targets={"crypto": 0.30, "equity_growth": 0.50,
                     "commodity_safe": 0.10, "commodity_energy": 0.10},
            drift_tolerance_pct=5.0,
        )
        agent = RiskManagerAgent(
            position_repo=repo,
            min_order_usd=10.0,
            max_open_trades=5,
            max_capital_per_trade_pct=50,
            risk_per_trade_pct=1.0,
            asset_concentration_check=True,
            max_asset_class_concentration_pct=80.0,  # permissive
            allocation_policy=policy,
        )
        # Adding SOL $5 (more crypto): 65/95 = 68%. Over 35% cap → reject.
        ok_alloc, reason_alloc = agent._check_allocation("SOL-USD", proposed_notional_usd=5.0)
        ok_conc, reason_conc = agent._check_concentration("SOL-USD", proposed_notional_usd=5.0)
        self.assertFalse(ok_alloc)
        self.assertTrue(ok_conc, f"concentration should allow; got {reason_conc}")


if __name__ == "__main__":
    unittest.main()
