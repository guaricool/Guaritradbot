"""
Sprint 46Q (audit M5) — edge cases of the native OCO stop-loss/take-profit
protection path.

The audit's M5 finding identified three concrete bugs in the OCO code that
were either unsafe or quietly produced phantom P&L:

  1. `listOrderStatus == ALL_DONE` is the exchange's terminal state for
     BOTH a successful OCO fill AND a manual cancellation of the
     orderList. The pre-Sprint-46Q code treated every ALL_DONE as a
     fill, which recorded a phantom TP_HIT_OCO profit for a
     position that the operator had just manually unprotected
     via the binance.us UI.

  2. The fallback path "OCO says done but no current price" used
     `pos.take_profit` as a neutral default — the SAME phantom
     profit bug in a different trigger (the realized_pnl was
     recorded against money that never moved).

  3. The STOP_LOSS_LIMIT's limit price sat only 0.5% below the
     stop trigger, so a typical 0.6% crypto gap triggered the
     stop but left the limit resting above the new market —
     unfilled, while the position lost more than the planned stop.
     The buffer was widened to 1.5% (configurable) in Sprint 46Q.

This file covers all three:

  A. ALL_DONE without any FILLED leg leaves the position open and
     audits OCO_CANCELLED_NOT_FILLED.
  B. ALL_DONE with exactly one FILLED leg closes the position at
     the trigger that matches the filled leg (TP or SL).
  C. ALL_DONE with a FILLED leg whose fill price is OUTSIDE the
     expected TP/SL range (slippage) closes at the actual fill
     price and audits OCO_FILL_SLIPPAGE.
  D. Default `stop_limit_buffer_pct` in BrokerClient.create_oco_sell_order
     is 1.5, not 0.5.

Run: python -m unittest tests.test_sprint_46q_m5_oco_edge_cases -v
"""
import json
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
from src.execution.broker import BrokerClient


class _FakeBrokerOCO:
    """Just enough of BrokerClient's surface for PositionMonitor's OCO
    reconciliation path. Returns whatever `get_oco_order_status` is set
    to by the test; that's the whole API the reconciliation uses."""

    def __init__(self, status_response):
        self._status = status_response

    def get_oco_order_status(self, symbol, order_list_id):
        return self._status


def _make_native_oco_position(repo, audit, broker_status, current_price=None):
    """Helper: put a protected-by-native-OCO position into the repo and
    return the PositionMonitor that will reconcile it."""
    pos = Position(
        asset="BTC-USD", direction="long",
        entry_price=10000.0, stop_loss=9900.0, take_profit=10500.0,
        qty=0.001, risk_usd=1.0,
        entry_ts=time.time() - 3600,
        strategy="momentum",
        protection_mode="native_oco",
        broker_oco_order_id="OCO-TEST-123",
    )
    repo.add_open(pos)
    prices = {"BTC-USD": current_price} if current_price is not None else {}
    return pos


