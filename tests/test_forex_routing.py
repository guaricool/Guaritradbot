"""
Forex (OANDA) broker routing tests.

Carlos: "seria integrar un broker para esto" -- forex added as a
third asset class alongside crypto (binance.us) and equity (Alpaca).
These tests verify the routing plumbing (ExecutionNode._resolve_broker,
broker_routing.resolve_broker_for_close, main.py's _get_active_asset_classes)
treats forex the same way equity is already treated -- never silently
falling back to the crypto broker for a forex symbol.

Run: python -m unittest tests.test_forex_routing -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.execution.execution_node import ExecutionNode
from src.execution.broker_routing import (
    build_asset_to_class_map,
    resolve_broker_for_close,
)


FOREX_BROKERS_CONFIG = {
    "crypto": {"symbols": ["BTC-USD"]},
    "equity": {"symbols": ["SPY"]},
    "forex": {"symbols": ["EURUSD=X", "GBPUSD=X"]},
}


def _make_mode_override(tmpdir, mandate_enabled):
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled, "alpaca_paper": True}, f)
    return path


class BuildAssetToClassMapForexTest(unittest.TestCase):
    def test_forex_symbols_mapped(self):
        mapping = build_asset_to_class_map(FOREX_BROKERS_CONFIG)
        self.assertEqual(mapping["EURUSD=X"], "forex")
        self.assertEqual(mapping["GBPUSD=X"], "forex")
        self.assertEqual(mapping["BTC-USD"], "crypto")
        self.assertEqual(mapping["SPY"], "equity")


class ResolveBrokerForCloseForexTest(unittest.TestCase):
    def test_forex_asset_resolves_to_oanda_broker(self):
        crypto_broker = MagicMock(name="crypto")
        alpaca_broker = MagicMock(name="alpaca")
        oanda_broker = MagicMock(name="oanda")
        mapping = build_asset_to_class_map(FOREX_BROKERS_CONFIG)
        broker, asset_class = resolve_broker_for_close(
            "EURUSD=X", mapping, crypto_broker, alpaca_broker, oanda_broker,
        )
        self.assertIs(broker, oanda_broker)
        self.assertEqual(asset_class, "forex")

    def test_forex_asset_with_no_oanda_broker_returns_none(self):
        """oanda_broker defaults to None -- callers that don't pass it
        (back-compat) must not accidentally get the crypto broker for
        a forex asset."""
        crypto_broker = MagicMock(name="crypto")
        alpaca_broker = MagicMock(name="alpaca")
        mapping = build_asset_to_class_map(FOREX_BROKERS_CONFIG)
        broker, asset_class = resolve_broker_for_close(
            "EURUSD=X", mapping, crypto_broker, alpaca_broker,
        )
        self.assertIsNone(broker)
        self.assertEqual(asset_class, "forex")

    def test_equity_and_crypto_unaffected(self):
        crypto_broker = MagicMock(name="crypto")
        alpaca_broker = MagicMock(name="alpaca")
        oanda_broker = MagicMock(name="oanda")
        mapping = build_asset_to_class_map(FOREX_BROKERS_CONFIG)
        broker, asset_class = resolve_broker_for_close(
            "SPY", mapping, crypto_broker, alpaca_broker, oanda_broker,
        )
        self.assertIs(broker, alpaca_broker)
        self.assertEqual(asset_class, "equity")
        broker, asset_class = resolve_broker_for_close(
            "BTC-USD", mapping, crypto_broker, alpaca_broker, oanda_broker,
        )
        self.assertIs(broker, crypto_broker)
        self.assertEqual(asset_class, "crypto")


class ExecutionNodeResolveBrokerForexTest(unittest.TestCase):
    def _make_node(self):
        tmpdir = tempfile.mkdtemp()
        bus = MagicMock()
        oanda_broker = MagicMock()
        node = ExecutionNode(
            bus,
            broker_client=MagicMock(),
            alpaca_broker=MagicMock(),
            oanda_broker=oanda_broker,
            brokers_config=FOREX_BROKERS_CONFIG,
            audit=MagicMock(),
            mode_override_path=_make_mode_override(tmpdir, False),
            position_repo=None,
        )
        return node, oanda_broker

    def test_forex_asset_resolves_to_oanda(self):
        node, oanda_broker = self._make_node()
        broker, asset_class, cfg = node._resolve_broker("EURUSD=X")
        self.assertIs(broker, oanda_broker)
        self.assertEqual(asset_class, "forex")
        self.assertIn("symbols", cfg)

    def test_forex_not_configured_returns_none_broker(self):
        tmpdir = tempfile.mkdtemp()
        bus = MagicMock()
        node = ExecutionNode(
            bus,
            broker_client=MagicMock(),
            alpaca_broker=MagicMock(),
            oanda_broker=None,  # not configured
            brokers_config=FOREX_BROKERS_CONFIG,
            audit=MagicMock(),
            mode_override_path=_make_mode_override(tmpdir, False),
            position_repo=None,
        )
        broker, asset_class, _ = node._resolve_broker("EURUSD=X")
        self.assertIsNone(broker)
        self.assertEqual(asset_class, "forex")


class ExecutionNodeForexNotConfiguredTest(unittest.TestCase):
    """Mirrors the existing ALPACA_NOT_CONFIGURED behavior for forex."""

    def test_forex_entry_fails_loudly_when_oanda_missing(self):
        tmpdir = tempfile.mkdtemp()
        bus = MagicMock()
        audit_events = []
        audit = MagicMock()
        audit.append.side_effect = lambda et, p: audit_events.append((et, p))
        node = ExecutionNode(
            bus,
            broker_client=MagicMock(),
            alpaca_broker=MagicMock(),
            oanda_broker=None,
            brokers_config=FOREX_BROKERS_CONFIG,
            audit=audit,
            mode_override_path=_make_mode_override(tmpdir, False),
            position_repo=None,
        )
        node.execute_order({
            "asset": "EURUSD=X", "direction": "long",
            "position_size": 500, "entry_price": 1.08,
        })
        statuses = [p.get("status", "") for _, p in audit_events]
        self.assertTrue(
            any("OANDA_NOT_CONFIGURED" in s for s in statuses),
            f"Expected an OANDA_NOT_CONFIGURED failure, got: {statuses}",
        )


class ExecutionNodeForexPaperFillTest(unittest.TestCase):
    def test_paper_mode_simulates_without_calling_broker_order(self):
        tmpdir = tempfile.mkdtemp()
        bus = MagicMock()
        oanda_broker = MagicMock()
        oanda_broker.is_market_open.return_value = True
        oanda_broker.get_latest_trade_price.return_value = 1.0805
        repo = MagicMock()
        repo.open.return_value = []
        node = ExecutionNode(
            bus,
            broker_client=MagicMock(),
            alpaca_broker=MagicMock(),
            oanda_broker=oanda_broker,
            brokers_config=FOREX_BROKERS_CONFIG,
            audit=MagicMock(),
            mode_override_path=_make_mode_override(tmpdir, False),  # paper
            position_repo=repo,
        )
        node.execute_order({
            "asset": "EURUSD=X", "direction": "long",
            "position_size": 500, "entry_price": 1.08,
        })
        oanda_broker.create_market_order.assert_not_called()

    def test_paper_mode_skips_when_market_closed(self):
        tmpdir = tempfile.mkdtemp()
        bus = MagicMock()
        oanda_broker = MagicMock()
        oanda_broker.is_market_open.return_value = False
        audit_events = []
        audit = MagicMock()
        audit.append.side_effect = lambda et, p: audit_events.append((et, p))
        node = ExecutionNode(
            bus,
            broker_client=MagicMock(),
            alpaca_broker=MagicMock(),
            oanda_broker=oanda_broker,
            brokers_config=FOREX_BROKERS_CONFIG,
            audit=audit,
            mode_override_path=_make_mode_override(tmpdir, False),
            position_repo=MagicMock(),
        )
        node.execute_order({
            "asset": "EURUSD=X", "direction": "long",
            "position_size": 500, "entry_price": 1.08,
        })
        event_types = [et for et, _ in audit_events]
        self.assertIn("TRADE_SKIPPED_MARKET_CLOSED", event_types)
        oanda_broker.create_market_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
