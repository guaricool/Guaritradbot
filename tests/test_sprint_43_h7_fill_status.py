"""
Sprint 43 H7 fix tests — partial / pending fills must NOT be treated
as fully filled.

The bug: `ExecutionNode` checked `broker_order.get("status") != "failed"`.
Anything that wasn't explicitly "failed" was treated as FILLED.
That included:
  - "pending"      (order accepted but not yet filled)
  - "partially_filled" (only part of the qty matched)
  - "new"          (just submitted)
  - "accepted"     (acknowledged by exchange, not yet filled)
  - "open"         (still working)
  - "unknown"      (status we don't recognize)

For each of these, the position was added to the repo with the
FULL requested qty — a ghost position or a position with a wrong
size, depending on which way the partial fill went.

The fix introduces `_classify_fill_status(broker_order)`:
  - "filled"      → only "filled"/"closed" + filled qty > 0
  - "partial"     → "partially_filled"/"partial"
  - "failed"      → "failed"/"rejected"/"expired"/"canceled"
  - "pending"     → "new"/"accepted"/"open"/"pending"
  - "unknown"     → None / non-dict / unrecognized

`execute_order` only adds a position to the repo on "filled".
"partial" publishes SYSTEM_ERROR so Carlos investigates.

Tests verify all the statuses and the position-persistence gating.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.execution.execution_node import (
    ExecutionNode,
    _classify_fill_status,
)


class ClassifyFillStatusTest(unittest.TestCase):
    """The helper must map each broker status to a clear verdict."""

    def test_filled_status(self):
        self.assertEqual(_classify_fill_status({"status": "filled"}), "filled")
        self.assertEqual(_classify_fill_status({"status": "closed"}), "filled")
        self.assertEqual(_classify_fill_status({"status": "FILLED"}), "filled")
        self.assertEqual(_classify_fill_status({"status": "fill"}), "filled")

    def test_filled_with_zero_amount_is_unknown(self):
        """
        Defensive: if the broker says "filled" but reports filled=0,
        treat as unknown. A "filled" with 0 qty is contradictory.
        """
        self.assertEqual(
            _classify_fill_status({"status": "filled", "filled": 0}),
            "unknown",
        )
        self.assertEqual(
            _classify_fill_status({"status": "filled", "filled": 0.0}),
            "unknown",
        )

    def test_filled_with_nonzero_amount(self):
        self.assertEqual(
            _classify_fill_status({"status": "filled", "filled": 0.5}),
            "filled",
        )

    def test_partial_status(self):
        self.assertEqual(
            _classify_fill_status({"status": "partially_filled"}),
            "partial",
        )
        self.assertEqual(
            _classify_fill_status({"status": "partial"}),
            "partial",
        )

    def test_failed_statuses(self):
        for s in ["failed", "rejected", "expired", "canceled", "cancelled"]:
            self.assertEqual(
                _classify_fill_status({"status": s}),
                "failed",
                f"Status '{s}' should map to 'failed'",
            )

    def test_pending_statuses(self):
        """Audit's claim: these used to be treated as FILLED. Now they're pending."""
        for s in ["pending", "new", "accepted", "open", "PARTIALLY_FILLED"]:
            verdict = _classify_fill_status({"status": s})
            self.assertNotEqual(
                verdict, "filled",
                f"Audit's H7 bug: status='{s}' must NOT be 'filled'",
            )
        # Pending-classification for the most common ones:
        self.assertEqual(_classify_fill_status({"status": "pending"}), "pending")
        self.assertEqual(_classify_fill_status({"status": "new"}), "pending")
        self.assertEqual(_classify_fill_status({"status": "accepted"}), "pending")
        self.assertEqual(_classify_fill_status({"status": "open"}), "pending")

    def test_unknown_statuses(self):
        """Garbage / None / missing should not silently pass as filled."""
        self.assertEqual(_classify_fill_status(None), "unknown")
        self.assertEqual(_classify_fill_status({}), "unknown")
        self.assertEqual(_classify_fill_status({"status": None}), "unknown")
        self.assertEqual(_classify_fill_status({"status": 42}), "unknown")
        self.assertEqual(_classify_fill_status("not a dict"), "unknown")
        self.assertEqual(
            _classify_fill_status({"status": "what_is_this"}),
            "unknown",
        )


def _make_node():
    tmpdir = tempfile.mkdtemp()
    import json
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": True, "alpaca_paper": False}, f)
    bus = MagicMock()
    audit_events = []
    audit = MagicMock()
    audit.append.side_effect = lambda et, p: audit_events.append((et, p))
    broker = MagicMock()
    broker.exchange.symbols = ["BTC/USD", "ETH/USD"]
    broker.exchange.options = {"sandboxMode": True}
    from src.data_store.positions import PositionRepository
    repo = PositionRepository(os.path.join(tmpdir, "positions.json"))
    node = ExecutionNode(
        bus,
        broker_client=broker,
        alpaca_broker=broker,
        brokers_config={"crypto": {"symbols": ["BTC-USD"]}, "equity": {"symbols": ["SPY"]}},
        audit=audit,
        mode_override_path=path,
        position_repo=repo,
    )
    return node, broker, audit_events, bus


class ExecuteOrderFillStatusTest(unittest.TestCase):
    """execute_order must persist position ONLY on a full fill."""

    def test_partial_fill_does_not_add_position(self):
        """H7 fix: a partial fill must NOT add a position to the repo."""
        node, broker, audit_events, bus = _make_node()
        broker.create_market_order.return_value = {
            "id": "FAKE_PARTIAL", "status": "partially_filled", "filled": 0.5
        }
        order = {
            "asset": "BTC-USD", "direction": "long", "position_size": 1.0,
            "entry_price": 50000.0, "stop_loss": 49000.0, "take_profit": 52000.0,
        }
        node.execute_order(order)
        self.assertEqual(
            node.position_repo.count_open(), 0,
            "PARTIAL fill must NOT add a position (H7 fix)",
        )
        # SYSTEM_ERROR published for the partial fill
        publishes = [c.args[0] for c in bus.publish.call_args_list]
        self.assertIn("SYSTEM_ERROR", publishes)
        sys_errs = [c.args[1] for c in bus.publish.call_args_list if c.args[0] == "SYSTEM_ERROR"]
        self.assertTrue(any(s.get("kind") == "PARTIAL_FILL" for s in sys_errs))

    def test_pending_fill_does_not_add_position(self):
        """H7 fix: a pending order must NOT add a position to the repo."""
        node, broker, _, _ = _make_node()
        broker.create_market_order.return_value = {"id": "FAKE_PEND", "status": "pending"}
        order = {
            "asset": "BTC-USD", "direction": "long", "position_size": 0.001,
            "entry_price": 50000.0, "stop_loss": 49000.0, "take_profit": 52000.0,
        }
        node.execute_order(order)
        self.assertEqual(
            node.position_repo.count_open(), 0,
            "PENDING fill must NOT add a position (H7 fix)",
        )

    def test_filled_status_with_zero_filled_treated_as_unknown(self):
        """A 'filled' status with filled=0 is contradictory — treat as unknown."""
        node, broker, _, _ = _make_node()
        broker.create_market_order.return_value = {
            "id": "FAKE", "status": "filled", "filled": 0
        }
        order = {
            "asset": "BTC-USD", "direction": "long", "position_size": 0.001,
            "entry_price": 50000.0, "stop_loss": 49000.0, "take_profit": 52000.0,
        }
        node.execute_order(order)
        self.assertEqual(
            node.position_repo.count_open(), 0,
            "filled=0 contradicts 'filled' status — must NOT add position",
        )

    def test_actual_filled_adds_position(self):
        """Regression: a real 'filled' response with filled > 0 adds position."""
        node, broker, _, _ = _make_node()
        broker.create_market_order.return_value = {
            "id": "FAKE_OK", "status": "filled", "filled": 0.001
        }
        order = {
            "asset": "BTC-USD", "direction": "long", "position_size": 0.001,
            "entry_price": 50000.0, "stop_loss": 49000.0, "take_profit": 52000.0,
        }
        node.execute_order(order)
        self.assertEqual(node.position_repo.count_open(), 1)


if __name__ == "__main__":
    unittest.main()
