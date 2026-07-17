"""
Carlos: "quiero que paper se comporte exactamente igual a como se
comportaria en live" -- paper mode should exercise the same gates and
the same live-price source live does, so its behavior is genuinely
representative and not a shortcut that hides what a real order would
do.

Two gaps closed in ExecutionNode.execute_order's paper-mode branch:

1. Fill price: paper used to record the fill at the STALE SIGNAL
   price (`entry_price`, from indicator/candle data that can lag the
   real market). Now it fetches a live price the same way the SL/TP
   monitor does (broker.get_ticker_price for crypto,
   broker.get_latest_trade_price for equity) BEFORE applying
   slippage, falling back to the signal price only if that live fetch
   fails.
2. Equity entries: live gates NEW entries on Alpaca's real market
   hours (`is_market_open`) and a symbol-tradeable pre-flight
   (`is_symbol_tradeable`) in `_execute_equity_order` -- paper used to
   skip straight past both, instant-filling 24/7 regardless. Paper now
   runs the identical checks (with the identical audit event types)
   before simulating the fill.

Run: python -m unittest tests.test_paper_live_parity -v
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
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository
from src.core.event_bus import EventBus


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


BROKERS_CONFIG = {
    "crypto": {"name": "binanceus", "symbols": ["BTC-USD"]},
    "equity": {"name": "alpaca", "symbols": ["SPY"]},
}


class _Harness:
    def _make(self, crypto_broker=None, alpaca_broker=None, paper_slippage_pct=0.0):
        self.tmpdir = tempfile.mkdtemp()
        self.bus = EventBus()
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.mode_override_path = _write_mode_override(self.tmpdir, False)  # paper
        return ExecutionNode(
            self.bus,
            broker_client=crypto_broker,
            alpaca_broker=alpaca_broker,
            brokers_config=BROKERS_CONFIG,
            audit=self.audit,
            position_repo=self.repo,
            mode_override_path=self.mode_override_path,
            paper_slippage_pct=paper_slippage_pct,
        )


class PaperCryptoUsesLivePriceTest(unittest.TestCase, _Harness):
    def setUp(self):
        self.crypto = MagicMock()
        self.crypto.exchange.symbols = ["BTC/USD"]
        self.node = self._make(crypto_broker=self.crypto, paper_slippage_pct=0.0)

    def _order(self, entry_price=50000.0):
        return {
            "asset": "BTC-USD", "direction": "long", "position_size": 0.001,
            "entry_price": entry_price, "stop_loss": 49000.0, "take_profit": 52000.0,
        }

    def test_fill_price_is_live_ticker_not_stale_signal_price(self):
        # Signal price is stale ($50000); the live ticker says $51200 --
        # the paper fill must record the LIVE price, not the signal.
        self.crypto.get_ticker_price.return_value = 51200.0
        self.node.execute_order(self._order(entry_price=50000.0))
        opens = self.repo.open()
        self.assertEqual(len(opens), 1)
        self.assertAlmostEqual(opens[0].entry_price, 51200.0, places=6)
        self.crypto.get_ticker_price.assert_called_once_with("BTC/USD")
        # The broker's ORDER-SUBMISSION method must never be called in
        # paper mode -- only the read-only ticker lookup.
        self.crypto.create_market_order.assert_not_called()

    def test_falls_back_to_signal_price_when_live_fetch_returns_none(self):
        self.crypto.get_ticker_price.return_value = None
        self.node.execute_order(self._order(entry_price=50000.0))
        opens = self.repo.open()
        self.assertAlmostEqual(opens[0].entry_price, 50000.0, places=6)

    def test_falls_back_to_signal_price_when_live_fetch_raises(self):
        self.crypto.get_ticker_price.side_effect = RuntimeError("network down")
        self.node.execute_order(self._order(entry_price=50000.0))
        opens = self.repo.open()
        self.assertAlmostEqual(opens[0].entry_price, 50000.0, places=6)

    def test_slippage_applies_on_top_of_live_price(self):
        node = self._make(crypto_broker=self.crypto, paper_slippage_pct=0.001)
        self.crypto.get_ticker_price.return_value = 51200.0
        node.execute_order(self._order(entry_price=50000.0))
        opens = self.repo.open()
        self.assertAlmostEqual(opens[0].entry_price, 51200.0 * 1.001, places=6)


class PaperEquityUsesLivePriceAndGatesTest(unittest.TestCase, _Harness):
    def setUp(self):
        self.alpaca = MagicMock()
        self.alpaca.is_market_open.return_value = True
        self.alpaca.is_symbol_tradeable.return_value = True
        self.node = self._make(alpaca_broker=self.alpaca, paper_slippage_pct=0.0)

    def _order(self, entry_price=500.0):
        return {
            "asset": "SPY", "direction": "long", "position_size": 0.02,
            "entry_price": entry_price, "stop_loss": 490.0, "take_profit": 520.0,
        }

    def test_fill_price_is_live_alpaca_price_not_stale_signal_price(self):
        self.alpaca.get_latest_trade_price.return_value = 505.5
        self.node.execute_order(self._order(entry_price=500.0))
        opens = self.repo.open()
        self.assertEqual(len(opens), 1)
        self.assertAlmostEqual(opens[0].entry_price, 505.5, places=6)
        self.alpaca.get_latest_trade_price.assert_called_once_with("SPY")
        self.alpaca.create_market_order.assert_not_called()

    def test_entry_skipped_when_market_closed_same_as_live(self):
        """The exact gap the audit called out: paper used to instant-fill
        24/7 with no market-hours awareness at all."""
        self.alpaca.is_market_open.return_value = False
        self.node.execute_order(self._order())
        self.assertEqual(self.repo.count_open(), 0)
        self.alpaca.is_symbol_tradeable.assert_not_called()  # gate fires first, same order as live
        self.alpaca.get_latest_trade_price.assert_not_called()
        skipped = [e for e in self.audit_events if e[0] == "TRADE_SKIPPED_MARKET_CLOSED"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][1]["asset"], "SPY")
        self.assertTrue(skipped[0][1].get("simulated"))

    def test_entry_rejected_when_symbol_not_tradeable(self):
        self.alpaca.is_symbol_tradeable.return_value = False
        self.node.execute_order(self._order())
        self.assertEqual(self.repo.count_open(), 0)
        failed = [e for e in self.audit_events if e[0] == "TRADE_FAILED"]
        self.assertEqual(len(failed), 1)
        self.assertIn("SYMBOL_NOT_TRADEABLE", failed[0][1]["status"])

    def test_entry_proceeds_and_fills_when_market_open_and_tradeable(self):
        self.alpaca.get_latest_trade_price.return_value = 501.0
        self.node.execute_order(self._order())
        self.assertEqual(self.repo.count_open(), 1)
        skipped = [e for e in self.audit_events if e[0] == "TRADE_SKIPPED_MARKET_CLOSED"]
        self.assertEqual(len(skipped), 0)

    def test_defensive_if_alpaca_broker_lacks_gate_methods(self):
        """An older/bare mock without is_market_open/is_symbol_tradeable
        must not crash paper execution -- same hasattr-guarded defensive
        contract as the live path."""
        bare_alpaca = MagicMock(spec=["get_latest_trade_price", "create_market_order"])
        bare_alpaca.get_latest_trade_price.return_value = 501.0
        node = self._make(alpaca_broker=bare_alpaca, paper_slippage_pct=0.0)
        node.execute_order(self._order())
        self.assertEqual(self.repo.count_open(), 1)


if __name__ == "__main__":
    unittest.main()
