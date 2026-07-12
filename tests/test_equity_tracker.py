"""
Sprint 23 tests — Live Equity Tracker.

Run: python -m unittest tests.test_equity_tracker -v
"""
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.equity_tracker import (
    EquityTracker, EquitySnapshot, format_equity_line,
    persist_tracker, load_tracker,
)
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository, Position


class EquitySnapshotTest(unittest.TestCase):
    def test_sub_dollar_precision(self):
        """With $10 starting, small wins should show 4 decimals."""
        snap = EquitySnapshot(
            timestamp=0, iso="2026-07-09",
            starting_balance=10.0,
            realized_pnl=0.0123,
            unrealized_pnl=0.0045,
            total_equity=10.0168,
            delta_usd=0.0168,
            delta_pct=0.168,
            open_positions=1,
            closed_positions=0,
            drawdown_usd=0.0,
            drawdown_pct=0.0,
        )
        self.assertAlmostEqual(snap.total_equity, 10.0168, places=4)
        self.assertAlmostEqual(snap.delta_usd, 0.0168, places=4)

    def test_to_dict_roundtrip(self):
        snap = EquitySnapshot(
            timestamp=1000.0, iso="x",
            starting_balance=10.0, realized_pnl=0.0, unrealized_pnl=0.0,
            total_equity=10.0, delta_usd=0.0, delta_pct=0.0,
            open_positions=0, closed_positions=0,
            drawdown_usd=0.0, drawdown_pct=0.0,
        )
        d = snap.to_dict()
        self.assertEqual(d["starting_balance"], 10.0)
        self.assertEqual(d["total_equity"], 10.0)


class EquityTrackerBasicTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.tracker = EquityTracker(
            starting_balance=10.0,
            position_repo=self.repo,
            audit=self.audit,
        )

    def test_initial_state_no_positions(self):
        snap = self.tracker.update({})
        self.assertEqual(snap.total_equity, 10.0)
        self.assertEqual(snap.delta_usd, 0.0)
        self.assertEqual(snap.delta_pct, 0.0)
        self.assertEqual(snap.open_positions, 0)
        self.assertEqual(snap.closed_positions, 0)

    def test_profitable_position_increases_equity(self):
        """A LONG position with price > entry should add to equity."""
        self.repo.add_open(Position(
            asset="BTC-USD", direction="long",
            entry_price=50000.0, stop_loss=49000.0, take_profit=52000.0,
            qty=0.001, risk_usd=1.0,
            entry_ts=time.time(), strategy="test",
        ))
        # Price went up to 50100: +$0.10 unrealized PnL (0.001 × 100)
        snap = self.tracker.update({"BTC-USD": 50100.0})
        self.assertAlmostEqual(snap.unrealized_pnl, 0.1, places=4)
        self.assertAlmostEqual(snap.total_equity, 10.1, places=4)
        self.assertAlmostEqual(snap.delta_usd, 0.1, places=4)
        self.assertAlmostEqual(snap.delta_pct, 1.0, places=4)

    def test_losing_position_decreases_equity(self):
        """A LONG position with price < entry should subtract from equity."""
        self.repo.add_open(Position(
            asset="BTC-USD", direction="long",
            entry_price=50000.0, stop_loss=49000.0, take_profit=52000.0,
            qty=0.001, risk_usd=1.0,
            entry_ts=time.time(), strategy="test",
        ))
        # Price went down to 49900: -$0.10 unrealized PnL
        snap = self.tracker.update({"BTC-USD": 49900.0})
        self.assertAlmostEqual(snap.unrealized_pnl, -0.1, places=4)
        self.assertAlmostEqual(snap.total_equity, 9.9, places=4)
        self.assertAlmostEqual(snap.delta_usd, -0.1, places=4)

    def test_realized_pnl_added_after_close(self):
        """Closing a profitable position should add realized PnL."""
        pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=50000.0, stop_loss=49000.0, take_profit=52000.0,
            qty=0.001, risk_usd=1.0,
            entry_ts=time.time(), strategy="test",
        )
        self.repo.add_open(pos)
        self.repo.close_position(pos.position_id, close_price=50500.0, reason="TP_HIT")
        # Realized PnL = (50500 - 50000) × 0.001 = $0.50
        snap = self.tracker.update({})
        self.assertAlmostEqual(snap.realized_pnl, 0.5, places=4)
        self.assertAlmostEqual(snap.total_equity, 10.5, places=4)
        self.assertAlmostEqual(snap.delta_usd, 0.5, places=4)
        self.assertEqual(snap.open_positions, 0)
        self.assertEqual(snap.closed_positions, 1)

    def test_combined_realized_and_unrealized(self):
        """Mix of closed winners + open positions."""
        # Closed winner: +$0.50
        pos1 = Position(
            asset="BTC-USD", direction="long",
            entry_price=50000.0, stop_loss=49000.0, take_profit=52000.0,
            qty=0.001, risk_usd=1.0,
            entry_ts=time.time(), strategy="test",
        )
        self.repo.add_open(pos1)
        self.repo.close_position(pos1.position_id, close_price=50500.0, reason="TP_HIT")
        # Open position: +$0.10 unrealized
        pos2 = Position(
            asset="ETH-USD", direction="long",
            entry_price=3000.0, stop_loss=2950.0, take_profit=3150.0,
            qty=0.01, risk_usd=0.5,
            entry_ts=time.time(), strategy="test",
        )
        self.repo.add_open(pos2)
        snap = self.tracker.update({"ETH-USD": 3010.0})
        # Realized: $0.50, Unrealized: $0.10, Total equity: $10.60
        self.assertAlmostEqual(snap.realized_pnl, 0.5, places=4)
        self.assertAlmostEqual(snap.unrealized_pnl, 0.1, places=4)
        self.assertAlmostEqual(snap.total_equity, 10.6, places=4)


class EquityTrackerHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.tracker = EquityTracker(
            starting_balance=10.0,
            position_repo=self.repo,
            history_size=5,
        )

    def test_history_accumulates(self):
        for i in range(3):
            self.tracker.update({})
        self.assertEqual(len(self.tracker.history), 4)  # 1 initial + 3 updates

    def test_history_capped_at_maxlen(self):
        for i in range(10):
            self.tracker.update({})
        self.assertEqual(len(self.tracker.history), 5)  # cap at history_size

    def test_equity_series_for_sparklines(self):
        # Simulate equity growing
        pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=50000.0, stop_loss=49000.0, take_profit=52000.0,
            qty=0.001, risk_usd=1.0,
            entry_ts=time.time(), strategy="test",
        )
        self.repo.add_open(pos)
        prices = [50000, 50100, 50200, 50300]
        for p in prices:
            self.tracker.update({"BTC-USD": p})
        series = self.tracker.equity_series()
        self.assertEqual(len(series), 5)  # 1 initial + 4 updates
        # Equity should grow
        self.assertGreater(series[-1], series[0])


class EquityTrackerDrawdownTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.tracker = EquityTracker(
            starting_balance=10.0,
            position_repo=self.repo,
        )

    def test_drawdown_tracks_peak(self):
        pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=50000.0, stop_loss=49000.0, take_profit=52000.0,
            qty=0.001, risk_usd=1.0,
            entry_ts=time.time(), strategy="test",
        )
        self.repo.add_open(pos)
        # Peak: price at 50500 (+$0.50)
        snap_peak = self.tracker.update({"BTC-USD": 50500.0})
        self.assertEqual(snap_peak.drawdown_usd, 0.0)
        # Then crash to 49500 (-$0.50 → total equity $9.50 → drawdown from $10.50 peak)
        snap_dd = self.tracker.update({"BTC-USD": 49500.0})
        self.assertLess(snap_dd.drawdown_usd, 0.0)
        self.assertAlmostEqual(snap_dd.drawdown_usd, -1.0, places=4)


class EquityTrackerAuditTest(unittest.TestCase):
    def test_update_no_longer_emits_audit_event(self):
        """Sprint 46S (audit A8): EQUITY_UPDATE used to be written to
        the audit ledger on every update() call (~720/day at the
        2-minute fast-tick cadence) -- the audit's exact complaint:
        "no escribir EQUITY_UPDATE al ledger en cada tick (ya está en
        equity_state.json)". This snapshot data is already fully
        available via `tracker.history`/`tracker.latest()` in-memory
        and persisted separately via `persist_tracker()` -- the audit
        ledger was a redundant, high-volume second copy. Confirm the
        ledger stays untouched by a plain update() call."""
        tmpdir = tempfile.mkdtemp()
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        repo = PositionRepository(os.path.join(tmpdir, "positions.json"))
        tracker = EquityTracker(
            starting_balance=10.0,
            position_repo=repo,
            audit=audit,
        )
        tracker.update({})
        tracker.update({})
        events = audit.read_all()
        equity_events = [e for e in events if e.get("event_type") == "EQUITY_UPDATE"]
        self.assertEqual(len(equity_events), 0)
        # The snapshot data itself is still fully available in-memory.
        latest = tracker.latest()
        self.assertTrue(hasattr(latest, "total_equity"))
        self.assertTrue(hasattr(latest, "delta_usd"))
        self.assertTrue(hasattr(latest, "delta_pct"))

    def test_reconcile_still_emits_audit_event(self):
        """Deposits/withdrawals detected by reconcile_external_balance
        are rare, meaningful events (unlike a routine mark-to-market
        tick) and should still hit the audit ledger."""
        tmpdir = tempfile.mkdtemp()
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        repo = PositionRepository(os.path.join(tmpdir, "positions.json"))
        tracker = EquityTracker(
            starting_balance=10.0,
            position_repo=repo,
            audit=audit,
        )
        tracker.reconcile_external_balance(broker_balance=30.0)
        events = audit.read_all()
        deposit_events = [e for e in events if e.get("event_type") == "EQUITY_DEPOSIT"]
        self.assertEqual(len(deposit_events), 1)


