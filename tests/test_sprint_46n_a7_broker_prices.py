"""
Sprint 46N — audit A7 tests.

Verifies `main.py`'s `_fetch_prices_for_open_positions`:
- crypto assets route to `broker_client.get_ticker_price` (ccxt-style
  symbol, "-" converted to "/")
- equity assets route to `alpaca_broker.get_latest_trade_price`
- unmapped/"unknown" assets fall back to the crypto broker (same
  backward-compat convention as `resolve_broker_for_close` in
  src/execution/broker_routing.py)
- a missing broker for an asset's class results in that asset being
  skipped (not an exception)
- a single asset's fetch failing doesn't abort the whole batch
  (best-effort, matches every other price fetch in this bot)
- yfinance / MarketAnalystAgent is no longer imported or called by
  this function at all

Run: python -m unittest tests.test_sprint_46n_a7_broker_prices -v
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main as main_module  # noqa: E402


def _pos(asset):
    p = MagicMock()
    p.asset = asset
    return p


BROKERS_CONFIG = {
    "crypto": {"name": "binanceus", "symbols": ["BTC-USD", "ETH-USD", "SOL-USD"]},
    "equity": {"name": "alpaca", "symbols": ["SPY", "QQQ", "GLD", "USO"]},
}


class CryptoRoutingTest(unittest.TestCase):
    def test_crypto_asset_uses_broker_client_ticker(self):
        repo = MagicMock()
        repo.open.return_value = [_pos("BTC-USD")]
        broker_client = MagicMock()
        broker_client.get_ticker_price.return_value = 65000.0
        alpaca_broker = MagicMock()

        prices = main_module._fetch_prices_for_open_positions(
            repo, broker_client=broker_client, alpaca_broker=alpaca_broker,
            brokers_config=BROKERS_CONFIG,
        )

        self.assertEqual(prices, {"BTC-USD": 65000.0})
        broker_client.get_ticker_price.assert_called_once_with("BTC/USD")
        alpaca_broker.get_latest_trade_price.assert_not_called()


class EquityRoutingTest(unittest.TestCase):
    def test_equity_asset_uses_alpaca_latest_trade(self):
        repo = MagicMock()
        repo.open.return_value = [_pos("SPY")]
        broker_client = MagicMock()
        alpaca_broker = MagicMock()
        alpaca_broker.get_latest_trade_price.return_value = 560.25

        prices = main_module._fetch_prices_for_open_positions(
            repo, broker_client=broker_client, alpaca_broker=alpaca_broker,
            brokers_config=BROKERS_CONFIG,
        )

        self.assertEqual(prices, {"SPY": 560.25})
        alpaca_broker.get_latest_trade_price.assert_called_once_with("SPY")
        broker_client.get_ticker_price.assert_not_called()


class UnknownAssetFallsBackToCryptoTest(unittest.TestCase):
    def test_unmapped_asset_routes_to_crypto_broker(self):
        repo = MagicMock()
        repo.open.return_value = [_pos("DOGE-USD")]  # not in BROKERS_CONFIG
        broker_client = MagicMock()
        broker_client.get_ticker_price.return_value = 0.15
        alpaca_broker = MagicMock()

        prices = main_module._fetch_prices_for_open_positions(
            repo, broker_client=broker_client, alpaca_broker=alpaca_broker,
            brokers_config=BROKERS_CONFIG,
        )

        self.assertEqual(prices, {"DOGE-USD": 0.15})
        broker_client.get_ticker_price.assert_called_once_with("DOGE/USD")


class MissingBrokerSkipsAssetTest(unittest.TestCase):
    def test_no_alpaca_broker_skips_equity_asset(self):
        repo = MagicMock()
        repo.open.return_value = [_pos("SPY")]
        broker_client = MagicMock()

        prices = main_module._fetch_prices_for_open_positions(
            repo, broker_client=broker_client, alpaca_broker=None,
            brokers_config=BROKERS_CONFIG,
        )

        self.assertEqual(prices, {})

    def test_no_crypto_broker_skips_crypto_asset(self):
        repo = MagicMock()
        repo.open.return_value = [_pos("BTC-USD")]
        alpaca_broker = MagicMock()

        prices = main_module._fetch_prices_for_open_positions(
            repo, broker_client=None, alpaca_broker=alpaca_broker,
            brokers_config=BROKERS_CONFIG,
        )

        self.assertEqual(prices, {})


class BestEffortPerAssetTest(unittest.TestCase):
    def test_one_asset_failing_does_not_abort_the_batch(self):
        repo = MagicMock()
        repo.open.return_value = [_pos("BTC-USD"), _pos("SPY"), _pos("ETH-USD")]
        broker_client = MagicMock()

        def ticker_side_effect(symbol):
            if symbol == "BTC/USD":
                raise RuntimeError("network hiccup (simulated)")
            return 3500.0

        broker_client.get_ticker_price.side_effect = ticker_side_effect
        alpaca_broker = MagicMock()
        alpaca_broker.get_latest_trade_price.return_value = None  # e.g. bad symbol

        prices = main_module._fetch_prices_for_open_positions(
            repo, broker_client=broker_client, alpaca_broker=alpaca_broker,
            brokers_config=BROKERS_CONFIG,
        )

        # BTC-USD's fetch raised -> skipped. SPY returned None -> skipped.
        # ETH-USD succeeded -> present.
        self.assertEqual(prices, {"ETH-USD": 3500.0})

    def test_no_open_positions_returns_empty_dict_without_calling_brokers(self):
        repo = MagicMock()
        repo.open.return_value = []
        broker_client = MagicMock()
        alpaca_broker = MagicMock()

        prices = main_module._fetch_prices_for_open_positions(
            repo, broker_client=broker_client, alpaca_broker=alpaca_broker,
            brokers_config=BROKERS_CONFIG,
        )

        self.assertEqual(prices, {})
        broker_client.get_ticker_price.assert_not_called()
        alpaca_broker.get_latest_trade_price.assert_not_called()


class NoYfinanceDependencyTest(unittest.TestCase):
    def test_market_analyst_agent_never_used_by_price_fetch(self):
        """Sprint 46N (audit A7): this function used to construct a
        MarketAnalystAgent and call `.fetch_one(..., interval="1d")`
        (yfinance under the hood) for every open position. Confirm
        that path is gone: patch MarketAnalystAgent to blow up if
        touched, and confirm a normal crypto+equity fetch still works
        without ever instantiating it.
        """
        import src.agents.market_analyst as ma_module

        original = ma_module.MarketAnalystAgent

        class _ExplodingMA:
            def __init__(self, *a, **kw):
                raise AssertionError(
                    "MarketAnalystAgent must not be constructed by "
                    "_fetch_prices_for_open_positions (audit A7 removed "
                    "the yfinance dependency from this function)"
                )

        ma_module.MarketAnalystAgent = _ExplodingMA
        try:
            repo = MagicMock()
            repo.open.return_value = [_pos("BTC-USD"), _pos("SPY")]
            broker_client = MagicMock()
            broker_client.get_ticker_price.return_value = 65000.0
            alpaca_broker = MagicMock()
            alpaca_broker.get_latest_trade_price.return_value = 560.0

            prices = main_module._fetch_prices_for_open_positions(
                repo, broker_client=broker_client, alpaca_broker=alpaca_broker,
                brokers_config=BROKERS_CONFIG,
            )

            self.assertEqual(prices, {"BTC-USD": 65000.0, "SPY": 560.0})
        finally:
            ma_module.MarketAnalystAgent = original


class DefaultArgumentsTest(unittest.TestCase):
    def test_no_brokers_at_all_returns_empty_without_raising(self):
        """Sanity: calling with all-default (no brokers, no config) —
        e.g. a stray caller that forgot to pass the new params — must
        not raise, just skip everything (fail-open, matches the rest
        of this best-effort helper).
        """
        repo = MagicMock()
        repo.open.return_value = [_pos("BTC-USD"), _pos("SPY")]

        prices = main_module._fetch_prices_for_open_positions(repo)

        self.assertEqual(prices, {})


if __name__ == "__main__":
    unittest.main()
