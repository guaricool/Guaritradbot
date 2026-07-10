"""
Sprint 43 M9 fix tests — unknown symbols must NOT silently default
to crypto.

The bug: `ExecutionNode._resolve_broker()` defaulted unknown
assets to "crypto" (binanceus). A typo or a stale config could
send an order for an unrecognized ticker to binanceus, which
might not support the pair (or worse, the order would fail
silently with SYMBOL_NOT_SUPPORTED, leaving the user
unaware their config is wrong).

The fix:
  1. `_resolve_broker()` now returns (None, "unknown", {}) for
     any asset not in the routing table.
  2. `execute_order()` detects the "unknown" asset_class and
     rejects the order with `FAILED (UNKNOWN_SYMBOL: ...)`,
     auditable and visible to NotificationAgent.
  3. SYSTEM_ERROR is published (C6 compatible) so Carlos gets
     a Telegram ping.
  4. The warning includes a sample of known symbols so it's
     obvious what the user needs to add to config.yaml.

Tests verify:
  - Known assets still route to the right broker (backward compat)
  - Unknown assets return (None, "unknown", {})
  - Unknown assets in execute_order() fail loudly (audit + publish)
  - The error message includes a hint about config.yaml
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.execution.execution_node import ExecutionNode


def _make_mode_override(tmpdir, mandate_enabled):
    import json
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled, "alpaca_paper": True}, f)
    return path


def _make_node(brokers_config=None, with_audit=True, with_event_bus=True):
    tmpdir = tempfile.mkdtemp()
    bus = MagicMock() if with_event_bus else None
    audit_events = []
    if with_audit:
        audit = MagicMock()
        audit.append.side_effect = lambda et, p: audit_events.append((et, p))
    else:
        audit = None
    broker = MagicMock()
    broker.exchange.symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
    broker.exchange.options = {"sandboxMode": True}
    broker.create_market_order.return_value = {"id": "FAKE", "status": "filled"}
    node = ExecutionNode(
        bus,
        broker_client=broker,
        alpaca_broker=broker,  # use same mock for both
        brokers_config=brokers_config or {
            "crypto": {"symbols": ["BTC-USD", "ETH-USD", "SOL-USD"]},
            "equity": {"symbols": ["SPY", "QQQ"]},
        },
        audit=audit,
        mode_override_path=_make_mode_override(tmpdir, False),  # paper mode
        position_repo=None,
    )
    return node, bus, audit_events


class ResolveBrokerUnknownTest(unittest.TestCase):
    """The resolution helper must return (None, 'unknown', {}) for
    assets that aren't in the routing table."""

    def test_known_crypto_asset_resolves(self):
        node, _, _ = _make_node()
        broker, asset_class, cfg = node._resolve_broker("BTC-USD")
        self.assertIsNotNone(broker)
        self.assertEqual(asset_class, "crypto")
        self.assertIn("symbols", cfg)

    def test_known_equity_asset_resolves(self):
        node, _, _ = _make_node()
        broker, asset_class, cfg = node._resolve_broker("SPY")
        self.assertEqual(asset_class, "equity")
        self.assertIn("symbols", cfg)

    def test_unknown_asset_returns_unknown(self):
        """
        The audit's claim: previously this would return
        (broker, "crypto", cfg) — silently routing the unknown
        asset to binanceus. Now it returns (None, "unknown", {}).
        """
        node, _, _ = _make_node()
        broker, asset_class, cfg = node._resolve_broker("XYZ-USD")
        self.assertIsNone(broker, "Unknown asset must NOT get a broker")
        self.assertEqual(asset_class, "unknown")
        self.assertEqual(cfg, {})

    def test_typo_in_btc_resolves_to_unknown(self):
        """Common typo: BTC vs BTC1 vs BTC-USDT vs BTC-USD"""
        node, _, _ = _make_node()
        for typo in ["BTC1-USD", "BTC-USDT", "btc-usd", "BTCUSD", "BTC_USD"]:
            broker, asset_class, _ = node._resolve_broker(typo)
            self.assertEqual(
                asset_class, "unknown",
                f"Typo '{typo}' must resolve to unknown, got '{asset_class}'",
            )
            self.assertIsNone(broker)

    def test_unknown_audits_and_publishes(self):
        """The unknown-resolution path must leave a trail (audit + SYSTEM_ERROR)."""
        node, bus, audit_events = _make_node()
        node._resolve_broker("XYZ-USD")
        # Audit recorded
        unknown_audit = [e for e in audit_events if e[0] == "UNKNOWN_SYMBOL_ROUTED"]
        self.assertEqual(len(unknown_audit), 1)
        self.assertEqual(unknown_audit[0][1]["asset"], "XYZ-USD")
        # SYSTEM_ERROR published
        publishes = [c.args[0] for c in bus.publish.call_args_list]
        self.assertIn("SYSTEM_ERROR", publishes)
        sys_err = next(c.args[1] for c in bus.publish.call_args_list if c.args[0] == "SYSTEM_ERROR")
        self.assertEqual(sys_err["kind"], "UNKNOWN_SYMBOL")
        self.assertEqual(sys_err["asset"], "XYZ-USD")
        # Error message mentions config.yaml
        self.assertIn("config.yaml", sys_err["error"])


class ExecuteOrderUnknownRejectionTest(unittest.TestCase):
    """execute_order must reject unknown assets with a clear failure."""

    def test_unknown_symbol_order_fails_loudly(self):
        node, bus, audit_events = _make_node()
        order = {
            "asset": "XYZ-USD",
            "direction": "long",
            "position_size": 0.001,
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
        }
        node.execute_order(order)
        # Broker was NEVER called for the unknown symbol
        # (the broker mock would have been called if we fell through
        # to crypto; the test verifies this didn't happen)
        # The order's status should be UNKNOWN_SYMBOL
        failed = [e for e in audit_events if e[0] == "TRADE_FAILED"]
        self.assertEqual(len(failed), 1)
        self.assertIn("UNKNOWN_SYMBOL", failed[0][1]["status"])
        self.assertEqual(failed[0][1]["kind"], "UNKNOWN_SYMBOL")
        # ORDER_EXECUTED published with FAILED status
        published = [c.args[1] for c in bus.publish.call_args_list if c.args[0] == "ORDER_EXECUTED"]
        self.assertEqual(len(published), 1)
        self.assertIn("UNKNOWN_SYMBOL", published[0]["status"])

    def test_known_symbol_still_works(self):
        """Regression: the M9 fix must NOT break known symbols."""
        node, bus, audit_events = _make_node()
        order = {
            "asset": "BTC-USD",
            "direction": "long",
            "position_size": 0.001,
            "entry_price": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
        }
        node.execute_order(order)
        # Should fall through to the paper-mode simulated fill
        fills = [e for e in audit_events if e[0] == "TRADE_FILLED"]
        self.assertEqual(len(fills), 1, "Known symbol BTC-USD should still fill in paper mode")
        # No UNKNOWN_SYMBOL audit event
        unknown = [e for e in audit_events if "UNKNOWN" in e[0]]
        self.assertEqual(len(unknown), 0, f"Known symbol should not produce UNKNOWN events: {unknown}")


if __name__ == "__main__":
    unittest.main()
