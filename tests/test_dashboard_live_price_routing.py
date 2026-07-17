"""
Real-time dashboard fix: `src.api.state._fetch_one_price` used to
ALWAYS use yfinance's daily-candle close, so the dashboard's displayed
"current price" / unrealized P&L could not visibly move within a
session even though the underlying asset was moving live. Fixed to
route through the same live broker feed `main.py`'s SL/TP checker uses
(ccxt ticker for crypto, Alpaca's market-data API for equities),
falling back to yfinance 1-minute bars (not daily) only when the
broker isn't configured or the call fails.

These tests exercise `_fetch_one_price` directly with mocked brokers.
"""
import unittest
from unittest.mock import patch, MagicMock

import src.api.state as state


class CryptoRoutesToLiveBrokerTickerTest(unittest.TestCase):
    def setUp(self):
        self.broker = MagicMock()
        state.set_brokers(broker_client=self.broker, alpaca_broker=None)

    def tearDown(self):
        state.set_brokers(broker_client=None, alpaca_broker=None)

    def test_btc_usd_routes_to_broker_with_slash_symbol(self):
        self.broker.get_ticker_price.return_value = 97850.2
        price, source = state._fetch_one_price("BTC-USD")
        self.assertEqual(price, 97850.2)
        self.assertEqual(source, "live")
        self.broker.get_ticker_price.assert_called_once_with("BTC/USD")

    def test_broker_failure_falls_back_to_yfinance_intraday(self):
        self.broker.get_ticker_price.return_value = None
        with patch("src.data.yf_safe.safe_yf_download") as mock_dl:
            import pandas as pd
            mock_dl.return_value = pd.DataFrame({"Close": [96000.0, 96500.0]})
            price, source = state._fetch_one_price("BTC-USD")
        self.assertEqual(price, 96500.0)
        self.assertEqual(source, "live")
        # Bug fix: must request 1-minute intraday bars, not a daily close.
        _, kwargs = mock_dl.call_args
        self.assertEqual(kwargs.get("interval"), "1m")

    def test_broker_exception_falls_back_without_raising(self):
        self.broker.get_ticker_price.side_effect = RuntimeError("boom")
        with patch("src.data.yf_safe.safe_yf_download") as mock_dl:
            import pandas as pd
            mock_dl.return_value = pd.DataFrame({"Close": [50.0]})
            price, source = state._fetch_one_price("BTC-USD")
        self.assertEqual(price, 50.0)


class EquityRoutesToAlpacaTest(unittest.TestCase):
    def setUp(self):
        self.alpaca = MagicMock()
        state.set_brokers(broker_client=None, alpaca_broker=self.alpaca)

    def tearDown(self):
        state.set_brokers(broker_client=None, alpaca_broker=None)

    def test_spy_routes_to_alpaca_latest_trade_price(self):
        self.alpaca.get_latest_trade_price.return_value = 589.2
        price, source = state._fetch_one_price("SPY")
        self.assertEqual(price, 589.2)
        self.assertEqual(source, "live")
        self.alpaca.get_latest_trade_price.assert_called_once_with("SPY")


class NoBrokerConfiguredFallsBackTest(unittest.TestCase):
    def setUp(self):
        state.set_brokers(broker_client=None, alpaca_broker=None)

    def test_no_brokers_falls_back_to_yfinance_intraday(self):
        with patch("src.data.yf_safe.safe_yf_download") as mock_dl:
            import pandas as pd
            mock_dl.return_value = pd.DataFrame({"Close": [241.2]})
            price, source = state._fetch_one_price("GLD")
        self.assertEqual(price, 241.2)
        self.assertEqual(source, "live")

    def test_all_paths_fail_returns_fetch_failed(self):
        with patch("src.data.yf_safe.safe_yf_download") as mock_dl:
            mock_dl.side_effect = Exception("network down")
            price, source = state._fetch_one_price("GLD")
        self.assertIsNone(price)
        self.assertEqual(source, "fetch_failed")


if __name__ == "__main__":
    unittest.main()
