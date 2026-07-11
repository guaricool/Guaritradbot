"""
Sprint 46N tests — audit findings C1 + C2 (AUDITORIA_COMPLETA_2026-07-11.md).

C1: closes/replacements were ALWAYS sent to the single crypto broker
    (`self.broker`, a ccxt/binance.us client), even for equity assets
    (SPY/QQQ/GLD/USO). A ccxt client rejects an equity symbol, so
    equity closes failed forever (CLOSE_FAILED loop, position stuck
    open on the exchange with no more protection).
C2: there was no paper/live check at all on the close/replace path —
    only "is some broker object configured" — unlike the entry side
    (ExecutionNode), which already gates real orders on
    `_is_mandate_enabled()`. In paper mode this meant closes could
    place REAL orders.

This file covers both PositionMonitor._execute_close and
RiskManagerAgent._try_replace_position, since they share the exact
same class of bug and the exact same fix (src/execution/broker_routing.py).

Run: python -m unittest tests.test_sprint_46n_close_routing -v
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

from src.agents.risk_agent import RiskManagerAgent
from src.data_store.position_monitor import PositionMonitor
from src.data_store.positions import Position, PositionRepository
from src.safety.audit_ledger import AuditLedger

_BROKERS_CONFIG = {
    "crypto": {"symbols": ["BTC-USD", "ETH-USD"]},
    "equity": {"symbols": ["SPY", "QQQ", "GLD", "USO"]},
}


class _FakeCryptoBroker:
    def __init__(self):
        self.orders = []

    def create_market_order(self, symbol, side, qty):
        self.orders.append({"symbol": symbol, "side": side, "qty": qty})
        return {"id": "fake_crypto", "symbol": symbol, "side": side, "qty": qty, "status": "filled"}


class _FakeAlpacaBroker:
    def __init__(self):
        self.orders = []

    def create_market_order(self, symbol, side, amount=None, notional_usd=None):
        self.orders.append({"symbol": symbol, "side": side, "amount": amount, "notional_usd": notional_usd})
        return {"id": "fake_alpaca", "symbol": symbol, "side": side, "status": "filled"}


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


def _make_position(asset, direction="long", qty=1.0):
    return Position(
        asset=asset,
        direction=direction,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        qty=qty,
        risk_usd=5.0,
        entry_ts=time.time(),
        strategy="TestStrategy",
    )


class PositionMonitorCloseRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.crypto_broker = _FakeCryptoBroker()
        self.alpaca_broker = _FakeAlpacaBroker()

    def _make_monitor(self, live=True):
        override_path = _write_mode_override(self.tmpdir, mandate_enabled=live)
        return PositionMonitor(
            repo=self.repo,
            audit=self.audit,
            broker=self.crypto_broker,
            alpaca_broker=self.alpaca_broker,
            brokers_config=_BROKERS_CONFIG,
            mode_override_path=override_path,
        )

    def test_equity_close_routes_to_alpaca_not_crypto(self):
        """C1: an SPY close must hit alpaca_broker, never crypto_broker."""
        monitor = self._make_monitor(live=True)
        pos = _make_position("SPY", direction="long", qty=2.0)
        self.repo.add_open(pos)

        closed = monitor._execute_close(pos, price=112.0, reason="TP_HIT")

        self.assertIsNotNone(closed)
        self.assertEqual(len(self.alpaca_broker.orders), 1)
        self.assertEqual(self.alpaca_broker.orders[0]["symbol"], "SPY")
        self.assertEqual(self.alpaca_broker.orders[0]["amount"], 2.0)
        self.assertEqual(len(self.crypto_broker.orders), 0)

    def test_crypto_close_still_routes_to_crypto_broker(self):
        """Regression: crypto behavior must be unchanged."""
        monitor = self._make_monitor(live=True)
        pos = _make_position("BTC-USD", direction="long", qty=0.001)
        self.repo.add_open(pos)

        closed = monitor._execute_close(pos, price=112.0, reason="TP_HIT")

        self.assertIsNotNone(closed)
        self.assertEqual(len(self.crypto_broker.orders), 1)
        self.assertEqual(self.crypto_broker.orders[0]["symbol"], "BTC/USD")
        self.assertEqual(len(self.alpaca_broker.orders), 0)

    def test_paper_mode_never_calls_any_broker(self):
        """C2: paper mode must simulate the close locally, no real order
        to either broker, for crypto OR equity."""
        monitor = self._make_monitor(live=False)
        btc = _make_position("BTC-USD", direction="long", qty=0.001)
        spy = _make_position("SPY", direction="long", qty=1.0)
        self.repo.add_open(btc)
        self.repo.add_open(spy)

        closed_btc = monitor._execute_close(btc, price=112.0, reason="TP_HIT")
        closed_spy = monitor._execute_close(spy, price=112.0, reason="TP_HIT")

        self.assertIsNotNone(closed_btc)
        self.assertIsNotNone(closed_spy)
        self.assertEqual(len(self.crypto_broker.orders), 0)
        self.assertEqual(len(self.alpaca_broker.orders), 0)

    def test_unknown_asset_class_falls_back_to_crypto_broker(self):
        """An asset not present in brokers_config resolves to
        `asset_class="unknown"`, which falls back to the crypto broker
        — the SAME broker every close/replace call used unconditionally
        before Sprint 46N. This is deliberate backward compatibility:
        `brokers_config` only ever RECLASSIFIES an asset as equity; an
        unmapped asset must not silently stop being closeable."""
        monitor = self._make_monitor(live=True)
        pos = _make_position("DOGE-USD", direction="long", qty=100.0)
        self.repo.add_open(pos)

        closed = monitor._execute_close(pos, price=112.0, reason="TP_HIT")

        self.assertIsNotNone(closed)
        self.assertEqual(len(self.crypto_broker.orders), 1)
        self.assertEqual(self.crypto_broker.orders[0]["symbol"], "DOGE-USD")
        self.assertEqual(len(self.alpaca_broker.orders), 0)


class RiskAgentReplacementRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.crypto_broker = _FakeCryptoBroker()
        self.alpaca_broker = _FakeAlpacaBroker()

    def _make_agent(self, live=True):
        override_path = _write_mode_override(self.tmpdir, mandate_enabled=live)
        return RiskManagerAgent(
            broker_client=self.crypto_broker,
            alpaca_broker=self.alpaca_broker,
            brokers_config=_BROKERS_CONFIG,
            mode_override_path=override_path,
            position_repo=self.repo,
            audit=self.audit,
            current_prices={"SPY": 500.0, "BTC-USD": 64000.0},
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            portfolio_stress_check=False,
            asset_concentration_check=False,
        )

    def test_replacement_close_on_equity_routes_to_alpaca(self):
        agent = self._make_agent(live=True)
        worst = _make_position("SPY", direction="long", qty=1.0)
        worst.entry_price = 500.0
        self.repo.add_open(worst)

        # Force a clearly-worse score for the open position and a
        # clearly-better one for the new hypothesis so replacement fires.
        new_hyp = {"asset": "QQQ", "direction": "long", "strategy": "Test"}
        new_trade = {"asset": "QQQ", "direction": "long"}
        new_score_inputs = {
            "expected_move_pct": 5.0, "confidence": 0.9, "atr_pct": 0.01,
        }
        # Make the open position score terribly (deep loss) so any
        # reasonable new_score clears replacement_score_threshold.
        agent.current_prices["SPY"] = 400.0  # -20% vs entry_price 500

        replaced = agent._try_replace_position(new_hyp, new_trade, new_score_inputs)

        self.assertTrue(replaced, "expected the SPY position to be replaced")
        self.assertEqual(len(self.alpaca_broker.orders), 1)
        self.assertEqual(self.alpaca_broker.orders[0]["symbol"], "SPY")
        self.assertEqual(len(self.crypto_broker.orders), 0)

    def test_paper_mode_replacement_never_calls_broker(self):
        agent = self._make_agent(live=False)
        worst = _make_position("SPY", direction="long", qty=1.0)
        worst.entry_price = 500.0
        self.repo.add_open(worst)
        agent.current_prices["SPY"] = 400.0

        new_hyp = {"asset": "QQQ", "direction": "long", "strategy": "Test"}
        new_trade = {"asset": "QQQ", "direction": "long"}
        new_score_inputs = {
            "expected_move_pct": 5.0, "confidence": 0.9, "atr_pct": 0.01,
        }

        replaced = agent._try_replace_position(new_hyp, new_trade, new_score_inputs)

        self.assertTrue(replaced)
        self.assertEqual(len(self.alpaca_broker.orders), 0)
        self.assertEqual(len(self.crypto_broker.orders), 0)


if __name__ == "__main__":
    unittest.main()
