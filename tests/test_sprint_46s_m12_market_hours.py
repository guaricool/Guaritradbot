"""
Sprint 46S (audit M12) — market-hours gate for equity ENTRIES.

The audit's exact complaint: "Contenedores en UTC sin TZ; nada limita
la generación de señales de acciones por sesión de mercado. Las
órdenes de Alpaca son TimeInForce.DAY -- enviadas fuera de horario
quedan encoladas al open y se llenan a un precio potencialmente lejano
de la señal. Los timestamps de audit.jsonl son naive, sin offset.
Gatear entradas de acciones con GET /v2/clock de Alpaca."

This covers the two testable code changes:
  1. AlpacaBroker.is_market_open() — wraps Alpaca's real clock endpoint,
     fail-open on any error.
  2. ExecutionNode._execute_equity_order — skips NEW entries when the
     market is closed, WITHOUT ever touching the close-order path
     (position_monitor.py's _close_position, which intentionally stays
     ungated — see execution_node.py's comment for why).
  3. AuditLedger.append()'s "iso" field is now offset-aware (the third
     M12 sub-fix; TZ-in-compose is infra-only and not unit-testable).

Run: python -m unittest tests.test_sprint_46s_m12_market_hours -v
"""
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.execution.execution_node import ExecutionNode
from src.execution.alpaca_broker import AlpacaBroker
from src.safety.audit_ledger import AuditLedger
from src.core.event_bus import EventBus


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


BROKERS_CONFIG = {
    "crypto": {"name": "binanceus", "symbols": ["BTC-USD", "ETH-USD", "SOL-USD"]},
    "equity": {"name": "alpaca", "symbols": ["SPY", "QQQ", "GLD", "USO"]},
}


class AlpacaBrokerIsMarketOpenTest(unittest.TestCase):
    """Direct unit tests of AlpacaBroker.is_market_open (mocked TradingClient)."""

    def _broker_with_mock_trading_client(self, mock_trading_client):
        with patch("alpaca.trading.client.TradingClient", return_value=mock_trading_client, create=True):
            return AlpacaBroker(api_key="FAKE_KEY", secret_key="FAKE_SECRET", paper=True)

    def test_market_open_true(self):
        mock_tc = MagicMock()
        clock = MagicMock()
        clock.is_open = True
        mock_tc.get_clock.return_value = clock
        broker = self._broker_with_mock_trading_client(mock_tc)
        self.assertTrue(broker.is_market_open())

    def test_market_closed_false(self):
        mock_tc = MagicMock()
        clock = MagicMock()
        clock.is_open = False
        mock_tc.get_clock.return_value = clock
        broker = self._broker_with_mock_trading_client(mock_tc)
        self.assertFalse(broker.is_market_open())

    def test_clock_error_fails_open(self):
        """Any exception from the clock call must return True (fail-open) —
        a broker/network blip must never silently block ALL equity entries
        forever, matching every other best-effort method on this class."""
        mock_tc = MagicMock()
        mock_tc.get_clock.side_effect = RuntimeError("network down")
        broker = self._broker_with_mock_trading_client(mock_tc)
        self.assertTrue(broker.is_market_open())


