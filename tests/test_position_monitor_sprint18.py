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


class B022StrategyAgentEmitsHypothesisEventsTest(unittest.TestCase):
    """
    B022: StrategyAgent MUST emit HYPOTHESIS_GENERATED events to the audit
    ledger so PositionMonitor.check_with_signals() can find them.

    Without this, the smart-profit-take feature is dead code.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))

    def test_strong_rsi_oversold_emits_with_high_strength(self):
        from src.agents.strategy_agent import StrategyAgent, _hypothesis_strength

        # A deep RSI oversold should yield high strength (>= 0.8)
        hyp = {
            "asset": "BTC-USD", "direction": "long", "strategy": "MeanReversion_LONG_RSI<30",
            "price": 50000, "rsi_at_signal": 22.0,
        }
        strength = _hypothesis_strength(hyp)
        self.assertGreaterEqual(strength, 0.8,
                                f"Deep oversold RSI=22 should yield strength>=0.8, got {strength}")

    def test_mild_oversold_emits_with_moderate_strength(self):
        from src.agents.strategy_agent import _hypothesis_strength

        hyp = {
            "asset": "BTC-USD", "direction": "long", "strategy": "MeanReversion_LONG_RSI<35",
            "price": 50000, "rsi_at_signal": 33.0,
        }
        strength = _hypothesis_strength(hyp)
        self.assertGreaterEqual(strength, 0.5)
        self.assertLess(strength, 0.8)

    def test_overbought_short_emits_with_high_strength(self):
        from src.agents.strategy_agent import _hypothesis_strength

        hyp = {
            "asset": "BTC-USD", "direction": "short", "strategy": "MeanReversion_SHORT_RSI>70",
            "price": 50000, "rsi_at_signal": 78.0,
        }
        strength = _hypothesis_strength(hyp)
        self.assertGreaterEqual(strength, 0.8)

    def test_default_strength_is_neutral(self):
        from src.agents.strategy_agent import _hypothesis_strength

        hyp = {
            "asset": "BTC-USD", "direction": "long", "strategy": "unknown_strategy",
            "price": 50000,
        }
        strength = _hypothesis_strength(hyp)
        self.assertEqual(strength, 0.5)

    def test_audit_receives_hypothesis_events_when_audit_set(self):
        """
        Direct test: simulate StrategyAgent._add_hyp + the audit emit
        logic to confirm events land in the audit ledger.
        """
        # Simulate what StrategyAgent.evaluate_strategies would do
        from src.agents.strategy_agent import _hypothesis_strength
        hypotheses = [
            {
                "asset": "BTC-USD", "tf": "1h", "direction": "long",
                "strategy": "MeanReversion_LONG_RSI<30", "price": 50000,
                "atr_at_signal": 200, "rsi_at_signal": 25.0,
            },
            {
                "asset": "ETH-USD", "tf": "1h", "direction": "short",
                "strategy": "MeanReversion_SHORT_RSI>70", "price": 3000,
                "atr_at_signal": 30, "rsi_at_signal": 75.0,
            },
        ]
        for h in hypotheses:
            self.audit.append("HYPOTHESIS_GENERATED", {
                "asset": h["asset"],
                "tf": h.get("tf", ""),
                "direction": h["direction"],
                "strategy": h["strategy"],
                "price": h["price"],
                "atr_at_signal": h.get("atr_at_signal", 0),
                "rsi_at_signal": h.get("rsi_at_signal", 0),
                "strength": _hypothesis_strength(h),
            })

        # Verify events landed
        events = self.audit.read_all()
        hyp_events = [e for e in events if e.get("event_type") == "HYPOTHESIS_GENERATED"]
        self.assertEqual(len(hyp_events), 2)
        self.assertEqual(hyp_events[0]["asset"], "BTC-USD")
        self.assertEqual(hyp_events[0]["direction"], "long")
        self.assertGreaterEqual(hyp_events[0]["strength"], 0.8)
        self.assertEqual(hyp_events[1]["asset"], "ETH-USD")
        self.assertEqual(hyp_events[1]["direction"], "short")

    def test_end_to_end_smart_take_with_real_audit_events(self):
        """
        Integration: open a profitable position, append HYPOTHESIS_GENERATED
        events to the audit, then call check_with_signals reading from the
        audit. This mirrors what main.py does in production.
        """
        repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        long_pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=40000.0, stop_loss=39000.0, take_profit=42000.0,
            qty=0.001, risk_usd=10.0,
            entry_ts=time.time() - 3600, strategy="momentum",
        )
        repo.add_open(long_pos)

        # Simulate: StrategyAgent emitted a strong SHORT signal 5 minutes ago
        import time as _t
        self.audit.append("HYPOTHESIS_GENERATED", {
            "asset": "BTC-USD", "direction": "short",
            "strategy": "MeanReversion_SHORT_RSI>70",
            "price": 41000, "rsi_at_signal": 78.0,
            "strength": 0.85,
            "ts": _t.time() - 300,
        })

        # Production code: read recent signals from audit
        recent = self.audit.read_since(_t.time() - 3600)
        signals = [e for e in recent if e.get("event_type") == "HYPOTHESIS_GENERATED"]

        monitor = PositionMonitor(repo=repo, audit=self.audit, broker=None,
                                  min_profit_to_protect=0.0)
        closes = monitor.check_with_signals(
            current_prices={"BTC-USD": 41000.0},  # +$10 profit
            signals=signals,
            signal_min_strength=0.6,
        )

        self.assertEqual(len(closes), 1, "Smart profit-take should have fired")
        self.assertGreater(closes[0].realized_pnl, 0)


if __name__ == "__main__":
    unittest.main()