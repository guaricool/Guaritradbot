"""
Sprint 36 — Multi-broker routing tests.

Verifies:
- Asset → broker routing by `brokers_config` (SPY → alpaca, BTC-USD → binanceus)
- Equity orders hit AlpacaBroker (not the default crypto broker)
- Equity orders without a configured AlpacaBroker fail with ALPACA_NOT_CONFIGURED
- AlpacaBroker.create_market_order handles notional_usd vs amount
- Paper mode simulates equity orders (no broker call)
- Symbol validation against Alpaca via is_symbol_tradeable
- Symbol not tradeable on Alpaca → SYMBOL_NOT_TRADEABLE before broker call

Run: python -m unittest tests.test_alpaca_broker_sprint36 -v
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

from src.execution.execution_node import ExecutionNode
from src.execution.alpaca_broker import AlpacaBroker
from src.core.event_bus import EventBus


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


def _make_alpaca(symbols_tradeable=("SPY", "QQQ", "GLD", "USO")):
    """Build a mock AlpacaBroker with the given tradeable symbols."""
    broker = MagicMock(spec=AlpacaBroker)
    broker.is_symbol_tradeable.side_effect = lambda s: s in symbols_tradeable
    broker.get_usd_balance.return_value = 100.0
    broker.create_market_order.return_value = {
        "id": "ALP_FAKE_1",
        "status": "filled",
        "symbol": "SPY",
        "side": "buy",
        "qty": "0.0133",
        "notional": "10.0",
    }
    return broker


def _make_crypto_broker(supported=("BTC/USD", "ETH/USD", "SOL/USD")):
    broker = MagicMock()
    exchange = MagicMock()
    exchange.id = "binanceus"
    exchange.symbols = supported
    broker.exchange = exchange
    broker.create_market_order.return_value = {"id": "BIN_FAKE_1", "status": "filled"}
    return broker


BROKERS_CONFIG = {
    "crypto": {"name": "binanceus", "symbols": ["BTC-USD", "ETH-USD", "SOL-USD"]},
    "equity": {"name": "alpaca", "symbols": ["SPY", "QQQ", "GLD", "USO"]},
}


class AlpacaBrokerRoutingTest(unittest.TestCase):
    """Equity orders must go to Alpaca, crypto must stay on binanceus."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Live mode (so the paper gate doesn't kick in)
        self.mode_override_path = _write_mode_override(self.tmpdir, True)
        self.bus = EventBus()
        self.crypto = _make_crypto_broker()
        self.alpaca = _make_alpaca()
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))

        self.node = ExecutionNode(
            self.bus,
            execution_mode="auto",
            broker_client=self.crypto,
            alpaca_broker=self.alpaca,
            brokers_config=BROKERS_CONFIG,
            kill_switch=None,
            audit=self.audit,
            mode_override_path=self.mode_override_path,
        )

    def test_spy_routes_to_alpaca_not_crypto(self):
        order = {
            "asset": "SPY", "direction": "long",
            "position_size": 0.0133,
            "entry_price": 750.0,
            "stop_loss": 740, "take_profit": 770,
        }
        self.node.execute_order(order)
        # Alpaca was called
        self.alpaca.create_market_order.assert_called_once()
        # Crypto broker was NOT called
        self.crypto.create_market_order.assert_not_called()
        # Verify Alpaca was called with notional_usd (fractional)
        call_args = self.alpaca.create_market_order.call_args
        call_kwargs = call_args.kwargs
        self.assertAlmostEqual(call_kwargs["notional_usd"], 0.0133 * 750.0, places=2)
        self.assertEqual(call_args.args[0], "SPY")    # symbol
        self.assertEqual(call_args.args[1], "buy")    # side
        # Audit recorded the fill with asset_class=equity
        fills = [e for e in self.audit_events if e[0] == "TRADE_FILLED"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0][1]["status"], "FILLED (LIVE MARKET — ALPACA)")
        self.assertEqual(fills[0][1]["asset_class"], "equity")
        self.assertEqual(fills[0][1]["broker"], "alpaca")
        self.assertEqual(fills[0][1]["order_kind"], "notional")

    def test_btc_routes_to_crypto_broker(self):
        """Regression: crypto orders must STILL go to binanceus, not Alpaca."""
        order = {
            "asset": "BTC-USD", "direction": "long",
            "position_size": 0.001,
            "entry_price": 50000, "stop_loss": 49000, "take_profit": 52000,
        }
        self.node.execute_order(order)
        # Crypto broker was called
        self.crypto.create_market_order.assert_called_once()
        # Alpaca was NOT called
        self.alpaca.create_market_order.assert_not_called()
        fills = [e for e in self.audit_events if e[0] == "TRADE_FILLED"]
        self.assertEqual(fills[0][1]["asset_class"], "crypto")

    def test_gld_routes_to_alpaca(self):
        order = {
            "asset": "GLD", "direction": "long",
            "position_size": 0.05,
            "entry_price": 200.0,
            "stop_loss": 195, "take_profit": 210,
        }
        self.node.execute_order(order)
        self.alpaca.create_market_order.assert_called_once()
        call_args = self.alpaca.create_market_order.call_args
        self.assertEqual(call_args.args[0], "GLD")
        self.assertAlmostEqual(call_args.kwargs["notional_usd"], 0.05 * 200.0, places=2)