class ExecutionNodeMarketHoursGateTest(unittest.TestCase):
    """ExecutionNode._execute_equity_order must skip NEW entries when
    the market is closed, and must NOT gate anything for crypto."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mode_override_path = _write_mode_override(self.tmpdir, True)  # live mode
        self.bus = EventBus()
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))

        self.alpaca = MagicMock(spec=AlpacaBroker)
        self.alpaca.is_symbol_tradeable.return_value = True
        self.alpaca.create_market_order.return_value = {
            "id": "ALP_1", "status": "filled", "symbol": "SPY",
            "side": "buy", "qty": "0.02", "filled": "0.02",
            "filled_avg_price": "500.0",
        }

        self.crypto = MagicMock()
        exchange = MagicMock()
        exchange.symbols = ["BTC/USD", "ETH/USD"]
        self.crypto.exchange = exchange
        self.crypto.create_market_order.return_value = {"id": "BIN_1", "status": "filled", "filled": "0.001"}

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

    def _equity_order(self):
        return {
            "asset": "SPY", "direction": "long",
            "position_size": 0.02, "entry_price": 500.0,
            "stop_loss": 490.0, "take_profit": 520.0,
        }

    def test_entry_skipped_when_market_closed(self):
        self.alpaca.is_market_open.return_value = False
        self.node.execute_order(self._equity_order())
        self.alpaca.create_market_order.assert_not_called()
        self.alpaca.is_symbol_tradeable.assert_not_called()  # gate fires BEFORE the tradeable pre-flight
        skipped = [e for e in self.audit_events if e[0] == "TRADE_SKIPPED_MARKET_CLOSED"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][1]["asset"], "SPY")

    def test_entry_proceeds_when_market_open(self):
        self.alpaca.is_market_open.return_value = True
        self.node.execute_order(self._equity_order())
        self.alpaca.create_market_order.assert_called_once()
        skipped = [e for e in self.audit_events if e[0] == "TRADE_SKIPPED_MARKET_CLOSED"]
        self.assertEqual(len(skipped), 0)

    def test_crypto_entry_never_gated_by_market_hours(self):
        """Crypto trades 24/7 and its broker has no is_market_open at
        all — the crypto path must be entirely unaffected."""
        self.alpaca.is_market_open.return_value = False  # equity market closed, irrelevant here
        crypto_order = {
            "asset": "BTC-USD", "direction": "long",
            "position_size": 0.001, "entry_price": 50000.0,
        }
        self.node.execute_order(crypto_order)
        self.crypto.create_market_order.assert_called_once()
        skipped = [e for e in self.audit_events if e[0] == "TRADE_SKIPPED_MARKET_CLOSED"]
        self.assertEqual(len(skipped), 0)

    def test_gate_is_defensive_if_broker_lacks_is_market_open(self):
        """A broker double without is_market_open (e.g. an older mock
        in some other test file) must not crash execute_order — the
        gate only applies via hasattr()."""
        bare_alpaca = MagicMock(spec=["is_symbol_tradeable", "create_market_order"])
        bare_alpaca.is_symbol_tradeable.return_value = True
        bare_alpaca.create_market_order.return_value = {
            "id": "ALP_2", "status": "filled", "filled": "0.02", "filled_avg_price": "500.0",
        }
        node = ExecutionNode(
            self.bus,
            execution_mode="auto",
            broker_client=self.crypto,
            alpaca_broker=bare_alpaca,
            brokers_config=BROKERS_CONFIG,
            kill_switch=None,
            audit=self.audit,
            mode_override_path=self.mode_override_path,
        )
        node.execute_order(self._equity_order())
        bare_alpaca.create_market_order.assert_called_once()


class AuditLedgerIsoOffsetTest(unittest.TestCase):
    """AuditLedger.append()'s 'iso' field must carry a UTC offset now,
    not a naive local-time string."""

    def test_iso_field_has_utc_offset(self):
        tmpdir = tempfile.mkdtemp()
        ledger = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        event = ledger.append("TEST", {})
        # Accept either a numeric offset (+HH:MM / -HH:MM) or literal
        # "+00:00" for a UTC-configured host — either way, some offset
        # marker must be present, unlike the old naive
        # "%Y-%m-%dT%H:%M:%S" format which had neither.
        self.assertRegex(
            event["iso"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
            f"iso field {event['iso']!r} is missing a UTC offset",
        )

    def test_iso_field_round_trips_via_fromisoformat(self):
        import datetime as dt
        tmpdir = tempfile.mkdtemp()
        ledger = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        event = ledger.append("TEST", {})
        parsed = dt.datetime.fromisoformat(event["iso"])
        self.assertIsNotNone(parsed.tzinfo)


if __name__ == "__main__":
    unittest.main()
