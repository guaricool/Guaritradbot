"""
Sprint 46N tests — audit finding C3 (AUDITORIA_COMPLETA_2026-07-11.md).

C3: `AlpacaBroker.create_market_order` returned the order's status at
the INSTANT of submission (almost always "new"/"accepted", never
"filled" — even for a plain market order during market hours, the
fill confirmation lands a moment later). On top of that, the status
was stringified with `str(order.status)`, which for alpaca-py's
`(str, Enum)`-hybrid `OrderStatus` produces `"OrderStatus.NEW"`, not
`"new"` — so `_classify_fill_status` NEVER matched any known status
for an Alpaca order and always fell through to "unknown". Combined,
every single Alpaca equity fill (even ones that filled instantly) was
silently treated as NOT_FILLED and never persisted to the repo — the
SL/TP protection this bot exists to provide never activated for a
single equity position.

This file covers:
  - `_normalize_status`: enum-style status objects are unwrapped via
    `.value`, not stringified as "OrderStatus.X".
  - `ExecutionNode._execute_equity_order` polls `broker.get_order()`
    when the initial status is pending, and persists once a polled
    response reaches "filled".
  - Immediate "filled" (no polling needed) still works — no regression
    for a broker/test double that already returns a terminal status.
  - If every poll attempt stays pending, the position is NOT persisted
    and a distinct SYSTEM_ERROR (kind=ALPACA_FILL_TIMEOUT) is
    published, instead of silently doing nothing.

Run: python -m unittest tests.test_sprint_46n_alpaca_fill_polling -v
"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.core.event_bus import EventBus
from src.execution.execution_node import ExecutionNode, _classify_fill_status
from src.execution.alpaca_broker import _normalize_status


class _FakeEnumStatus:
    """Mimics alpaca-py's OrderStatus: a (str, Enum) hybrid whose
    __str__ returns "OrderStatus.NEW" but whose .value is "new"."""
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return f"OrderStatus.{self.value.upper()}"


class NormalizeStatusTest(unittest.TestCase):
    def test_enum_like_status_uses_value_not_str(self):
        status = _FakeEnumStatus("new")
        self.assertEqual(_normalize_status(status), "new")
        # Sanity: confirm the naive str() WOULD have been wrong,
        # which is exactly the bug this fix addresses.
        self.assertEqual(str(status), "OrderStatus.NEW")

    def test_plain_string_status_passthrough(self):
        self.assertEqual(_normalize_status("filled"), "filled")

    def test_none_status(self):
        self.assertEqual(_normalize_status(None), "")


class _FakeAlpacaBrokerPolling:
    """Simulates create_market_order returning a pending status, then
    get_order() progressing toward a terminal status after some number
    of polls. `responses` is a list of statuses returned by successive
    get_order() calls; the last one repeats if polled more times than
    the list has entries."""

    def __init__(self, initial_status="new", poll_responses=None, filled_qty="1"):
        self.initial_status = initial_status
        self.poll_responses = poll_responses or []
        self.filled_qty = filled_qty
        self.get_order_calls = 0
        self.is_symbol_tradeable = MagicMock(return_value=True)

    def create_market_order(self, symbol, side, amount=None, notional_usd=None):
        return {
            "id": "ORDER_1",
            "status": self.initial_status,
            "symbol": symbol,
            "endpoint": "paper",
        }

    def get_order(self, order_id, endpoint=None):
        idx = min(self.get_order_calls, len(self.poll_responses) - 1) if self.poll_responses else 0
        self.get_order_calls += 1
        if not self.poll_responses:
            return {"id": order_id, "status": "new"}
        status = self.poll_responses[idx]
        return {"id": order_id, "status": status, "filled": self.filled_qty}


def _make_execution_node(broker, poll_attempts=3, poll_delay=0.0):
    bus = EventBus()
    node = ExecutionNode(
        bus,
        execution_mode="auto",
        alpaca_broker=broker,
        brokers_config={"equity": {"symbols": ["SPY"]}},
        alpaca_fill_poll_attempts=poll_attempts,
        # 0 delay so the test suite doesn't actually sleep.
        alpaca_fill_poll_delay_s=poll_delay,
    )
    return node, bus


class ExecuteEquityOrderPollingTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _order_data(self):
        return {
            "asset": "SPY",
            "direction": "long",
            "position_size": 1.0,
            "entry_price": 500.0,
            "strategy": "test",
            "stop_loss": 490.0,
            "take_profit": 520.0,
        }

    def test_immediate_filled_needs_no_polling(self):
        """Regression: a broker that already returns 'filled' on submit
        must still work without ever calling get_order()."""
        broker = _FakeAlpacaBrokerPolling(initial_status="filled", filled_qty="1")
        node, bus = _make_execution_node(broker)
        events = []
        bus.subscribe("ORDER_EXECUTED", lambda d: events.append(d))

        node._execute_equity_order(self._order_data(), broker)

        self.assertEqual(broker.get_order_calls, 0, "should not poll when already filled")
        self.assertTrue(events[0]["status"].startswith("FILLED"))

    def test_pending_then_filled_after_polling(self):
        """The realistic Alpaca case: submit comes back 'new', a few
        polls later it's 'filled' — position must be persisted."""
        broker = _FakeAlpacaBrokerPolling(
            initial_status="new",
            poll_responses=["new", "new", "filled"],
        )
        node, bus = _make_execution_node(broker, poll_attempts=5)
        events = []
        bus.subscribe("ORDER_EXECUTED", lambda d: events.append(d))

        node._execute_equity_order(self._order_data(), broker)

        self.assertGreaterEqual(broker.get_order_calls, 3)
        self.assertTrue(events[0]["status"].startswith("FILLED"), events[0]["status"])

    def test_still_pending_after_poll_budget_publishes_timeout_not_filled(self):
        """If every poll attempt stays pending, do NOT persist and
        publish a distinct ALPACA_FILL_TIMEOUT SYSTEM_ERROR (not a
        silent no-op, and not misclassified as a normal failure)."""
        broker = _FakeAlpacaBrokerPolling(
            initial_status="new",
            poll_responses=["new", "new", "new"],
        )
        node, bus = _make_execution_node(broker, poll_attempts=3)
        order_events = []
        sys_errors = []
        bus.subscribe("ORDER_EXECUTED", lambda d: order_events.append(d))
        bus.subscribe("SYSTEM_ERROR", lambda d: sys_errors.append(d))

        node._execute_equity_order(self._order_data(), broker)

        self.assertEqual(broker.get_order_calls, 3)
        self.assertFalse(order_events[0]["status"].startswith("FILLED"))
        timeout_errors = [e for e in sys_errors if e.get("kind") == "ALPACA_FILL_TIMEOUT"]
        self.assertEqual(len(timeout_errors), 1)

    def test_pending_then_partial_after_polling(self):
        """A polled 'partially_filled' must be classified as partial,
        not filled — no position persisted, PARTIAL_FILL raised."""
        broker = _FakeAlpacaBrokerPolling(
            initial_status="new",
            poll_responses=["new", "partially_filled"],
            filled_qty="0.5",
        )
        node, bus = _make_execution_node(broker, poll_attempts=5)
        order_events = []
        sys_errors = []
        bus.subscribe("ORDER_EXECUTED", lambda d: order_events.append(d))
        bus.subscribe("SYSTEM_ERROR", lambda d: sys_errors.append(d))

        node._execute_equity_order(self._order_data(), broker)

        self.assertTrue(order_events[0]["status"].startswith("PARTIAL_FILL"))
        partials = [e for e in sys_errors if e.get("kind") == "PARTIAL_FILL"]
        self.assertEqual(len(partials), 1)


if __name__ == "__main__":
    unittest.main()