class AlpacaNotConfiguredTest(unittest.TestCase):
    """When equity asset arrives but alpaca_broker is None, fail loud."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mode_override_path = _write_mode_override(self.tmpdir, True)
        self.bus = EventBus()
        self.crypto = _make_crypto_broker()
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))

        self.node = ExecutionNode(
            self.bus,
            execution_mode="auto",
            broker_client=self.crypto,
            alpaca_broker=None,           # ← the key part
            brokers_config=BROKERS_CONFIG,
            kill_switch=None,
            audit=self.audit,
            mode_override_path=self.mode_override_path,
        )

    def test_spy_fails_alpaca_not_configured(self):
        order = {
            "asset": "SPY", "direction": "long",
            "position_size": 0.0133, "entry_price": 750,
        }
        self.node.execute_order(order)
        # No broker was called
        self.crypto.create_market_order.assert_not_called()
        # Audit recorded the failure
        fails = [e for e in self.audit_events if e[0] == "TRADE_FAILED"]
        self.assertEqual(len(fails), 1)
        self.assertIn("ALPACA_NOT_CONFIGURED", fails[0][1]["status"])
        self.assertEqual(fails[0][1]["asset_class"], "equity")


class EquityPaperModeTest(unittest.TestCase):
    """Paper mode should simulate equity orders, not call Alpaca."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Paper mode
        self.mode_override_path = _write_mode_override(self.tmpdir, False)
        self.bus = EventBus()
        self.alpaca = _make_alpaca()
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))

        self.node = ExecutionNode(
            self.bus,
            execution_mode="auto",
            broker_client=None,
            alpaca_broker=self.alpaca,
            brokers_config=BROKERS_CONFIG,
            kill_switch=None,
            audit=self.audit,
            mode_override_path=self.mode_override_path,
        )

    def test_spy_paper_does_not_call_alpaca(self):
        order = {
            "asset": "SPY", "direction": "long",
            "position_size": 0.0133, "entry_price": 750,
        }
        self.node.execute_order(order)
        # Alpaca was NOT called
        self.alpaca.create_market_order.assert_not_called()
        # Audit recorded the simulated fill
        fills = [e for e in self.audit_events if e[0] == "TRADE_FILLED"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0][1]["status"], "FILLED (PAPER)")
        self.assertTrue(fills[0][1]["simulated"])
        self.assertEqual(fills[0][1]["asset_class"], "equity")


