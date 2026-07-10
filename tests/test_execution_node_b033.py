"""
Sprint 35 / B033 regression tests.

Bugs being fixed:
  1. ExecutionNode.execute_order() ALWAYS called the broker when
     broker_client was set, even when mandate_enabled=false (paper
     mode). binanceus has no testnet — so real money was being sent
     to the live exchange during paper testing.
  2. The bot tried to send SPY/GLD/USO/QQQ to binanceus, which only
     supports crypto. The error came back as a generic ccxt
     "does not have market symbol" exception, with no clear reason
     in the audit log.

These tests pin the new behavior:
  - Paper mode: orders are simulated locally, broker is NOT called
  - Live mode: orders are sent to the broker as before
  - Unsupported symbol: rejected BEFORE hitting the broker, with
    a clear SYMBOL_NOT_SUPPORTED status

Run: python -m unittest tests.test_execution_node_b033 -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.execution.execution_node import ExecutionNode, _is_mandate_enabled
from src.core.event_bus import EventBus


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


class IsMandateEnabledTest(unittest.TestCase):
    def test_no_file_returns_false(self):
        self.assertFalse(_is_mandate_enabled("/nonexistent/path.json"))

    def test_malformed_json_returns_false(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json")
            path = f.name
        try:
            self.assertFalse(_is_mandate_enabled(path))
        finally:
            os.unlink(path)

    def test_mandate_enabled_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_mode_override(tmp, True)
            self.assertTrue(_is_mandate_enabled(path))

    def test_mandate_enabled_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_mode_override(tmp, False)
            self.assertFalse(_is_mandate_enabled(path))


def _make_broker(supported_symbols: list):
    """Build a mock broker with the given supported symbols."""
    broker = MagicMock()
    exchange = MagicMock()
    exchange.id = "binanceus"
    exchange.symbols = supported_symbols
    broker.exchange = exchange
    broker.create_market_order.return_value = {"id": "FAKE_123", "status": "filled"}
    return broker


class PaperModeGateTest(unittest.TestCase):
    """B033 #1: paper mode must NOT call the broker."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Default: paper mode (mandate_enabled=false)
        self.mode_override_path = _write_mode_override(self.tmpdir, False)
        self.bus = EventBus()
        # binanceus uses USD pairs (no T): "BTC/USD", "ETH/USD"
        # (NOT the global binance "BTC/USDT" — different exchange)
        self.broker = _make_broker(["BTC/USD", "ETH/USD"])
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))

        self.node = ExecutionNode(
            self.bus,
            execution_mode="auto",
            broker_client=self.broker,
            kill_switch=None,
            audit=self.audit,
            mode_override_path=self.mode_override_path,
        )

    def test_paper_mode_does_not_call_broker(self):
        """The critical safety: paper mode must NOT hit the broker."""
        order = {
            "asset": "BTC-USD",
            "direction": "long",
            "position_size": 0.001,
            "entry_price": 50000,
            "stop_loss": 49000,
            "take_profit": 52000,
        }
        self.node.execute_order(order)
        # Broker was NEVER called
        self.broker.create_market_order.assert_not_called()
        # Audit recorded the simulated fill
        fills = [e for e in self.audit_events if e[0] == "TRADE_FILLED"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0][1]["status"], "FILLED (PAPER)")
        self.assertTrue(fills[0][1].get("simulated"))

    def test_paper_mode_emits_order_executed_with_simulated_flag(self):
        order = {
            "asset": "BTC-USD", "direction": "long", "position_size": 0.001,
            "entry_price": 50000, "stop_loss": 49000, "take_profit": 52000,
        }
        captured = []
        self.bus.subscribe("ORDER_EXECUTED", lambda d: captured.append(d))
        self.node.execute_order(order)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["status"], "FILLED (PAPER)")
        self.assertTrue(captured[0].get("simulated"))

    def test_live_mode_calls_broker(self):
        """Sanity: live mode still sends orders to the broker."""
        # Flip to live mode
        self.mode_override_path = _write_mode_override(self.tmpdir, True)
        self.node.mode_override_path = self.mode_override_path
        order = {
            "asset": "BTC-USD", "direction": "long", "position_size": 0.001,
            "entry_price": 50000, "stop_loss": 49000, "take_profit": 52000,
        }
        self.node.execute_order(order)
        # Broker WAS called
        self.broker.create_market_order.assert_called_once()
        # Audit recorded live fill
        fills = [e for e in self.audit_events if e[0] == "TRADE_FILLED"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0][1]["status"], "FILLED (LIVE MARKET)")
        self.assertNotIn("simulated", fills[0][1])

    def test_paper_mode_toggle_picks_up_immediately(self):
        """A dashboard toggle from paper to live should take effect on the next order."""
        order = {
            "asset": "BTC-USD", "direction": "long", "position_size": 0.001,
            "entry_price": 50000, "stop_loss": 49000, "take_profit": 52000,
        }
        # First order: paper
        self.node.execute_order(order)
        self.broker.create_market_order.assert_not_called()
        # Flip to live
        self.mode_override_path = _write_mode_override(self.tmpdir, True)
        self.node.mode_override_path = self.mode_override_path
        # Second order: live
        self.node.execute_order(order)
        self.broker.create_market_order.assert_called_once()


