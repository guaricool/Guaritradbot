"""
Sprint 46R (audit M11.2): regression tests for the Telegram
retry-with-meta-alert behavior in NotificationAgent.

Audit M11.2: "send_telegram_message hace 1 intento, sin retry, sin
cola, sin meta-alerta. Si Telegram cae, toda la alertería
desaparece en silencio."

Fix: 3 attempts with exponential backoff (1s/2s/4s). On final
failure, emit a SYSTEM_ERROR audit event, append to
audit/telegram_failures.jsonl, and write a TELEGRAM_DELIVERY_FAILED
audit event so the existing audit-log readers see the outage.

These tests cover:
  1. Happy path: 1 attempt, returns True
  2. Transient failure: 2 attempts, then success → True
  3. Persistent failure: 3 attempts, then meta-alert fired
  4. No Telegram config: skip cleanly (returns False, no retries)
  5. Meta-alert side effects: SYSTEM_ERROR event + JSONL append +
     audit.append
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.agents.notification_agent import NotificationAgent


def _make_response(status_code: int, text: str = "") -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    r.text = text
    return r


def _make_agent(audit=None, event_bus=None, tmp_dir=None) -> NotificationAgent:
    cfg = {"notifications": {"enabled": True, "live_only": False}}
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-1234567890"
    os.environ["TELEGRAM_CHAT_ID"] = "999999999"
    # Use a tmp dir for the failures JSONL
    if tmp_dir:
        os.environ["AUDIT_TELEGRAM_FAILURES_OVERRIDE"] = ""
    agent = NotificationAgent(
        event_bus=event_bus,
        config=cfg,
        audit=audit,
    )
    # Redirect the failures log to the tmp dir
    if tmp_dir:
        agent._TELEGRAM_FAILURES_LOG = os.path.join(tmp_dir, "telegram_failures.jsonl")
    return agent


class TelegramRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_happy_path_one_attempt(self):
        agent = _make_agent(tmp_dir=self.tmp)
        with patch("src.agents.notification_agent.requests.post",
                   return_value=_make_response(200)) as mock_post:
            ok = agent.send_telegram_message("hello world")
        self.assertTrue(ok)
        self.assertEqual(mock_post.call_count, 1,
                         "Happy path should make exactly 1 request")

    def test_transient_failure_recovers_on_retry(self):
        agent = _make_agent(tmp_dir=self.tmp)
        # First call: 500, second call: 200
        with patch("src.agents.notification_agent.requests.post",
                   side_effect=[_make_response(500, "internal error"),
                                _make_response(200)]) as mock_post, \
             patch("src.agents.notification_agent.time.sleep") as mock_sleep:
            ok = agent.send_telegram_message("hello world")
        self.assertTrue(ok)
        self.assertEqual(mock_post.call_count, 2)
        # First sleep should be 1.0s (the first backoff)
        self.assertEqual(mock_sleep.call_count, 1)
        self.assertEqual(mock_sleep.call_args_list[0].args[0], 1.0)

    def test_persistent_failure_fires_meta_alert(self):
        audit = MagicMock()
        event_bus = MagicMock()
        agent = _make_agent(audit=audit, event_bus=event_bus, tmp_dir=self.tmp)

        # All 3 attempts fail
        with patch("src.agents.notification_agent.requests.post",
                   return_value=_make_response(503, "service unavailable")) as mock_post, \
             patch("src.agents.notification_agent.time.sleep") as mock_sleep:
            ok = agent.send_telegram_message("this will fail")
        self.assertFalse(ok)
        self.assertEqual(mock_post.call_count, 4,
                         "Should make 1 initial + 3 retries = 4 attempts")

        # Meta-alert side effects
        # 1. SYSTEM_ERROR event
        event_bus.publish.assert_called_once()
        evt_name, evt_payload = event_bus.publish.call_args.args
        self.assertEqual(evt_name, "SYSTEM_ERROR")
        self.assertEqual(evt_payload["kind"], "TELEGRAM_DELIVERY_FAILED")
        self.assertIn("HTTP 503", evt_payload["error"])

        # 2. audit.append with TELEGRAM_DELIVERY_FAILED
        audit.append.assert_called_once()
        evt_type, payload = audit.append.call_args.args
        self.assertEqual(evt_type, "TELEGRAM_DELIVERY_FAILED")
        self.assertEqual(payload["last_error"], "HTTP 503: service unavailable")

        # 3. JSONL side-channel file
        log_path = os.path.join(self.tmp, "telegram_failures.jsonl")
        self.assertTrue(os.path.exists(log_path),
                        f"telegram_failures.jsonl should exist at {log_path}")
        with open(log_path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["last_error"],
                         "HTTP 503: service unavailable")
        self.assertIn("text", lines[0])
        self.assertIn("ts", lines[0])

    def test_no_telegram_config_skips_cleanly(self):
        cfg = {"notifications": {"enabled": True, "live_only": False}}
        # Delete the env vars
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        # Reload agent with no token
        agent = NotificationAgent(
            event_bus=None,
            config=cfg,
            audit=None,
        )
        # Should return False and not raise, even with no token
        with patch("src.agents.notification_agent.requests.post") as mock_post:
            ok = agent.send_telegram_message("hi")
        self.assertFalse(ok)
        self.assertEqual(mock_post.call_count, 0,
                         "No Telegram config = no requests at all")

    def test_network_exception_treated_as_failure(self):
        audit = MagicMock()
        agent = _make_agent(audit=audit, tmp_dir=self.tmp)
        with patch("src.agents.notification_agent.requests.post",
                   side_effect=requests.exceptions.Timeout("read timeout")) as mock_post, \
             patch("src.agents.notification_agent.time.sleep"):
            ok = agent.send_telegram_message("hello")
        self.assertFalse(ok)
        self.assertEqual(mock_post.call_count, 4)
        audit.append.assert_called_once()
        evt_type, payload = audit.append.call_args.args
        self.assertEqual(evt_type, "TELEGRAM_DELIVERY_FAILED")
        self.assertIn("Timeout", payload["last_error"])


if __name__ == "__main__":
    unittest.main()
