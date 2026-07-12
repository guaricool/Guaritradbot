"""
Sprint 46R (audit M11.4): regression tests for the dead-man's
switch ping helper.

Audit M11.4: "Considerar un dead-man's switch (ping a
healthchecks.io por ciclo)."

These tests cover:
  1. Disabled (no URL): no request, no error, returns True
  2. Happy path (2xx): returns True, _last_ping_ok=True
  3. Non-2xx: returns False, _last_ping_ok=False, error recorded
  4. Timeout: returns False, _last_ping_ok=False
  5. Connection error: returns False
  6. State update is thread-safe (snapshot from get_ping_state)
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.observability.dead_mans_switch import (
    PING_TIMEOUT_S,
    get_ping_state,
    ping_dead_mans_switch,
)


def _make_response(status_code: int) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    return r


class DeadMansSwitchPingTest(unittest.TestCase):
    def setUp(self):
        # Clear any prior URL so we control the behavior per test
        os.environ.pop("HEALTHCHECKS_PING_URL", None)

    def test_disabled_no_url(self):
        """No URL = disabled, no requests, no error."""
        with patch("src.observability.dead_mans_switch.requests.get") as mock_get:
            ok, err = ping_dead_mans_switch()
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(mock_get.call_count, 0,
                         "Disabled state should not hit the network")

        state = get_ping_state()
        self.assertIsNone(state["url"])
        self.assertIn("disabled", (state["last_error"] or "").lower())

    def test_happy_path_2xx(self):
        with patch("src.observability.dead_mans_switch.requests.get",
                   return_value=_make_response(200)) as mock_get:
            ok, err = ping_dead_mans_switch("https://hc-ping.com/abc-123")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(mock_get.call_count, 1)

        state = get_ping_state()
        self.assertEqual(state["url"], "https://hc-ping.com/abc-123")
        self.assertTrue(state["last_ok"])
        self.assertIsNone(state["last_error"])
        self.assertGreater(state["last_at"], 0)

    def test_non_2xx_records_error(self):
        with patch("src.observability.dead_mans_switch.requests.get",
                   return_value=_make_response(500)):
            ok, err = ping_dead_mans_switch("https://hc-ping.com/abc")
        self.assertFalse(ok)
        self.assertIn("HTTP 500", err)

        state = get_ping_state()
        self.assertFalse(state["last_ok"])
        self.assertIn("HTTP 500", state["last_error"])

    def test_timeout_records_error(self):
        with patch("src.observability.dead_mans_switch.requests.get",
                   side_effect=requests.exceptions.Timeout("read timeout")):
            ok, err = ping_dead_mans_switch("https://hc-ping.com/abc")
        self.assertFalse(ok)
        self.assertIn("timeout", err.lower())

        state = get_ping_state()
        self.assertFalse(state["last_ok"])
        self.assertIn("timeout", state["last_error"].lower())

    def test_connection_error_records_error(self):
        with patch("src.observability.dead_mans_switch.requests.get",
                   side_effect=requests.exceptions.ConnectionError("dns failed")):
            ok, err = ping_dead_mans_switch("https://hc-ping.com/abc")
        self.assertFalse(ok)
        self.assertIn("ConnectionError", err)
        self.assertIn("dns failed", err)

    def test_timeout_passed_to_requests(self):
        """Verify the timeout kwarg is forwarded to requests.get."""
        with patch("src.observability.dead_mans_switch.requests.get",
                   return_value=_make_response(200)) as mock_get:
            ping_dead_mans_switch("https://hc-ping.com/abc", timeout_s=2.5)
        # The get() call's kwargs should include timeout=2.5
        call_kwargs = mock_get.call_args.kwargs
        self.assertEqual(call_kwargs.get("timeout"), 2.5)

    def test_state_thread_safe(self):
        """Concurrent pings + get_ping_state() should not raise.

        Smoke test for the lock: spawn N threads doing alternating
        ping + get_ping_state, no exception, state always readable.
        """
        errors = []
        def worker(i: int):
            try:
                for _ in range(20):
                    if i % 2 == 0:
                        ping_dead_mans_switch("https://hc-ping.com/x")
                    else:
                        get_ping_state()
            except Exception as e:
                errors.append(e)

        with patch("src.observability.dead_mans_switch.requests.get",
                   return_value=_make_response(200)):
            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(8)]
            for t in threads: t.start()
            for t in threads: t.join()
        self.assertEqual(errors, [],
                         f"Concurrent access raised: {errors}")


if __name__ == "__main__":
    unittest.main()