class SymbolValidationTest(unittest.TestCase):
    """B033 #2: reject unsupported symbols BEFORE hitting the broker."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Live mode (so the paper gate doesn't kick in)
        self.mode_override_path = _write_mode_override(self.tmpdir, True)
        self.bus = EventBus()
        # binanceus only supports crypto: BTC/USD, ETH/USD, SOL/USD
        # (no T, slash-separated, no stocks like SPY/QQQ)
        self.broker = _make_broker(["BTC/USD", "ETH/USD", "SOL/USD"])
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))

        self.node = ExecutionNode(
            self.bus,
            execution_mode="auto",
            broker_client=self.broker,
            kill_switch=None,
            audit=self.audit,
            mode_override_path=self.mode_override_path,
        )

    def test_spy_rejected_before_broker_call(self):
        order = {
            "asset": "SPY", "direction": "short", "position_size": 0.01,
            "entry_price": 750, "stop_loss": 760, "take_profit": 740,
        }
        self.node.execute_order(order)
        # Broker was NEVER called
        self.broker.create_market_order.assert_not_called()
        # Audit recorded the failure with clear reason
        failures = [e for e in self.audit_events if e[0] == "TRADE_FAILED"]
        self.assertEqual(len(failures), 1)
        self.assertIn("SYMBOL_NOT_SUPPORTED", failures[0][1]["status"])
        # 'SPY' has no slash or dash, so the code normalizes to 'SPY/USDT'
        self.assertEqual(failures[0][1]["symbol"], "SPY/USDT")

    def test_btcusdt_passes_validation(self):
        order = {
            "asset": "BTC-USD", "direction": "long", "position_size": 0.001,
            "entry_price": 50000, "stop_loss": 49000, "take_profit": 52000,
        }
        self.node.execute_order(order)
        # Broker WAS called (validation passed)
        self.broker.create_market_order.assert_called_once()

    def test_cached_symbols_reused(self):
        """Supported symbols should be fetched once and cached."""
        # First call populates cache
        self.broker.exchange.symbols = ["BTC/USD", "ETH/USD"]
        order = {
            "asset": "BTC-USD", "direction": "long", "position_size": 0.001,
            "entry_price": 50000, "stop_loss": 49000, "take_profit": 52000,
        }
        self.node.execute_order(order)
        # Now mutate the underlying list — cache should ignore
        self.broker.exchange.symbols = []
        self.node.execute_order(order)
        # Both calls should have hit the broker (cache returned the original list)
        self.assertEqual(self.broker.create_market_order.call_count, 2)


class NoBrokerTest(unittest.TestCase):
    """When broker is not configured, behavior should be unchanged."""

    def test_no_broker_simulates_filled(self):
        tmpdir = tempfile.mkdtemp()
        path = _write_mode_override(tmpdir, True)
        bus = EventBus()
        audit_events = []
        audit = MagicMock()
        audit.append.side_effect = lambda et, p: audit_events.append((et, p))
        node = ExecutionNode(
            bus, execution_mode="auto", broker_client=None,
            kill_switch=None, audit=audit, mode_override_path=path,
        )
        order = {
            "asset": "BTC-USD", "direction": "long", "position_size": 0.001,
            "entry_price": 50000, "stop_loss": 49000, "take_profit": 52000,
        }
        node.execute_order(order)
        fills = [e for e in audit_events if e[0] == "TRADE_FILLED"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0][1]["status"], "FILLED (SIMULATED)")


class KillSwitchStillWorksTest(unittest.TestCase):
    """The existing kill-switch behavior should be preserved (defense in depth)."""

    def test_kill_switch_blocks_order(self):
        tmpdir = tempfile.mkdtemp()
        path = _write_mode_override(tmpdir, True)  # live mode
        bus = EventBus()
        kill_switch = MagicMock()
        kill_switch.is_triggered.return_value = True
        audit = MagicMock()
        audit_events = []
        audit.append.side_effect = lambda et, p: audit_events.append((et, p))
        broker = _make_broker(["BTC/USD"])
        node = ExecutionNode(
            bus, execution_mode="auto", broker_client=broker,
            kill_switch=kill_switch, audit=audit, mode_override_path=path,
        )
        order = {
            "asset": "BTC-USD", "direction": "long", "position_size": 0.001,
            "entry_price": 50000, "stop_loss": 49000, "take_profit": 52000,
        }
        # Manually invoke on_order_approved since that's where kill switch is checked
        node.on_order_approved(order)
        # Broker was never called
        broker.create_market_order.assert_not_called()
        # Audit got TRADE_BLOCKED_KILLSWITCH
        blocked = [e for e in audit_events if e[0] == "TRADE_BLOCKED_KILLSWITCH"]
        self.assertEqual(len(blocked), 1)


if __name__ == "__main__":
    unittest.main()
