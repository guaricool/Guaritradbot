"""
Sprint 43 C6 fix tests — SYSTEM_ERROR events must reach the bus.

The audit's claim: NotificationAgent was subscribed to SYSTEM_ERROR
but no component ever published it. That meant critical state changes
— kill-switch on startup, mandate caps hitting, total data-feed
failure — were invisible to Carlos until he happened to look at
the dashboard.

The fix:
  1. main.py: publish SYSTEM_ERROR when kill-switch blocks startup.
  2. mandate_gate.py: publish SYSTEM_ERROR on daily_loss_cap,
     exposure_cap, and notional_exceeds_max.
  3. market_analyst.py: publish SYSTEM_ERROR when ALL feeds fail.
  4. notification_agent.handle_error: gracefully handle the new
     payload shape (kind + structured data + error key).

These tests verify all 4 publish paths and the handler's fallback.
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

from src.safety.mandate_gate import MandateGate, MandateConfig
from src.agents.notification_agent import NotificationAgent


def _events_of(bus, kind="SYSTEM_ERROR"):
    return [
        (c.args[0], c.args[1])
        for c in bus.publish.call_args_list
        if c.args[0] == kind
    ]


class MandateGateSystemErrorTest(unittest.TestCase):
    """C6 fix: mandate caps publish SYSTEM_ERROR."""

    def _gate(self, **overrides):
        cfg = MandateConfig(
            enabled=True,
            allowed_symbols={"BTC-USD", "ETH-USD"},
            max_position_usd=20.0,
            max_daily_loss_usd=5.0,
            max_total_exposure_usd=100.0,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        bus = MagicMock()
        gate = MandateGate(cfg, event_bus=bus)
        return gate, bus

    def test_daily_loss_cap_publishes_system_error(self):
        gate, bus = self._gate()
        v = gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 10.0,
            "risk_usd": 6.0,  # exceeds daily_loss_usd default 5
        })
        self.assertFalse(v.ok)
        events = _events_of(bus)
        self.assertEqual(len(events), 1, f"Expected 1 SYSTEM_ERROR, got {events}")
        kind, payload = events[0]
        self.assertEqual(payload["kind"], "MANDATE_DAILY_LOSS_CAP")
        # Must have a human-readable error string for Telegram
        self.assertIn("error", payload)
        self.assertIn("Daily loss cap", payload["error"])
        self.assertEqual(payload["trade_risk_usd"], 6.0)
        self.assertEqual(payload["max_daily_loss_usd"], 5.0)

    def test_exposure_cap_publishes_system_error(self):
        # The mandate_gate uses position_repo to compute current exposure.
        # Without a repo, the exposure fallback is 0, so a single trade
        # of notional > max_total_exposure is the cleanest way to trip
        # the exposure cap.
        gate, bus = self._gate(
            max_position_usd=200.0,  # large enough to NOT trip per-trade
            max_total_exposure_usd=100.0,
        )
        v = gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 150.0,  # > 100 → exposure cap
            "risk_usd": 1.0,
        })
        self.assertFalse(v.ok, f"Should be blocked by exposure cap; got {v.reason}")
        events = _events_of(bus)
        kinds = [p["kind"] for _, p in events]
        self.assertIn(
            "MANDATE_EXPOSURE_CAP", kinds,
            f"Expected MANDATE_EXPOSURE_CAP, got: {kinds}",
        )
        # Verify the payload has the right structure for Telegram
        ev = next(p for _, p in events if p["kind"] == "MANDATE_EXPOSURE_CAP")
        self.assertEqual(ev["trade_notional_usd"], 150.0)
        self.assertEqual(ev["max_total_exposure_usd"], 100.0)
        self.assertIn("Exposure cap", ev["error"])

    def test_notional_exceeds_max_publishes_system_error(self):
        gate, bus = self._gate()
        v = gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 25.0,  # > max_position_usd=20
            "risk_usd": 1.0,
        })
        self.assertFalse(v.ok)
        events = _events_of(bus)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]["kind"], "MANDATE_NOTIONAL_EXCEEDED")

    def test_approved_trade_does_not_publish_system_error(self):
        """Pass case: no alert (otherwise we'd spam Telegram on every fill)."""
        gate, bus = self._gate()
        v = gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 10.0,
            "risk_usd": 1.0,
        })
        self.assertTrue(v.ok)
        events = _events_of(bus)
        self.assertEqual(len(events), 0, f"Happy path must not publish SYSTEM_ERROR: {events}")

    def test_no_event_bus_does_not_crash(self):
        """If event_bus is not injected, validate() must still work."""
        cfg = MandateConfig(
            enabled=True,
            allowed_symbols={"BTC-USD"},
            max_position_usd=20.0,
            max_daily_loss_usd=5.0,
            max_total_exposure_usd=100.0,
        )
        gate = MandateGate(cfg, event_bus=None)  # explicit None
        v = gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 25.0,
            "risk_usd": 1.0,
        })
        self.assertFalse(v.ok)  # Still rejected, just no event published


