"""
Sprint 18 tests — PositionMonitor smart profit-take.

Covers:
- Feature 2: Smart profit-take — close a position that's IN PROFIT when a
  strong opposite-direction signal arrives.

Run: python -m unittest tests.test_position_monitor_sprint18 -v
"""
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data_store.positions import PositionRepository, Position
from src.data_store.position_monitor import PositionMonitor
from src.safety.audit_ledger import AuditLedger


class SmartProfitTakeTest(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))

        # Open a LONG BTC position that is currently in profit
        self.long_pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=40000.0,
            stop_loss=39000.0,
            take_profit=42000.0,
            qty=0.001,
            risk_usd=10.0,
            entry_ts=time.time() - 3600,  # 1h ago
            strategy="momentum",
        )
        self.repo.add_open(self.long_pos)
        self.monitor = PositionMonitor(
            repo=self.repo, audit=self.audit, broker=None,
            min_profit_to_protect=0.0,
        )

    def test_profitable_long_closed_on_strong_short_signal(self):
        """LONG @40k, now @41k (+$10 profit), strong SHORT signal → close."""
        signals = [{
            "asset": "BTC-USD",
            "direction": "short",
            "strength": 0.85,
        }]
        closes = self.monitor.check_with_signals(
            current_prices={"BTC-USD": 41000.0},
            signals=signals,
            signal_min_strength=0.6,
        )

        self.assertEqual(len(closes), 1, "Should have closed the profitable long")
        closed = closes[0]
        self.assertEqual(closed.asset, "BTC-USD")
        self.assertGreater(closed.realized_pnl, 0,
                           f"Should have realized profit; got {closed.realized_pnl}")
        self.assertIn("SMART_PROFIT_TAKE", closed.close_reason or "")

        # Audit log records it
        events = self.audit.read_all()
        closed_events = [e for e in events if e.get("event_type") == "TRADE_CLOSED"]
        self.assertEqual(len(closed_events), 1)
        self.assertIn("SMART_PROFIT_TAKE", closed_events[0]["reason"])

    def test_profitable_long_NOT_closed_on_weak_signal(self):
        """LONG in profit, but reversal signal is weak → keep position."""
        signals = [{
            "asset": "BTC-USD",
            "direction": "short",
            "strength": 0.3,  # below threshold 0.6
        }]
        closes = self.monitor.check_with_signals(
            current_prices={"BTC-USD": 41000.0},
            signals=signals,
            signal_min_strength=0.6,
        )

        self.assertEqual(len(closes), 0, "Weak signal should not trigger close")
        self.assertEqual(self.repo.count_open(), 1, "Position should still be open")

    def test_losing_position_NOT_closed_even_with_strong_signal(self):
        """LONG at loss, even strong SHORT signal → don't 'protect' non-existent profit."""
        # Position is long @40000, now at 39500 (-$5)
        signals = [{
            "asset": "BTC-USD",
            "direction": "short",
            "strength": 0.9,
        }]
        closes = self.monitor.check_with_signals(
            current_prices={"BTC-USD": 39500.0},
            signals=signals,
            signal_min_strength=0.6,
        )

        self.assertEqual(len(closes), 0,
                         "Losing position should not be 'profit-protected'")
        self.assertEqual(self.repo.count_open(), 1)

    def test_no_reversal_signal_keeps_profitable_long(self):
        """LONG in profit, but NO opposing signal → keep position."""
        signals = [{
            "asset": "BTC-USD",
            "direction": "long",  # same direction, no reversal
            "strength": 0.9,
        }]
        closes = self.monitor.check_with_signals(
            current_prices={"BTC-USD": 41000.0},
            signals=signals,
            signal_min_strength=0.6,
        )
        self.assertEqual(len(closes), 0)
        self.assertEqual(self.repo.count_open(), 1)

    def test_mechanical_sl_tp_still_works(self):
        """Original SL/TP check still functions."""
        # Price drops below SL → close mechanically
        closes = self.monitor.check(current_prices={"BTC-USD": 38000.0})
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0].close_reason, "STOP_HIT")


class MinProfitToProtectTest(unittest.TestCase):

    def test_below_threshold_does_not_trigger(self):
        """If unrealized PnL < min_profit_to_protect, don't trigger even with signal."""
        tmpdir = tempfile.mkdtemp()
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        repo = PositionRepository(os.path.join(tmpdir, "positions.json"))

        pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=40000.0, stop_loss=39000.0, take_profit=42000.0,
            qty=0.001, risk_usd=10.0,
            entry_ts=time.time() - 3600, strategy="test",
        )
        repo.add_open(pos)

        # $5 profit is below threshold of $7
        monitor = PositionMonitor(repo=repo, audit=audit, broker=None,
                                  min_profit_to_protect=7.0)
        signals = [{"asset": "BTC-USD", "direction": "short", "strength": 0.9}]
        closes = monitor.check_with_signals(
            current_prices={"BTC-USD": 40500.0},  # +$5 profit
            signals=signals, signal_min_strength=0.6,
        )
        self.assertEqual(len(closes), 0)


if __name__ == "__main__":
    unittest.main()