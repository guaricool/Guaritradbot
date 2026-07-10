"""
Sprint 34 tests — NotificationAgent (refactored).

Verifies:
  - Live gate: when live_only=True and not in live, no message sent
  - Live gate: when live_only=True and in live, message sent
  - live_only=False bypasses the gate
  - handle_trade_opened formats entry details (SL/TP/risk)
  - handle_trade_closed formats P&L with sign + duration
  - handle_position_update formats P&L progress with bar
  - min_pnl_usd threshold skips dust updates
  - Telegram send is mocked — no real HTTP

Run: python -m unittest tests.test_notification_agent_sprint34 -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.notification_agent import NotificationAgent, _is_live_mode
from src.core.event_bus import EventBus


def _write_mode_override(tmpdir, mandate_enabled: bool):
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


class IsLiveModeTest(unittest.TestCase):
    def test_no_file_returns_false(self):
        self.assertFalse(_is_live_mode("/nonexistent/path.json"))

    def test_empty_file_returns_false(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{}")
            path = f.name
        try:
            self.assertFalse(_is_live_mode(path))
        finally:
            os.unlink(path)

    def test_malformed_json_returns_false(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("this is not json")
            path = f.name
        try:
            self.assertFalse(_is_live_mode(path))
        finally:
            os.unlink(path)

    def test_mandate_enabled_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_mode_override(tmp, True)
            self.assertTrue(_is_live_mode(path))

    def test_mandate_enabled_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_mode_override(tmp, False)
            self.assertFalse(_is_live_mode(path))


class NotificationAgentGateTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mode_override_path = _write_mode_override(self.tmpdir, True)  # live
        self.bus = EventBus()
        self.config = {
            "notifications": {
                "enabled": True,
                "live_only": True,
                "position_update_min_pnl_usd": 0.0,
            }
        }

    def _make_agent(self, **overrides):
        cfg = {**self.config}
        cfg["notifications"] = {**self.config["notifications"], **overrides}
        with patch("requests.post"):  # block all HTTP
            return NotificationAgent(
                self.bus, cfg,
                mode_override_path=self.mode_override_path,
            )

    def test_live_only_true_and_live_mode_sends(self):
        agent = self._make_agent()
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("TRADE_OPENED", {
                "position_id": "pos_test", "asset": "BTC-USD",
                "direction": "long", "entry_price": 50000, "qty": 0.001,
                "stop_loss": 49000, "take_profit": 52000, "risk_usd": 5.0,
                "strategy": "test", "notional_usd": 50.0,
            })
            send.assert_called_once()
            msg = send.call_args[0][0]
            self.assertIn("NUEVA ENTRADA", msg)
            self.assertIn("BTC-USD", msg)
            self.assertIn("$50,000.00", msg)
            self.assertIn("LONG", msg)

    def test_live_only_true_and_paper_mode_skips(self):
        # Write a paper-mode override
        path = _write_mode_override(self.tmpdir, False)
        agent = NotificationAgent(
            self.bus, self.config, mode_override_path=path
        )
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("TRADE_OPENED", {
                "position_id": "pos_test", "asset": "BTC-USD",
                "direction": "long", "entry_price": 50000, "qty": 0.001,
                "stop_loss": 49000, "take_profit": 52000, "risk_usd": 5.0,
                "strategy": "test", "notional_usd": 50.0,
            })
            send.assert_not_called()

    def test_live_only_false_always_sends(self):
        path = _write_mode_override(self.tmpdir, False)  # paper
        cfg = {**self.config}
        cfg["notifications"] = {**self.config["notifications"], "live_only": False}
        agent = NotificationAgent(
            self.bus, cfg, mode_override_path=path
        )
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("TRADE_OPENED", {
                "position_id": "pos_test", "asset": "BTC-USD",
                "direction": "long", "entry_price": 50000, "qty": 0.001,
                "stop_loss": 49000, "take_profit": 52000, "risk_usd": 5.0,
                "strategy": "test", "notional_usd": 50.0,
            })
            send.assert_called_once()

    def test_notifications_disabled_sends_nothing(self):
        cfg = {**self.config}
        cfg["notifications"] = {**self.config["notifications"], "enabled": False}
        agent = NotificationAgent(
            self.bus, cfg, mode_override_path=self.mode_override_path
        )
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("TRADE_OPENED", {
                "asset": "BTC-USD", "direction": "long", "entry_price": 50000,
                "qty": 0.001, "stop_loss": 49000, "take_profit": 52000,
                "risk_usd": 5.0, "strategy": "test", "notional_usd": 50.0,
            })
            send.assert_not_called()


class TradeOpenedFormatTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mode_override_path = _write_mode_override(self.tmpdir, True)
        self.bus = EventBus()
        self.config = {"notifications": {"enabled": True, "live_only": True}}

    def test_long_entry_includes_sl_tp_pct(self):
        agent = NotificationAgent(
            self.bus, self.config, mode_override_path=self.mode_override_path
        )
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("TRADE_OPENED", {
                "position_id": "pos_abc_123", "asset": "BTC-USD",
                "direction": "long", "entry_price": 50000, "qty": 0.002,
                "stop_loss": 49000, "take_profit": 52000, "risk_usd": 2.0,
                "strategy": "RSI", "notional_usd": 100.0,
            })
            msg = send.call_args[0][0]
            # SL distance: (50000-49000)/50000 = 2.00%
            self.assertIn("-2.00%", msg)
            # TP distance: (52000-50000)/50000 = 4.00%
            self.assertIn("+4.00%", msg)
            self.assertIn("RSI", msg)
            self.assertIn("pos_abc_123", msg)

    def test_short_entry_includes_sl_tp_pct(self):
        agent = NotificationAgent(
            self.bus, self.config, mode_override_path=self.mode_override_path
        )
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("TRADE_OPENED", {
                "position_id": "pos_short", "asset": "BTC-USD",
                "direction": "short", "entry_price": 50000, "qty": 0.002,
                "stop_loss": 51000, "take_profit": 48000, "risk_usd": 2.0,
                "strategy": "RSI", "notional_usd": 100.0,
            })
            msg = send.call_args[0][0]
            # SL distance for short: (51000-50000)/50000 = 2.00%
            self.assertIn("-2.00%", msg)
            # TP distance for short: (50000-48000)/50000 = 4.00%
            self.assertIn("+4.00%", msg)


class TradeClosedFormatTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mode_override_path = _write_mode_override(self.tmpdir, True)
        self.bus = EventBus()
        self.config = {"notifications": {"enabled": True, "live_only": True}}

    def test_profitable_close_uses_plus_sign_and_green_emoji(self):
        agent = NotificationAgent(
            self.bus, self.config, mode_override_path=self.mode_override_path
        )
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("TRADE_CLOSED", {
                "asset": "BTC-USD", "direction": "long",
                "entry_price": 50000, "close_price": 50500,
                "qty": 0.001, "pnl_usd": 0.5, "duration_s": 3600,
                "reason": "TP_HIT",
            })
            msg = send.call_args[0][0]
            self.assertIn("POSICIÓN CERRADA", msg)
            self.assertIn("+$0.50", msg)
            self.assertIn("✅", msg)  # green check for profit
            self.assertIn("TP_HIT", msg)
            self.assertIn("1.0h", msg)  # duration formatted

    def test_losing_close_uses_red_emoji(self):
        agent = NotificationAgent(
            self.bus, self.config, mode_override_path=self.mode_override_path
        )
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("TRADE_CLOSED", {
                "asset": "BTC-USD", "direction": "long",
                "entry_price": 50000, "close_price": 49500,
                "qty": 0.001, "pnl_usd": -0.5, "duration_s": 1800,
                "reason": "STOP_HIT",
            })
            msg = send.call_args[0][0]
            self.assertIn("-$0.50", msg)
            self.assertIn("❌", msg)
            self.assertIn("STOP_HIT", msg)
            self.assertIn("0.5h", msg)


class PositionUpdateFormatTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mode_override_path = _write_mode_override(self.tmpdir, True)
        self.bus = EventBus()
        self.config = {
            "notifications": {
                "enabled": True, "live_only": True,
                "position_update_min_pnl_usd": 0.0,
            }
        }

    def test_profitable_update_uses_up_emoji(self):
        agent = NotificationAgent(
            self.bus, self.config, mode_override_path=self.mode_override_path
        )
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("POSITION_UPDATE", {
                "asset": "BTC-USD", "direction": "long",
                "entry_price": 50000, "current_price": 50200,
                "qty": 0.001, "stop_loss": 49000, "take_profit": 52000,
                "unrealized_pnl_usd": 0.20, "unrealized_pnl_pct": 0.40,
                "duration_hours": 1.5,
            })
            msg = send.call_args[0][0]
            self.assertIn("UPDATE", msg)
            self.assertIn("+$0.20", msg)
            self.assertIn("+0.40%", msg)
            self.assertIn("📈", msg)  # up arrow for profit
            self.assertIn("1.5h", msg)

    def test_losing_update_uses_down_emoji(self):
        agent = NotificationAgent(
            self.bus, self.config, mode_override_path=self.mode_override_path
        )
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("POSITION_UPDATE", {
                "asset": "BTC-USD", "direction": "long",
                "entry_price": 50000, "current_price": 49900,
                "qty": 0.001, "stop_loss": 49000, "take_profit": 52000,
                "unrealized_pnl_usd": -0.10, "unrealized_pnl_pct": -0.20,
                "duration_hours": 0.5,
            })
            msg = send.call_args[0][0]
            self.assertIn("-$0.10", msg)
            self.assertIn("-0.20%", msg)
            self.assertIn("📉", msg)

    def test_dust_update_skipped_by_threshold(self):
        # Set min_pnl threshold to $0.05
        cfg = {**self.config}
        cfg["notifications"] = {**self.config["notifications"], "position_update_min_pnl_usd": 0.05}
        agent = NotificationAgent(
            self.bus, cfg, mode_override_path=self.mode_override_path
        )
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("POSITION_UPDATE", {
                "asset": "BTC-USD", "direction": "long",
                "entry_price": 50000, "current_price": 50001,
                "qty": 0.001, "stop_loss": 49000, "take_profit": 52000,
                "unrealized_pnl_usd": 0.00001, "unrealized_pnl_pct": 0.0,
                "duration_hours": 0.1,
            })
            send.assert_not_called()


class SystemErrorAlwaysNotifiesTest(unittest.TestCase):
    """The SYSTEM_ERROR handler bypasses the live gate (critical alerts)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mode_override_path = _write_mode_override(self.tmpdir, False)  # paper
        self.bus = EventBus()
        self.config = {"notifications": {"enabled": True, "live_only": True}}

    def test_error_notified_even_in_paper(self):
        agent = NotificationAgent(
            self.bus, self.config, mode_override_path=self.mode_override_path
        )
        with patch.object(agent, "send_telegram_message") as send:
            self.bus.publish("SYSTEM_ERROR", {"error": "Broker disconnected"})
            send.assert_called_once()
            self.assertIn("Broker disconnected", send.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
