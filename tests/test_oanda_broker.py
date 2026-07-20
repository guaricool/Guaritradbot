"""
OandaBroker — forex broker client (EUR/USD, GBP/USD, etc. via OANDA
v20 API). Mirrors AlpacaBroker's interface shape so ExecutionNode/
RiskManagerAgent/PositionMonitor can treat forex as a third asset
class the same way they already treat equity vs crypto.

Run: python -m unittest tests.test_oanda_broker -v
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.execution.oanda_broker import (
    OandaBroker,
    to_oanda_instrument,
    from_oanda_instrument,
)


class SymbolConversionTest(unittest.TestCase):
    def test_yfinance_to_oanda(self):
        self.assertEqual(to_oanda_instrument("EURUSD=X"), "EUR_USD")
        self.assertEqual(to_oanda_instrument("GBPUSD=X"), "GBP_USD")
        self.assertEqual(to_oanda_instrument("USDJPY=X"), "USD_JPY")

    def test_already_oanda_format_passthrough(self):
        self.assertEqual(to_oanda_instrument("EUR_USD"), "EUR_USD")

    def test_oanda_to_yfinance(self):
        self.assertEqual(from_oanda_instrument("EUR_USD"), "EURUSD=X")
        self.assertEqual(from_oanda_instrument("USD_JPY"), "USDJPY=X")

    def test_roundtrip(self):
        for sym in ("EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCAD=X", "AUDUSD=X"):
            self.assertEqual(from_oanda_instrument(to_oanda_instrument(sym)), sym)


class OandaBrokerConstructionTest(unittest.TestCase):
    def test_requires_token_and_account(self):
        with self.assertRaises(ValueError):
            OandaBroker(api_token=None, account_id=None)

    def test_defaults_to_practice_environment(self):
        broker = OandaBroker(api_token="tok", account_id="101-001-1-001")
        self.assertEqual(broker.environment, "practice")

    def test_invalid_environment_falls_back_to_practice(self):
        broker = OandaBroker(api_token="tok", account_id="101-001-1-001", environment="production")
        self.assertEqual(broker.environment, "practice")

    def test_live_environment_honored(self):
        broker = OandaBroker(api_token="tok", account_id="101-001-1-001", environment="live")
        self.assertEqual(broker.environment, "live")

    def test_reads_from_env_vars(self):
        with patch.dict(os.environ, {"OANDA_API_TOKEN": "envtok", "OANDA_ACCOUNT_ID": "101-001-2-002"}):
            broker = OandaBroker()
            self.assertEqual(broker.api_token, "envtok")
            self.assertEqual(broker.account_id, "101-001-2-002")


class OandaBrokerBalanceTest(unittest.TestCase):
    def _make_broker(self):
        return OandaBroker(api_token="tok", account_id="101-001-1-001")

    def test_get_usd_balance_success(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.return_value = {"account": {"NAV": "987.65"}}
        self.assertEqual(broker.get_usd_balance(), 987.65)

    def test_get_usd_balance_failure_returns_zero(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.side_effect = RuntimeError("network error")
        self.assertEqual(broker.get_usd_balance(), 0.0)


class OandaBrokerMarketHoursTest(unittest.TestCase):
    def _make_broker(self):
        return OandaBroker(api_token="tok", account_id="101-001-1-001")

    def test_market_open_when_tradeable_true(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.return_value = {"prices": [{"tradeable": True}]}
        self.assertTrue(broker.is_market_open())

    def test_market_closed_when_tradeable_false(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.return_value = {"prices": [{"tradeable": False}]}
        self.assertFalse(broker.is_market_open())

    def test_fails_open_on_error(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.side_effect = RuntimeError("timeout")
        self.assertTrue(broker.is_market_open())


class OandaBrokerPriceTest(unittest.TestCase):
    def _make_broker(self):
        return OandaBroker(api_token="tok", account_id="101-001-1-001")

    def test_get_latest_trade_price_mid_of_bid_ask(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.return_value = {
            "prices": [{"bids": [{"price": "1.0800"}], "asks": [{"price": "1.0802"}]}]
        }
        price = broker.get_latest_trade_price("EURUSD=X")
        self.assertAlmostEqual(price, 1.0801, places=4)

    def test_get_latest_trade_price_accepts_oanda_format_too(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.return_value = {
            "prices": [{"bids": [{"price": "1.0800"}], "asks": [{"price": "1.0802"}]}]
        }
        price = broker.get_latest_trade_price("EUR_USD")
        self.assertAlmostEqual(price, 1.0801, places=4)

    def test_get_latest_trade_price_failure_returns_none(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.side_effect = RuntimeError("boom")
        self.assertIsNone(broker.get_latest_trade_price("EURUSD=X"))


class OandaBrokerOrderTest(unittest.TestCase):
    def _make_broker(self):
        return OandaBroker(api_token="tok", account_id="101-001-1-001")

    def test_buy_order_sends_positive_units(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.return_value = {
            "orderFillTransaction": {"id": "1234", "units": "500", "price": "1.0801", "time": "2026-07-20T10:00:00Z"}
        }
        result = broker.create_market_order("EURUSD=X", "buy", 500)
        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["symbol"], "EUR_USD")
        sent_data = broker._client.request.call_args[0][0].data
        self.assertEqual(sent_data["order"]["units"], "500")

    def test_sell_order_sends_negative_units(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.return_value = {
            "orderFillTransaction": {"id": "1235", "units": "-500", "price": "1.0801", "time": "2026-07-20T10:00:00Z"}
        }
        result = broker.create_market_order("EURUSD=X", "sell", 500)
        self.assertEqual(result["status"], "filled")
        sent_data = broker._client.request.call_args[0][0].data
        self.assertEqual(sent_data["order"]["units"], "-500")

    def test_invalid_side_fails_without_calling_api(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        result = broker.create_market_order("EURUSD=X", "hold", 500)
        self.assertEqual(result["status"], "failed")
        broker._client.request.assert_not_called()

    def test_order_not_filled_reports_failure(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.return_value = {
            "orderCancelTransaction": {"reason": "MARKET_HALTED"}
        }
        result = broker.create_market_order("EURUSD=X", "buy", 500)
        self.assertEqual(result["status"], "failed")
        self.assertIn("MARKET_HALTED", result["error"])

    def test_broker_exception_reports_failure(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.side_effect = RuntimeError("connection reset")
        result = broker.create_market_order("EURUSD=X", "buy", 500)
        self.assertEqual(result["status"], "failed")
        self.assertIn("connection reset", result["error"])

    def test_units_round_to_whole_number_minimum_one(self):
        broker = self._make_broker()
        broker._client = MagicMock()
        broker._client.request.return_value = {
            "orderFillTransaction": {"id": "1", "units": "1", "price": "1.08", "time": "2026-07-20T10:00:00Z"}
        }
        broker.create_market_order("EURUSD=X", "buy", 0.3)
        sent_data = broker._client.request.call_args[0][0].data
        self.assertEqual(sent_data["order"]["units"], "1")


if __name__ == "__main__":
    unittest.main()