class AllDoneWithoutFillLeavesPositionOpenTest(unittest.TestCase):
    """Sprint 46Q (audit M5 bug #1): ALL_DONE was treated as a fill even
    when both legs were cancelled — produced phantom TP_HIT_OCO profit.
    Now we parse `orderReports` and only close when a leg is FILLED."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))

    def _all_done_with_no_filled_legs(self):
        """ALL_DONE with both legs CANCELED — what a manual cancel looks
        like on binance.us. Pre-fix code would have marked the position
        closed at take_profit (phantom profit)."""
        return {
            "orderListId": "OCO-TEST-123",
            "listOrderStatus": "ALL_DONE",
            "orderReports": [
                {"orderId": 1, "symbol": "BTCUSDT", "side": "SELL",
                 "type": "LIMIT_MAKER", "status": "CANCELED",
                 "price": "10500.00", "stopPrice": "0"},
                {"orderId": 2, "symbol": "BTCUSDT", "side": "SELL",
                 "type": "STOP_LOSS_LIMIT", "status": "CANCELED",
                 "price": "9851.50", "stopPrice": "9900.00"},
            ],
        }

    def test_manually_cancelled_oco_does_not_close_position(self):
        """The position must STAY OPEN when ALL_DONE but no leg FILLED.
        A real close never happened on the exchange — recording one in
        the local repo would be a phantom close with phantom P&L."""
        _make_native_oco_position(
            self.repo, self.audit,
            broker_status=self._all_done_with_no_filled_legs(),
            current_price=10300.0,  # price clearly between SL and TP
        )
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit,
            broker=_FakeBrokerOCO(self._all_done_with_no_filled_legs()),
        )
        closes = monitor.check(current_prices={"BTC-USD": 10300.0})

        self.assertEqual(closes, [],
                         "Cancelled OCO must not close the position — "
                         "no real fill happened on the exchange")
        self.assertEqual(self.repo.count_open(), 1,
                         "Position must remain open after a cancelled OCO")

    def test_cancelled_oco_audits_the_situation(self):
        """Even though we don't close, the operator needs to know — the
        OCO was cancelled but the position is still open. Emit
        OCO_CANCELLED_NOT_FILLED so it shows up in the dashboard's
        audit feed (and Telegram if escalated to SYSTEM_ERROR)."""
        _make_native_oco_position(
            self.repo, self.audit,
            broker_status=self._all_done_with_no_filled_legs(),
        )
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit,
            broker=_FakeBrokerOCO(self._all_done_with_no_filled_legs()),
        )
        monitor.check(current_prices={})

        # Read the audit log to confirm the event was recorded
        with open(os.path.join(self.tmpdir, "audit.jsonl"), "r") as f:
            events = [json.loads(line) for line in f if line.strip()]
        cancel_events = [e for e in events
                         if e.get("event_type") == "OCO_CANCELLED_NOT_FILLED"]
        self.assertEqual(len(cancel_events), 1)
        self.assertEqual(cancel_events[0].get("asset"), "BTC-USD")
        self.assertEqual(cancel_events[0].get("list_status"), "ALL_DONE")
        # The detail field is a hint for human readers / dashboard
        self.assertIn("cancelled", cancel_events[0].get("detail", "").lower())

    def test_rejected_legs_also_count_as_not_filled(self):
        """A REJECTED leg (rather than CANCELED) — the same all-done-
        without-fill logic should apply. Some exchanges report
        cancellations as REJECTED; the audit's concern was the
        binary "all_done means filled" assumption, so we accept any
        non-FILLED status as a no-fill."""
        status = {
            "orderListId": "OCO-TEST-123",
            "listOrderStatus": "ALL_DONE",
            "orderReports": [
                {"orderId": 1, "symbol": "BTCUSDT", "status": "REJECTED"},
                {"orderId": 2, "symbol": "BTCUSDT", "status": "EXPIRED"},
            ],
        }
        _make_native_oco_position(
            self.repo, self.audit, broker_status=status,
        )
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit,
            broker=_FakeBrokerOCO(status),
        )
        closes = monitor.check(current_prices={})
        self.assertEqual(closes, [])
        self.assertEqual(self.repo.count_open(), 1)


class AllDoneWithFillClosesAtTriggerPriceTest(unittest.TestCase):
    """Sprint 46Q (audit M5 bug #1, positive case): when a leg DID fill,
    the position closes at the trigger that matches which leg filled."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))

    def test_take_profit_leg_filled_closes_at_tp(self):
        status = {
            "orderListId": "OCO-TEST-123",
            "listOrderStatus": "ALL_DONE",
            "orderReports": [
                {"orderId": 1, "symbol": "BTCUSDT", "side": "SELL",
                 "type": "LIMIT_MAKER", "status": "FILLED",
                 "price": "10500.00", "stopPrice": "0"},
                # The other leg is automatically CANCELED by the
                # exchange when one leg FILLED.
                {"orderId": 2, "symbol": "BTCUSDT", "side": "SELL",
                 "type": "STOP_LOSS_LIMIT", "status": "CANCELED",
                 "price": "9851.50", "stopPrice": "9900.00"},
            ],
        }
        _make_native_oco_position(
            self.repo, self.audit, broker_status=status,
        )
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit,
            broker=_FakeBrokerOCO(status),
        )
        closes = monitor.check(current_prices={"BTC-USD": 10600.0})
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0].close_reason, "TP_HIT_OCO")
        # Close price = the TP trigger, not the observed market price
        self.assertAlmostEqual(closes[0].closed_price, 10500.0)
        self.assertEqual(self.repo.count_open(), 0)

    def test_stop_leg_filled_closes_at_sl(self):
        status = {
            "orderListId": "OCO-TEST-123",
            "listOrderStatus": "ALL_DONE",
            "orderReports": [
                # The other leg is automatically CANCELED.
                {"orderId": 1, "symbol": "BTCUSDT", "side": "SELL",
                 "type": "LIMIT_MAKER", "status": "CANCELED",
                 "price": "10500.00", "stopPrice": "0"},
                {"orderId": 2, "symbol": "BTCUSDT", "side": "SELL",
                 "type": "STOP_LOSS_LIMIT", "status": "FILLED",
                 "price": "9851.50", "stopPrice": "9900.00"},
            ],
        }
        _make_native_oco_position(
            self.repo, self.audit, broker_status=status,
        )
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit,
            broker=_FakeBrokerOCO(status),
        )
        closes = monitor.check(current_prices={"BTC-USD": 9851.50})
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0].close_reason, "STOP_HIT_OCO")
        # Sprint 46Q: use the actual exchange-reported fill price for
        # realized PnL, not the trigger price. The fill at 9851.50 is
        # within the 2% buffer of stop_loss=9900 (floor=9702) so the
        # reason is the normal STOP_HIT_OCO — but the accounting price
        # is the actual 9851.50, not the 9900 trigger.
        self.assertAlmostEqual(closes[0].closed_price, 9851.50)
        self.assertEqual(self.repo.count_open(), 0)