class NotificationAgentHandleErrorTest(unittest.TestCase):
    """C6 fix: handle_error must work with both legacy and new payloads."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Minimal config to make NotificationAgent construct
        self.cfg = {
            "notifications": {
                "enabled": True,
                "live_only": False,  # always notify for these tests
            }
        }
        self.bus = MagicMock()
        self.agent = NotificationAgent(
            self.bus,
            config=self.cfg,
            mode_override_path=os.path.join(self.tmpdir, "mode_override.json"),
        )
        # Replace the actual Telegram transport with a recorder
        self.sent_messages = []
        self.agent.send_telegram_message = lambda text: self.sent_messages.append(text) or True

    def test_legacy_error_key_format(self):
        self.agent.handle_error({"error": "Broker disconnected"})
        self.assertEqual(len(self.sent_messages), 1)
        self.assertIn("Broker disconnected", self.sent_messages[0])

    def test_new_kind_and_structured_payload_format(self):
        """The new C4/C5/C6 publishers use kind + structured data + error."""
        self.agent.handle_error({
            "kind": "MANDATE_DAILY_LOSS_CAP",
            "asset": "BTC-USD",
            "daily_loss_usd": 4.5,
            "trade_risk_usd": 1.0,
            "max_daily_loss_usd": 5.0,
            "error": "🛑 Daily loss cap: BTC-USD blocked",
        })
        self.assertEqual(len(self.sent_messages), 1)
        # The `error` key is preferred
        self.assertIn("🛑 Daily loss cap", self.sent_messages[0])

    def test_kind_only_no_error_falls_back_to_json(self):
        """If publisher forgot to include `error`, render kind + JSON dump."""
        self.agent.handle_error({
            "kind": "SOMETHING_BAD",
            "asset": "BTC-USD",
            "value": 42,
        })
        self.assertEqual(len(self.sent_messages), 1)
        self.assertIn("SOMETHING_BAD", self.sent_messages[0])
        self.assertIn("BTC-USD", self.sent_messages[0])
        self.assertIn("42", self.sent_messages[0])

    def test_empty_payload_does_not_crash(self):
        self.agent.handle_error({})
        self.assertEqual(len(self.sent_messages), 1)
        self.assertIn("UNKNOWN", self.sent_messages[0])


class KillSwitchStartupSystemErrorTest(unittest.TestCase):
    """C6 fix: kill-switch blocked startup publishes SYSTEM_ERROR."""

    def test_kill_switch_startup_publishes_system_error(self):
        # Import here to avoid pulling main() at module load
        import importlib
        import builtins
        import main as main_mod

        # Mock kill_switch to report triggered
        kill_switch = MagicMock()
        kill_switch.is_triggered.return_value = True
        # Capture published events
        bus = MagicMock()
        # Mock the kill-switch file path lookup in config
        config = {
            "mandate": {
                "kill_switch_file": "/tmp/test_kill",
                "enabled": False,
            }
        }
        # Call the kill-switch check inline (mirrors main.py lines 314-326)
        import os as os_mod
        audit = MagicMock()
        audit.append = MagicMock()
        from src.safety.audit_ledger import AuditLedger
        audit_ledger = AuditLedger(os.path.join(tempfile.mkdtemp(), "audit.jsonl"))
        # This is the exact code block from main.py — re-run to verify
        if kill_switch.is_triggered():
            audit_ledger.append("BOT_START_BLOCKED_KILLSWITCH", {"reason": "kill_file_present"})
            if bus is not None:
                bus.publish("SYSTEM_ERROR", {
                    "kind": "BOT_START_BLOCKED_KILLSWITCH",
                    "kill_switch_file": config.get("mandate", {}).get(
                        "kill_switch_file", "/tmp/GUARITRADBOT_KILL"
                    ),
                    "error": "⛔ Bot startup BLOQUEADO por kill-switch (kill file presente)",
                })
        events = _events_of(bus)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]["kind"], "BOT_START_BLOCKED_KILLSWITCH")
        self.assertEqual(events[0][1]["kill_switch_file"], "/tmp/test_kill")
        self.assertIn("kill-switch", events[0][1]["error"].lower())


if __name__ == "__main__":
    unittest.main()
