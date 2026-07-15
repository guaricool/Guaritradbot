"""
Sprint 46N tests — audit finding C4 (AUDITORIA_COMPLETA_2026-07-11.md).

C4: `_persist_filled_position` always used the REQUESTED qty/price (what
RiskManagerAgent asked for), never what the broker actually filled.
binance.us deducts its taker fee from the BASE asset by default on a BUY
(e.g. buying BTC, the fee is taken in BTC), so the account ends up
holding slightly LESS than requested. The bot would then try to SELL the
full requested qty later (to close the position, or when placing a
protective OCO order) and get "insufficient balance" — this was the
actual root cause of the CLOSE_FAILED loop from the 2026-07-10/11 live
incident, distinct from (but related to) the long+short bug Sprint 46M
already fixed.

This file covers:
  - `_extract_crypto_fill`: nets a base-asset fee out of the filled qty,
    from both the singular `fee` dict and the `fees` list ccxt shapes.
  - `_extract_alpaca_fill`: reads Alpaca's filled/filled_avg_price keys,
    no fee-netting (Alpaca is commission-free).
  - `_persist_filled_position`: actual_qty/actual_entry_price override
    the requested values when provided and positive; risk_usd scales
    proportionally; falls back to requested values when not provided
    (paper mode / no broker data).
  - `_execute_crypto_order`: the OCO sell amount uses the actual
    (fee-netted) filled qty, not the requested amount — selling more
    than the account actually holds would reject the OCO order.

Run: python -m unittest tests.test_sprint_46n_c4_fill_persistence -v
"""
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.core.event_bus import EventBus
from src.data_store.positions import PositionRepository
from src.execution.execution_node import (
    ExecutionNode,
    _extract_crypto_fill,
    _extract_alpaca_fill,
)
from src.safety.audit_ledger import AuditLedger


class ExtractCryptoFillTest(unittest.TestCase):
    def test_nets_single_fee_dict_in_base_asset(self):
        order = {
            "filled": 0.001,
            "average": 60000.0,
            "fee": {"currency": "BTC", "cost": 0.000001},
        }
        qty, price = _extract_crypto_fill(order, "BTC/USD")
        self.assertAlmostEqual(qty, 0.000999)
        self.assertEqual(price, 60000.0)

    def test_nets_fees_list_summing_base_asset_entries(self):
        order = {
            "filled": 0.001,
            "price": 60000.0,
            "fees": [
                {"currency": "BTC", "cost": 0.0000005},
                {"currency": "BTC", "cost": 0.0000005},
                {"currency": "USD", "cost": 0.06},  # different currency, ignored
            ],
        }
        qty, price = _extract_crypto_fill(order, "BTC/USD")
        self.assertAlmostEqual(qty, 0.000999)
        self.assertEqual(price, 60000.0)

    def test_no_fee_entries_returns_raw_filled(self):
        order = {"filled": 0.5, "average": 100.0}
        qty, price = _extract_crypto_fill(order, "ETH/USD")
        self.assertEqual(qty, 0.5)
        self.assertEqual(price, 100.0)

    def test_fee_in_different_currency_not_netted(self):
        """A fee paid in a separate balance (e.g. BNB) must not reduce
        the reported base-asset qty."""
        order = {
            "filled": 0.001,
            "average": 60000.0,
            "fee": {"currency": "BNB", "cost": 0.01},
        }
        qty, price = _extract_crypto_fill(order, "BTC/USD")
        self.assertEqual(qty, 0.001)

    def test_zero_filled_returns_none(self):
        qty, price = _extract_crypto_fill({"filled": 0}, "BTC/USD")
        self.assertIsNone(qty)
        self.assertIsNone(price)

    def test_non_dict_returns_none(self):
        qty, price = _extract_crypto_fill(None, "BTC/USD")
        self.assertIsNone(qty)
        self.assertIsNone(price)

    def test_fee_math_going_negative_falls_back_to_raw_filled(self):
        """Defensive: if fee netting would produce <= 0, don't persist
        garbage — fall back to the raw filled qty."""
        order = {
            "filled": 0.001,
            "average": 100.0,
            "fee": {"currency": "BTC", "cost": 0.002},  # bigger than filled
        }
        qty, price = _extract_crypto_fill(order, "BTC/USD")
        self.assertEqual(qty, 0.001)


class ExtractAlpacaFillTest(unittest.TestCase):
    def test_reads_filled_and_avg_price(self):
        order = {"filled": "1.0133", "filled_avg_price": "500.25"}
        qty, price = _extract_alpaca_fill(order)
        self.assertAlmostEqual(qty, 1.0133)
        self.assertAlmostEqual(price, 500.25)

    def test_zero_filled_returns_none(self):
        qty, price = _extract_alpaca_fill({"filled": "0"})
        self.assertIsNone(qty)
        self.assertIsNone(price)

    def test_missing_price_returns_none_price_but_qty(self):
        qty, price = _extract_alpaca_fill({"filled": "1.0"})
        self.assertEqual(qty, 1.0)
        self.assertIsNone(price)


def _make_order_data(asset="BTC-USD", qty=0.001, entry_price=60000.0, risk_usd=10.0):
    return {
        "asset": asset,
        "direction": "long",
        "position_size": qty,
        "entry_price": entry_price,
        "stop_loss": 58000.0,
        "take_profit": 65000.0,
        "risk_usd": risk_usd,
        "strategy": "TestStrategy",
    }


class PersistFilledPositionActualFillTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.bus = EventBus()
        self.node = ExecutionNode(
            self.bus,
            execution_mode="auto",
            position_repo=self.repo,
            audit=self.audit,
        )

    def test_actual_qty_and_price_override_requested(self):
        order_data = _make_order_data(qty=0.001, entry_price=60000.0, risk_usd=10.0)
        self.node._persist_filled_position(
            order_data, "FILLED (LIVE MARKET)",
            actual_qty=0.000999, actual_entry_price=59950.0,
        )
        open_positions = self.repo.open()
        self.assertEqual(len(open_positions), 1)
        pos = open_positions[0]
        self.assertEqual(pos.qty, 0.000999)
        self.assertEqual(pos.entry_price, 59950.0)
        # risk_usd scaled proportionally: 10.0 * (0.000999/0.001)
        self.assertAlmostEqual(pos.risk_usd, 10.0 * (0.000999 / 0.001))

    def test_no_actual_values_falls_back_to_requested(self):
        """Paper mode / no broker data: must preserve exact pre-46N
        behavior of trusting the requested qty/price."""
        order_data = _make_order_data(qty=0.001, entry_price=60000.0, risk_usd=10.0)
        self.node._persist_filled_position(order_data, "FILLED (PAPER)")
        pos = self.repo.open()[0]
        self.assertEqual(pos.qty, 0.001)
        self.assertEqual(pos.entry_price, 60000.0)
        self.assertEqual(pos.risk_usd, 10.0)

    def test_realigns_sl_and_tp_when_actual_price_differs(self):
        """When actual_entry_price differs from requested, stop_loss and
        take_profit must be adjusted by the price difference."""
        order_data = _make_order_data(qty=0.001, entry_price=60000.0, risk_usd=10.0)
        self.node._persist_filled_position(
            order_data, "FILLED (LIVE MARKET)",
            actual_qty=0.001, actual_entry_price=59950.0,
        )
        pos = self.repo.open()[0]
        self.assertEqual(pos.entry_price, 59950.0)
        # Delta is -50.0. SL should be 58000 - 50 = 57950. TP should be 65000 - 50 = 64950.
        self.assertEqual(pos.stop_loss, 57950.0)
        self.assertEqual(pos.take_profit, 64950.0)

    def test_actual_qty_zero_or_negative_ignored(self):
        """A defensive zero/negative actual_qty must not override the
        requested qty (would persist a non-positive position size)."""
        order_data = _make_order_data(qty=0.001, entry_price=60000.0, risk_usd=10.0)
        self.node._persist_filled_position(
            order_data, "FILLED (LIVE MARKET)",
            actual_qty=0, actual_entry_price=0,
        )
        pos = self.repo.open()[0]
        self.assertEqual(pos.qty, 0.001)
        self.assertEqual(pos.entry_price, 60000.0)


class _FakeCryptoBrokerWithFee:
    """Simulates a binance.us BUY fill where ccxt reports the GROSS
    filled amount plus a separate fee dict — the taker fee is deducted
    from the base asset (BTC), so the account actually ends up holding
    `gross_filled - fee_cost`. Records the amount requested for any OCO
    sell order placed afterward, so the test can assert it uses the
    NET (actual) amount, not the gross fill or the originally requested
    order size."""

    def __init__(self, gross_filled=0.001, fee_cost=0.000001):
        self.gross_filled = gross_filled
        self.fee_cost = fee_cost
        self.oco_calls = []
        self.exchange = type("FakeExchange", (), {"symbols": ["BTC/USD"], "id": "binanceus"})()

    def create_market_order(self, symbol, side, amount):
        return {
            "id": "fake_order",
            "symbol": symbol,
            "side": side,
            "status": "filled",
            "filled": self.gross_filled,
            "average": 60000.0,
            "fee": {"currency": "BTC", "cost": self.fee_cost},
        }

    def create_oco_sell_order(self, symbol, amount, take_profit_price, stop_price,
                              stop_limit_buffer_pct=1.5):
        # Sprint 46Q (audit M5): ExecutionNode now passes the configurable
        # `stop_limit_buffer_pct` so the STOP_LOSS_LIMIT's limit price
        # sits the configured % below the stop trigger. The fake broker
        # accepts it as a kwarg (mirroring the real `BrokerClient` sig)
        # so the call doesn't TypeError before the assertions can run.
        self.oco_calls.append(amount)
        return {"status": "ok", "orderListId": "123"}


class CryptoOrderUsesActualFillForOcoTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.bus = EventBus()

    def test_oco_amount_uses_actual_filled_not_requested(self):
        broker = _FakeCryptoBrokerWithFee(gross_filled=0.001, fee_cost=0.000001)
        node = ExecutionNode(
            self.bus,
            execution_mode="auto",
            broker_client=broker,
            brokers_config={"crypto": {"symbols": ["BTC/USD"]}},
            position_repo=self.repo,
            audit=self.audit,
            use_native_crypto_stops=True,
        )
        order_data = _make_order_data(asset="BTC/USD", qty=0.001, entry_price=60000.0)

        node._execute_crypto_order(order_data, broker)

        self.assertEqual(len(broker.oco_calls), 1)
        self.assertAlmostEqual(broker.oco_calls[0], 0.000999)
        pos = self.repo.open()[0]
        self.assertAlmostEqual(pos.qty, 0.000999)
        self.assertEqual(pos.protection_mode, "native_oco")


if __name__ == "__main__":
    unittest.main()