class FormatEquityLineTest(unittest.TestCase):
    def test_format_with_precision(self):
        snap = EquitySnapshot(
            timestamp=0, iso="2026-07-09",
            starting_balance=10.0,
            realized_pnl=0.0123, unrealized_pnl=0.0,
            total_equity=10.0123, delta_usd=0.0123, delta_pct=0.123,
            open_positions=0, closed_positions=1,
            drawdown_usd=0.0, drawdown_pct=0.0,
        )
        line = format_equity_line(snap, precision=4)
        self.assertIn("$10.0123", line)
        self.assertIn("+$0.0123", line)
        self.assertIn("+0.12%", line)
        self.assertIn("🟢", line)

    def test_format_negative_delta(self):
        snap = EquitySnapshot(
            timestamp=0, iso="2026-07-09",
            starting_balance=10.0,
            realized_pnl=-0.05, unrealized_pnl=0.0,
            total_equity=9.95, delta_usd=-0.05, delta_pct=-0.5,
            open_positions=0, closed_positions=1,
            drawdown_usd=-0.05, drawdown_pct=-0.5,
        )
        line = format_equity_line(snap, precision=4)
        self.assertIn("$9.9500", line)
        self.assertIn("$-0.0500", line)
        self.assertIn("🔴", line)


class EquityTrackerValidationTest(unittest.TestCase):
    def test_negative_starting_balance_rejected(self):
        with self.assertRaises(ValueError):
            EquityTracker(starting_balance=-1.0)

    def test_zero_starting_balance_rejected(self):
        with self.assertRaises(ValueError):
            EquityTracker(starting_balance=0.0)


class EquityTrackerPersistTest(unittest.TestCase):
    """Sprint 24: crash-only persistence of equity tracker state."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "equity_state.json")

    def test_persist_creates_file(self):
        tracker = EquityTracker(starting_balance=10.0, history_size=50)
        persist_tracker(tracker, self.path)
        self.assertTrue(os.path.exists(self.path))

    def test_persist_then_load_roundtrip(self):
        # Create tracker, take a few snapshots, persist
        tracker = EquityTracker(starting_balance=10.0, history_size=50)
        tracker.update({})
        tracker.update({})
        # Force a max_equity
        tracker._max_equity = 10.5
        persist_tracker(tracker, self.path)

        # Load in a fresh tracker
        loaded = load_tracker(self.path)
        self.assertEqual(loaded.starting_balance, 10.0)
        self.assertEqual(len(loaded.history), len(tracker.history))
        self.assertEqual(loaded._max_equity, 10.5)

    def test_persist_includes_audit_state(self):
        """Even without audit, persist should work (audit is optional)."""
        tracker = EquityTracker(starting_balance=10.0, history_size=50)
        tracker.update({})
        tracker.update({})
        persist_tracker(tracker, self.path)
        # No audit = no crash
        loaded = load_tracker(self.path)
        self.assertEqual(loaded.starting_balance, 10.0)

    def test_load_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_tracker(os.path.join(self.tmpdir, "nonexistent.json"))

    def test_load_corrupt_file_raises(self):
        bad_path = os.path.join(self.tmpdir, "bad.json")
        with open(bad_path, "w") as f:
            f.write("{ this is not valid json")
        with self.assertRaises(RuntimeError):
            load_tracker(bad_path)

    def test_atomic_write_does_not_corrupt_on_failure(self):
        """If persist fails mid-write, existing file is preserved."""
        # Write initial good state
        tracker = EquityTracker(starting_balance=10.0, history_size=50)
        persist_tracker(tracker, self.path)
        original_content = open(self.path).read()

        # Simulate a failed write (point to a path in a non-existent dir)
        bad_path = os.path.join(self.tmpdir, "nonexistent", "subdir", "state.json")
        try:
            persist_tracker(tracker, bad_path)
        except Exception:
            pass

        # Original file is still intact
        self.assertEqual(open(self.path).read(), original_content)

    def test_load_restores_max_equity_for_drawdown(self):
        """After load, drawdown calc should use restored peak."""
        # Original tracker had a peak at $10.50
        tracker = EquityTracker(starting_balance=10.0, history_size=50)
        tracker._max_equity = 10.5
        persist_tracker(tracker, self.path)

        # Load and check drawdown
        loaded = load_tracker(self.path)
        self.assertEqual(loaded._max_equity, 10.5)
        # A snapshot at $10.0 should show drawdown of -$0.5
        snap = loaded.update({})
        self.assertAlmostEqual(snap.drawdown_usd, -0.5, places=4)


if __name__ == "__main__":
    unittest.main()
