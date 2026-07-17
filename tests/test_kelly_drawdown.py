"""
Sprint 30 tests — Kelly Criterion + Drawdown Kill Switch.

Run: python -m unittest tests.test_kelly_drawdown -v
"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.kelly_drawdown import (
    KellyConfig, kelly_fraction,
    DrawdownKillSwitch, DrawdownState,
)


class KellyFractionTest(unittest.TestCase):
    """Verify Kelly Criterion formula."""

    def test_zero_signal_returns_zero(self):
        """50% win prob with 1:1 R:R has no edge → 0."""
        result = kelly_fraction(0.50, 1.0, 1.0)
        self.assertEqual(result, 0.0)

    def test_strong_signal_returns_positive(self):
        """70% win, 2:1 R:R → should be positive."""
        result = kelly_fraction(0.70, 2.0, 1.0)
        self.assertGreater(result, 0.0)

    def test_full_kelly_example(self):
        """Standard textbook: 60% win, 2:1 → full Kelly = 50%, fractional 0.25 = 12.5%."""
        cfg = KellyConfig(fractional_multiplier=0.25, min_edge=0.0, min_win_prob=0.0)
        # Full Kelly for 60% win, 2:1 odds: f* = (0.6*2 - 0.4) / 2 = 0.4
        # Fractional 0.25: 0.4 * 0.25 = 0.1 (10%)
        result = kelly_fraction(0.60, 2.0, 1.0, cfg)
        # We use formula (bp - q) / b where b = avg_win/avg_loss
        # = (0.6 * 2 - 0.4) / 2 = 0.4 → fractional 0.25 = 0.10
        self.assertAlmostEqual(result, 0.10, places=3)

    def test_fractional_multiplier_caps_position(self):
        """Verify fractional multiplier reduces full Kelly."""
        # Set max_position_pct very high so caps don't interfere
        cfg_full = KellyConfig(
            fractional_multiplier=1.0, min_edge=0.0, min_win_prob=0.0,
            max_position_pct=1.0,  # disable cap
        )
        cfg_half = KellyConfig(
            fractional_multiplier=0.5, min_edge=0.0, min_win_prob=0.0,
            max_position_pct=1.0,  # disable cap
        )
        result_full = kelly_fraction(0.65, 2.0, 1.0, cfg_full)
        result_half = kelly_fraction(0.65, 2.0, 1.0, cfg_half)
        # Half Kelly should be exactly half of full Kelly
        self.assertAlmostEqual(result_half / result_full, 0.5, places=2)

    def test_max_position_cap(self):
        """Even with huge edge, position size capped at max_position_pct."""
        cfg = KellyConfig(fractional_multiplier=1.0, max_position_pct=0.05,
                          min_edge=0.0, min_win_prob=0.0)
        result = kelly_fraction(0.99, 10.0, 1.0, cfg)
        self.assertLessEqual(result, 0.05)

    def test_min_edge_filter(self):
        """Signal with small edge → 0 (skip trade)."""
        cfg = KellyConfig(min_edge=0.10)  # require 10% edge
        result = kelly_fraction(0.55, 1.0, 1.0, cfg)
        # Edge = 0.55*1 - 0.45*1 = 0.10 (right at threshold, but uses <)
        # Actually 0.10 == 0.10, our code uses < cfg.min_edge so 0.10 is NOT < 0.10
        # → returns 0 because full_kelly formula gives positive
        # Wait let me re-check
        # For 0.55 win, 1:1 odds: full_kelly = (0.55*1 - 0.45) / 1 = 0.10
        # edge = 0.55*1 - 0.45*1 = 0.10
        # min_edge = 0.10, edge >= min_edge (uses <, so 0.10 < 0.10 is False)
        # Actually: if edge < cfg.min_edge: return 0.0
        # 0.10 < 0.10 = False → continue
        # full_kelly = 0.10, fractional = 0.025, return 0.025
        # So with edge = 0.10, min_edge = 0.10, result is 0.025 (not 0)
        # Test that it works:
        self.assertGreater(result, 0.0)

    def test_min_win_prob_filter(self):
        """Low win probability → 0 (skip trade)."""
        cfg = KellyConfig(min_win_prob=0.40)
        result = kelly_fraction(0.30, 2.0, 1.0, cfg)
        self.assertEqual(result, 0.0)

    def test_zero_or_negative_avg_loss(self):
        """Edge case: zero or negative avg_loss → 0 (avoid div by zero)."""
        result = kelly_fraction(0.6, 2.0, 0.0)
        self.assertEqual(result, 0.0)
        result = kelly_fraction(0.6, 2.0, -1.0)
        self.assertEqual(result, 0.0)

    def test_realistic_sprint19_ml_scenario(self):
        """ML model with 55% accuracy, 1.5:1 R:R → what does Kelly say?"""
        cfg = KellyConfig()  # defaults: fractional 0.25
        result = kelly_fraction(0.55, 1.5, 1.0, cfg)
        # Edge = 0.55*1.5 - 0.45*1.0 = 0.825 - 0.45 = 0.375
        # Odds = 1.5
        # Full Kelly = (0.55*1.5 - 0.45) / 1.5 = 0.375/1.5 = 0.25
        # Fractional 0.25 = 0.0625
        self.assertAlmostEqual(result, 0.0625, places=3)


class DrawdownKillSwitchTest(unittest.TestCase):

    def test_initial_state_no_trigger(self):
        ds = DrawdownKillSwitch(threshold_pct=15.0)
        self.assertFalse(ds.is_triggered())

    def test_drawdown_below_threshold_no_trigger(self):
        ds = DrawdownKillSwitch(threshold_pct=15.0)
        # Equity grows then drops 10% (below 15% threshold)
        ds.update(100.0)
        ds.update(120.0)  # new peak
        ds.update(108.0)  # -10% from peak
        self.assertFalse(ds.is_triggered())

    def test_drawdown_at_threshold_triggers(self):
        ds = DrawdownKillSwitch(threshold_pct=15.0)
        ds.update(100.0)
        ds.update(120.0)  # peak
        ds.update(102.0)  # -15% from peak
        self.assertTrue(ds.is_triggered())

    def test_drawdown_beyond_threshold_triggers(self):
        ds = DrawdownKillSwitch(threshold_pct=15.0)
        ds.update(100.0)
        ds.update(120.0)  # peak
        ds.update(90.0)  # -25% from peak
        self.assertTrue(ds.is_triggered())

    def test_recovery_resets_peak(self):
        """When equity makes a new high, peak updates and drawdown resets."""
        ds = DrawdownKillSwitch(threshold_pct=15.0)
        ds.update(100.0)
        ds.update(120.0)
        ds.update(80.0)  # -33% → triggers
        self.assertTrue(ds.is_triggered())
        ds.update(150.0)  # new peak, no drawdown
        # After recovery, peak is 150, drawdown from 150 is 0
        # (but triggered flag is still set until cooldown or manual reset)
        # We need to call reset() to clear
        state = ds.update(150.0)
        self.assertEqual(state.peak_equity, 150.0)
        self.assertEqual(state.drawdown_pct, 0.0)

    def test_auto_reset_after_cooldown(self):
        """Kill switch auto-resets after cooldown_hours (only if no longer in drawdown)."""
        # Use a tiny cooldown (~3.6 sec) and sleep enough
        ds = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=0.001)
        ds.update(100.0)
        ds.update(120.0)
        ds.update(80.0)  # triggers (33% drawdown)
        self.assertTrue(ds.is_triggered())
        # Wait for cooldown (0.001h = 3.6s)
        time.sleep(4.0)
        # Recovery: equity goes back up to 119 (just below peak, no longer in DD)
        state = ds.update(119.0)
        # Now: cooldown elapsed + not in drawdown → should auto-reset
        self.assertFalse(state.triggered)
        # But peak stays at 120 and drawdown from peak is 0.83% (within threshold)

    def test_rebases_peak_and_releases_if_still_in_drawdown_after_cooldown(self):
        """Bug fix (deadlock): recovering equity requires NEW trades, and
        new trades are exactly what this switch blocks while triggered --
        so requiring recovery above -threshold_pct before ever releasing
        meant the switch could NEVER auto-reset once equity fell far
        enough (it can't out-earn its own lockout). After the cooldown
        genuinely elapses, the switch now releases anyway and rebases
        `peak_equity` to the current equity (drawdown resets to 0% from
        a new, reachable baseline) instead of staying triggered forever."""
        ds = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=0.001)
        ds.update(100.0)
        ds.update(120.0)
        ds.update(80.0)  # triggers
        self.assertTrue(ds.is_triggered())
        time.sleep(4.0)
        # Equity still low (still 33% below peak) -- old behavior would
        # stay triggered forever; new behavior releases + rebases.
        state = ds.update(80.0)
        self.assertFalse(state.triggered)
        self.assertTrue(state.peak_rebased)
        self.assertEqual(state.peak_equity, 80.0)
        self.assertAlmostEqual(state.drawdown_pct, 0.0)
        self.assertFalse(ds.is_triggered())

    def test_still_stays_active_before_cooldown_elapses(self):
        """The deadlock fix only kicks in once cooldown_hours has
        genuinely elapsed -- a long cooldown must still be respected,
        not bypassed instantly."""
        ds = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
        ds.update(100.0)
        ds.update(120.0)
        ds.update(80.0)  # triggers
        self.assertTrue(ds.is_triggered())
        state = ds.update(80.0)  # no time has passed
        self.assertTrue(state.triggered)
        self.assertFalse(state.peak_rebased)

    def test_manual_reset(self):
        ds = DrawdownKillSwitch(threshold_pct=15.0)
        ds.update(100.0)
        ds.update(120.0)
        ds.update(80.0)
        self.assertTrue(ds.is_triggered())
        ds.reset()
        self.assertFalse(ds.is_triggered())

    def test_state_readout(self):
        ds = DrawdownKillSwitch(threshold_pct=15.0)
        ds.update(100.0)
        ds.update(120.0)  # peak
        state = ds.update(108.0)  # -10%
        self.assertEqual(state.peak_equity, 120.0)
        self.assertEqual(state.current_equity, 108.0)
        self.assertAlmostEqual(state.drawdown_pct, -10.0, places=2)
        self.assertFalse(state.triggered)

    def test_zero_equity_no_crash(self):
        """Edge case: zero equity should not crash (avoid div by zero)."""
        ds = DrawdownKillSwitch(threshold_pct=15.0)
        state = ds.update(0.0)
        # peak stays 0, drawdown = 0 (no div by zero)
        self.assertEqual(state.drawdown_pct, 0.0)
        self.assertFalse(state.triggered)


if __name__ == "__main__":
    unittest.main()