class EquitySymbolValidationTest(unittest.TestCase):
    """is_symbol_tradeable=False should reject before hitting the broker."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mode_override_path = _write_mode_override(self.tmpdir, True)
        self.bus = EventBus()
        # Alpaca with only SPY tradeable, NOT QQQ
        self.alpaca = _make_alpaca(symbols_tradeable=("SPY",))
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))

        self.node = ExecutionNode(
            self.bus,
            execution_mode="auto",
            broker_client=None,
            alpaca_broker=self.alpaca,
            brokers_config=BROKERS_CONFIG,
            kill_switch=None,
            audit=self.audit,
            mode_override_path=self.mode_override_path,
        )

    def test_qqq_rejected_before_broker_call(self):
        order = {
            "asset": "QQQ", "direction": "long",
            "position_size": 0.01, "entry_price": 500,
        }
        self.node.execute_order(order)
        # Alpaca was never called (we returned at the validation gate)
        self.alpaca.create_market_order.assert_not_called()
        # Audit recorded the failure
        fails = [e for e in self.audit_events if e[0] == "TRADE_FAILED"]
        self.assertEqual(len(fails), 1)
        self.assertIn("SYMBOL_NOT_TRADEABLE", fails[0][1]["status"])
        self.assertEqual(fails[0][1]["asset"], "QQQ")


class AlpacaBrokerCreateOrderTest(unittest.TestCase):
    """Direct unit tests of AlpacaBroker.create_market_order (with mock TradingClient)."""

    def _broker_with_mock_trading_client(self, mock_trading_client):
        with patch("alpaca.trading.client.TradingClient", return_value=mock_trading_client, create=True):
            return AlpacaBroker(api_key="FAKE_KEY", secret_key="FAKE_SECRET", paper=True)

    def test_notional_usd_calls_submit_with_notional(self):
        mock_tc = MagicMock()
        order = MagicMock()
        order.id = "ord_1"
        order.status = "filled"
        order.symbol = "SPY"
        order.side = "buy"
        order.qty = "0.0133"
        order.notional = "10.00"
        order.submitted_at = "2026-07-10T12:00:00Z"
        mock_tc.submit_order.return_value = order

        broker = self._broker_with_mock_trading_client(mock_tc)
        result = broker.create_market_order("SPY", "buy", notional_usd=10.00)
        self.assertEqual(result["status"], "filled")
        # Verify submit_order was called with a MarketOrderRequest
        self.assertTrue(mock_tc.submit_order.called)
        req_arg = mock_tc.submit_order.call_args.args[0]
        self.assertEqual(req_arg.notional, 10.00)
        self.assertIsNone(req_arg.qty)
        self.assertEqual(req_arg.symbol, "SPY")

    def test_amount_calls_submit_with_qty(self):
        mock_tc = MagicMock()
        order = MagicMock()
        order.id = "ord_2"
        order.status = "filled"
        order.symbol = "AAPL"
        order.side = "buy"
        order.qty = "5"
        order.notional = None
        order.submitted_at = "2026-07-10T12:00:00Z"
        mock_tc.submit_order.return_value = order

        broker = self._broker_with_mock_trading_client(mock_tc)
        result = broker.create_market_order("AAPL", "buy", amount=5)
        self.assertEqual(result["status"], "filled")
        req_arg = mock_tc.submit_order.call_args.args[0]
        self.assertEqual(req_arg.qty, 5)
        self.assertIsNone(req_arg.notional)

    def test_both_amount_and_notional_rejected(self):
        broker = self._broker_with_mock_trading_client(MagicMock())
        result = broker.create_market_order("SPY", "buy", amount=1, notional_usd=10)
        self.assertEqual(result["status"], "failed")
        self.assertIn("exactly one of", result["error"])

    def test_neither_amount_nor_notional_rejected(self):
        broker = self._broker_with_mock_trading_client(MagicMock())
        result = broker.create_market_order("SPY", "buy")
        self.assertEqual(result["status"], "failed")

    def test_invalid_side_rejected(self):
        broker = self._broker_with_mock_trading_client(MagicMock())
        result = broker.create_market_order("SPY", "hold", amount=1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("invalid side", result["error"])

    def test_notional_below_alpaca_minimum_rejected(self):
        broker = self._broker_with_mock_trading_client(MagicMock())
        result = broker.create_market_order("SPY", "buy", notional_usd=0.50)
        self.assertEqual(result["status"], "failed")
        self.assertIn("minimum $1.00", result["error"])

    def test_submit_exception_returns_failed(self):
        mock_tc = MagicMock()
        mock_tc.submit_order.side_effect = Exception("network down")
        broker = self._broker_with_mock_trading_client(mock_tc)
        result = broker.create_market_order("SPY", "buy", amount=1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("network down", result["error"])


class AlpacaBrokerInitTest(unittest.TestCase):
    def test_missing_keys_raises_value_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                AlpacaBroker(paper=True)

    def test_import_error_when_alpaca_py_missing(self):
        with patch.dict(os.environ, {"ALPACA_API_KEY": "x", "ALPACA_SECRET_KEY": "y"}):
            with patch.dict(sys.modules, {"alpaca.trading.client": None}):
                with self.assertRaises(ImportError):
                    AlpacaBroker(paper=True)


if __name__ == "__main__":
    unittest.main()