class OcoFillSlippageTest(unittest.TestCase):
    """Sprint 46Q (audit M5): when the filled price is OUTSIDE the
    expected TP/SL trigger range (typical for STOP_LOSS_LIMIT — a fast
    drop can fill a few bps below stopPrice), we use the actual fill
    price and audit why it differs from the trigger."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))

    def test_fill_outside_range_uses_exchange_price_and_audits(self):
        # Position has stop_loss=9900 but the actual fill was 9500
        # (slipped 4% below the trigger — well outside the new
        # 1.5% buffer, indicating a gap event). Pre-fix code would
        # either close at 9900 (over-reporting) or use
        # pos.take_profit (phantom profit). Sprint 46Q uses the
        # actual fill price and audits the slip.
        status = {
            "orderListId": "OCO-TEST-123",
            "listOrderStatus": "ALL_DONE",
            "orderReports": [
                {"orderId": 1, "symbol": "BTCUSDT", "status": "CANCELED"},
                {"orderId": 2, "symbol": "BTCUSDT", "status": "FILLED",
                 "price": "9500.00", "stopPrice": "9900.00"},
            ],
        }
        _make_native_oco_position(
            self.repo, self.audit, broker_status=status,
        )
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit,
            broker=_FakeBrokerOCO(status),
        )
        closes = monitor.check(current_prices={"BTC-USD": 9500.0})
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0].close_reason, "OCO_FILL_OUTSIDE_TRIGGER_RANGE")
        self.assertAlmostEqual(closes[0].closed_price, 9500.0)

        # Audit event captured the slip
        with open(os.path.join(self.tmpdir, "audit.jsonl"), "r") as f:
            events = [json.loads(line) for line in f if line.strip()]
        slippage_events = [e for e in events
                           if e.get("event_type") == "OCO_FILL_SLIPPAGE"]
        self.assertEqual(len(slippage_events), 1)
        self.assertEqual(slippage_events[0].get("expected_sl"), 9900.0)
        self.assertEqual(slippage_events[0].get("actual_fill"), 9500.0)

    def test_fill_within_stop_buffer_uses_normal_stop_reason(self):
        """The inverse case: a fill at 9850 (within the 1.5% buffer
        of stop_loss=9900) is a NORMAL stop fill, not slippage. The
        OCO_FILL_OUTSIDE_TRIGGER_RANGE path should NOT fire here —
        the bot should record the standard STOP_HIT_OCO reason
        and use the actual fill price (9850) for realized PnL (the
        stop guarantee was honored within the buffer)."""
        status = {
            "orderListId": "OCO-TEST-123",
            "listOrderStatus": "ALL_DONE",
            "orderReports": [
                {"orderId": 1, "symbol": "BTCUSDT", "status": "CANCELED"},
                {"orderId": 2, "symbol": "BTCUSDT", "status": "FILLED",
                 "price": "9850.00", "stopPrice": "9900.00"},
            ],
        }
        _make_native_oco_position(
            self.repo, self.audit, broker_status=status,
        )
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit,
            broker=_FakeBrokerOCO(status),
        )
        closes = monitor.check(current_prices={"BTC-USD": 9850.0})
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0].close_reason, "STOP_HIT_OCO")
        self.assertAlmostEqual(closes[0].closed_price, 9850.0)


class StopLimitBufferDefaultTest(unittest.TestCase):
    """Sprint 46Q (audit M5 bug #3): the STOP_LOSS_LIMIT's limit price
    must sit 1.5% below the stop trigger by default, not 0.5%. The old
    0.5% was vulnerable to typical crypto gap moves; the audit's
    suggestion to widen was exactly this. 1.5% survives a typical
    overnight BTC gap and is still inside the 2x ATR stop the
    signal layer uses."""

    def test_default_stop_limit_buffer_is_1_5_pct(self):
        """The constructor default must be 1.5 — the audit's exact
        recommendation. The 0.5 pre-Sprint-46Q default was the
        safety hole."""
        import inspect
        sig = inspect.signature(BrokerClient.create_oco_sell_order)
        default = sig.parameters["stop_limit_buffer_pct"].default
        self.assertEqual(default, 1.5)

    def test_buffer_is_passed_through_to_exchange(self):
        """The buffer must be honored — verify that the STOP_LOSS_LIMIT
        limit price the function actually submits is `stop_price *
        (1 - buffer/100)`. This is the OCO docstring's contract;
        the test pins it so a future refactor can't quietly break it.
        """
        captured = {}

        class _FakeExchange:
            def market(self, symbol):
                return {"id": symbol.replace("/", "")}

            def amount_to_precision(self, symbol, amount):
                return str(amount)

            def price_to_precision(self, symbol, price):
                return f"{price:.4f}"

            def private_post_order_oco(self, params):
                captured.update(params)
                return {"orderListId": "fake"}

        class _FakeBroker:
            exchange = _FakeExchange()

        # Use a buffer of 2.0% so the expected limit price is
        # unambiguously different from both 0.5% and 1.5%.
        BrokerClient.create_oco_sell_order(
            _FakeBroker(),
            symbol="BTC/USDT",
            amount=0.001,
            take_profit_price=10500.0,
            stop_price=10000.0,
            stop_limit_buffer_pct=2.0,
        )

        # 10000 * (1 - 2/100) = 9800
        self.assertAlmostEqual(float(captured["stopLimitPrice"]), 9800.0, places=2)
        self.assertAlmostEqual(float(captured["stopPrice"]), 10000.0, places=2)


if __name__ == "__main__":
    unittest.main()
