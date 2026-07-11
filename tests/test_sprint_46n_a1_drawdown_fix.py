"""
Sprint 46N tests — audit finding A1 (AUDITORIA_COMPLETA_2026-07-11.md).

A1: the drawdown kill switch had three compounding bugs:

1. "Missing price = 100% loss" -- main.py's job_with_monitor built its
   own `current_equity` using `prices.get(pos.asset, 0.0)`, so a
   single failed price fetch for one asset made that position's
   unrealized P&L become `-entry_price * qty` (a fabricated ~100%
   loss), not $0. This produced the impossible -264%/-212% drawdown
   alerts observed in production.
2. "Wrong equity base" -- the equity value fed to DrawdownKillSwitch
   was pure cumulative P&L (starting near 0), not real account equity
   (starting balance + P&L), so drawdown_pct was hugely exaggerated
   relative to the real dollar move.
3. "Not persisted" -- DrawdownKillSwitch.peak_equity/triggered/
   triggered_at lived only in memory; every bot restart silently reset
   the peak to 0.0 and cleared any active trigger.

Fix: main.py now feeds DrawdownKillSwitch the SAME EquityTracker
snapshot already computed correctly elsewhere (starting_balance +
realized + unrealized, with missing-price positions contributing $0
via EquityTracker.update()'s `if price is not None` guard) instead of
recomputing a broken value. DrawdownKillSwitch gained persist()/load()
so peak_equity/triggered/triggered_at survive a restart.

This test file covers (2) and (3) directly on DrawdownKillSwitch/
EquityTracker in isolation, since main.py's job_with_monitor is a
closure that can't be unit-tested without running the whole bot.

Run: python -m unittest tests.test_sprint_46n_a1_drawdown_fix -v
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.kelly_drawdown import DrawdownKillSwitch
from src.safety.equity_tracker import EquityTracker
from src.data_store.positions import Position, PositionRepository


def _make_open_position(asset="BTC-USD", entry_price=50000.0, qty=0.001):
    return Position(
        asset=asset, direction="long", entry_price=entry_price,
        stop_loss=entry_price * 0.98, take_profit=entry_price * 1.04,
        qty=qty, risk_usd=10.0, entry_ts=1000.0, strategy="test",
    )


class MissingPriceNoLongerFabricatesLossTest(unittest.TestCase):
    """EquityTracker (the corrected equity source main.py now feeds
    the drawdown switch) must NOT treat a missing price as "asset
    worth $0" -- unlike the old job_with_monitor computation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(path=os.path.join(self.tmpdir, "positions.json"))

    def test_missing_price_contributes_zero_not_full_loss(self):
        self.repo.add_open(_make_open_position(asset="BTC-USD", entry_price=50000.0, qty=0.001))
        tracker = EquityTracker(starting_balance=100.0, position_repo=self.repo)
        # No price supplied for BTC-USD at all -- simulates a failed fetch.
        snap = tracker.update(current_prices={})
        self.assertEqual(snap.unrealized_pnl, 0.0)
        self.assertEqual(snap.total_equity, 100.0)

    def test_equity_base_includes_starting_balance(self):
        """The old bug's equity base was pure PnL (~0); the fix's base
        is starting_balance + PnL, so a small dollar move produces a
        sane percentage instead of an exaggerated one."""
        self.repo.add_open(_make_open_position(asset="BTC-USD", entry_price=50000.0, qty=0.001))
        tracker = EquityTracker(starting_balance=1000.0, position_repo=self.repo)
        # BTC drops slightly -- realistic small loss.
        snap = tracker.update(current_prices={"BTC-USD": 49500.0})
        expected_unrealized = (49500.0 - 50000.0) * 0.001  # -0.5
        self.assertAlmostEqual(snap.unrealized_pnl, expected_unrealized, places=6)
        self.assertAlmostEqual(snap.total_equity, 1000.0 + expected_unrealized, places=6)
        # A tiny -$0.50 move on a $1000 account must NOT look like a
        # catastrophic drawdown.
        self.assertGreater(snap.total_equity / 1000.0, 0.99)


class DrawdownKillSwitchPersistenceTest(unittest.TestCase):
    """A1: peak_equity/triggered/triggered_at must survive a restart."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "drawdown_kill_state.json")

    def test_load_with_no_file_returns_fresh_switch(self):
        switch = DrawdownKillSwitch.load(self.path, threshold_pct=15.0, cooldown_hours=24.0)
        self.assertEqual(switch.peak_equity, 0.0)
        self.assertFalse(switch.triggered)
        self.assertIsNone(switch.triggered_at)
        self.assertEqual(switch.threshold_pct, 15.0)
        self.assertEqual(switch.cooldown_hours, 24.0)

    def test_persist_then_load_restores_peak_equity(self):
        switch = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
        switch.update(1000.0)
        switch.update(1050.0)  # new peak
        switch.persist(self.path)

        reloaded = DrawdownKillSwitch.load(self.path, threshold_pct=15.0, cooldown_hours=24.0)
        self.assertEqual(reloaded.peak_equity, 1050.0)
        self.assertFalse(reloaded.triggered)

    def test_persist_then_load_restores_triggered_state(self):
        switch = DrawdownKillSwitch(threshold_pct=10.0, cooldown_hours=24.0)
        switch.update(1000.0)          # peak = 1000
        state = switch.update(880.0)   # -12% drawdown -- triggers
        self.assertTrue(state.triggered)
        switch.persist(self.path)

        reloaded = DrawdownKillSwitch.load(self.path, threshold_pct=10.0, cooldown_hours=24.0)
        self.assertTrue(reloaded.triggered)
        self.assertIsNotNone(reloaded.triggered_at)
        self.assertEqual(reloaded.peak_equity, 1000.0)
        # A restart must not silently un-trigger a real active kill switch.
        self.assertTrue(reloaded.is_triggered())

    def test_load_uses_current_config_not_persisted_config(self):
        """threshold_pct/cooldown_hours always come from the caller
        (current config), never get frozen into the persisted file."""
        switch = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
        switch.update(1000.0)
        switch.persist(self.path)

        # Simulate a config change between restarts -- new threshold.
        reloaded = DrawdownKillSwitch.load(self.path, threshold_pct=5.0, cooldown_hours=48.0)
        self.assertEqual(reloaded.threshold_pct, 5.0)
        self.assertEqual(reloaded.cooldown_hours, 48.0)
        # Peak equity state is still restored.
        self.assertEqual(reloaded.peak_equity, 1000.0)

    def test_load_with_corrupt_file_fails_open(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not valid json{{{")
        switch = DrawdownKillSwitch.load(self.path, threshold_pct=15.0, cooldown_hours=24.0)
        self.assertEqual(switch.peak_equity, 0.0)
        self.assertFalse(switch.triggered)

    def test_persist_is_atomic_write(self):
        switch = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
        switch.update(500.0)
        switch.persist(self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
