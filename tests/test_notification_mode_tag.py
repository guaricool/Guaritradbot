"""
Bug fix: NotificationAgent's Telegram messages for TRADE_OPENED,
TRADE_CLOSED, and POSITION_UPDATE all had the literal string "LIVE"
hardcoded in the message text, regardless of the bot's actual mode.

Harmless while `notifications.live_only=True` (the default) -- those
notifications only fire in live mode anyway, so "LIVE" happened to
always be correct. But with `live_only=False` (paper notifications
enabled, a real config Carlos was running), every message still said
"LIVE" while the bot was in PAPER mode -- exactly backwards for a
message whose entire point is telling the operator whether real money
moved.

Run: python -m unittest tests.test_notification_mode_tag -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.notification_agent import NotificationAgent
from src.core.event_bus import EventBus


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


def _make_agent(tmpdir, live: bool, live_only: bool = False):
    bus = EventBus()
    config = {"notifications": {"enabled": True, "live_only": live_only}}
    mode_path = _write_mode_override(tmpdir, live)
    return NotificationAgent(bus, config, mode_override_path=mode_path), bus


class TradeOpenedModeTagTest(unittest.TestCase):
    def _order(self):
        return {
            "position_id": "pos_1", "asset": "BTC-USD", "direction": "long",
            "entry_price": 50000, "qty": 0.002, "stop_loss": 49000,
            "take_profit": 52000, "risk_usd": 2.0, "strategy": "RSI",
        }

    def test_says_paper_when_in_paper_mode(self):
        tmpdir = tempfile.mkdtemp()
        agent, bus = _make_agent(tmpdir, live=False)
        with patch.object(agent, "send_telegram_message") as send:
            bus.publish("TRADE_OPENED", self._order())
            msg = send.call_args[0][0]
            self.assertIn("PAPER", msg)
            self.assertNotIn("LIVE", msg)

    def test_says_live_when_in_live_mode(self):
        tmpdir = tempfile.mkdtemp()
        agent, bus = _make_agent(tmpdir, live=True)
        with patch.object(agent, "send_telegram_message") as send:
            bus.publish("TRADE_OPENED", self._order())
            msg = send.call_args[0][0]
            self.assertIn("LIVE", msg)
            self.assertNotIn("PAPER", msg)


class TradeClosedModeTagTest(unittest.TestCase):
    def _close(self):
        return {
            "asset": "BTC-USD", "pnl_usd": 5.0, "reason": "take_profit",
            "entry_price": 50000, "close_price": 51000, "direction": "long",
            "duration_s": 3600, "qty": 0.002,
        }

    def test_says_paper_when_in_paper_mode(self):
        tmpdir = tempfile.mkdtemp()
        agent, bus = _make_agent(tmpdir, live=False)
        with patch.object(agent, "send_telegram_message") as send:
            bus.publish("TRADE_CLOSED", self._close())
            msg = send.call_args[0][0]
            self.assertIn("PAPER", msg)
            self.assertNotIn("LIVE", msg)

    def test_says_live_when_in_live_mode(self):
        tmpdir = tempfile.mkdtemp()
        agent, bus = _make_agent(tmpdir, live=True)
        with patch.object(agent, "send_telegram_message") as send:
            bus.publish("TRADE_CLOSED", self._close())
            msg = send.call_args[0][0]
            self.assertIn("LIVE", msg)
            self.assertNotIn("PAPER", msg)


class PositionUpdateModeTagTest(unittest.TestCase):
    def _update(self):
        return {
            "asset": "BTC-USD", "direction": "long", "entry_price": 50000,
            "current_price": 51000, "unrealized_pnl_usd": 5.0,
            "unrealized_pnl_pct": 2.0, "duration_hours": 1.0,
            "stop_loss": 49000, "take_profit": 52000,
        }

    def test_says_paper_when_in_paper_mode(self):
        tmpdir = tempfile.mkdtemp()
        agent, bus = _make_agent(tmpdir, live=False)
        with patch.object(agent, "send_telegram_message") as send:
            bus.publish("POSITION_UPDATE", self._update())
            msg = send.call_args[0][0]
            self.assertIn("PAPER", msg)
            self.assertNotIn("LIVE", msg)

    def test_says_live_when_in_live_mode(self):
        tmpdir = tempfile.mkdtemp()
        agent, bus = _make_agent(tmpdir, live=True)
        with patch.object(agent, "send_telegram_message") as send:
            bus.publish("POSITION_UPDATE", self._update())
            msg = send.call_args[0][0]
            self.assertIn("LIVE", msg)
            self.assertNotIn("PAPER", msg)


if __name__ == "__main__":
    unittest.main()
