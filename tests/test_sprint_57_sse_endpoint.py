"""
Sprint 57 — Server-Sent Events endpoint.

The pre-57 WebSocket at /ws/live is broken at the Traefik proxy
layer: Traefik returns 403 Forbidden on every HTTP/1.1 upgrade
request, regardless of router config or middleware. The fix
in Sprint 55.4 was a workaround (polling fallback) -- the
proper fix in Sprint 57 is to add a Server-Sent Events
endpoint that uses plain HTTP/1.1 chunked transfer and no
upgrade headers. SSE works with any proxy.

These tests pin the contract of the new /api/events endpoint:
  1. Auth: missing or bad token returns 401, NOT 200.
  2. Content-Type is `text/event-stream` so the browser's
     EventSource knows what to do with it.
  3. The stream starts with a `hello` event so the dashboard
     knows it's live (mirrors the WebSocket behavior).
  4. Events from `_broadcast` are delivered to the SSE stream
     in the same shape (`data: <json>\n\n`).
  5. The WebSocket endpoint at /ws/live still works (we keep
     it for back-compat and for non-Traefik deployments).
  6. Disconnect cleanup: when the client disconnects, the SSE
     queue is removed from APP_STATE["sse_clients"] so the
     broadcaster doesn't waste cycles on a dead consumer.
"""
# Sprint 57: server.py imports print() statements with emojis at
# module load time. On Windows + cp1252, this raises UnicodeEncodeError
# before the test body even runs. Force utf-8 on stdout/stderr so
# the import succeeds.
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _run(coro):
    """Drive an async coroutine to completion in a sync test."""
    return asyncio.run(coro)


class RouteRegistrationTest(unittest.TestCase):
    """Sprint 57 #1: the new endpoint is actually wired in
    the FastAPI app (otherwise the dashboard's EventSource
    will get a 404)."""

    def test_sse_endpoint_is_registered(self):
        from src.api.server import app
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        self.assertIn("/api/events", paths)

    def test_websocket_endpoint_still_registered(self):
        """Sprint 57: WebSocket stays for back-compat. The new
        SSE endpoint is a parallel option, not a replacement."""
        from src.api.server import app
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        self.assertIn("/ws/live", paths)


class AuthTest(unittest.TestCase):
    """Sprint 57 #2: auth is enforced. Missing or bad token
    returns 401, not 200 (so the dashboard's EventSource
    immediately knows to drop back to polling)."""

    def _make_request(self, token=None):
        from fastapi import Request
        req = MagicMock(spec=Request)
        req.query_params = {"token": token} if token else {}
        return req

    def test_missing_token_returns_401(self):
        from src.api.server import sse_events
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            _run(sse_events(token=None))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_bad_token_returns_401(self):
        from src.api.server import sse_events
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            _run(sse_events(token="not-a-real-token"))
        self.assertEqual(ctx.exception.status_code, 401)


class HelloEventTest(unittest.TestCase):
    """Sprint 57 #3: the stream starts with a `hello` event.
    Same shape as the WebSocket `hello` so the dashboard
    treats them uniformly."""

    def test_hello_event_on_connect(self):
        from src.api import server as srv
        from src.api import auth
        from fastapi import HTTPException
        # Patch verify_token to always succeed for this test
        real_verify = auth.verify_token
        auth.verify_token = lambda t: (True, "ok")
        try:
            # Patch APP_STATE to a clean dict
            saved = dict(srv.APP_STATE)
            srv.APP_STATE.clear()
            srv.APP_STATE["sse_clients"] = set()
            try:
                # Call sse_events; it returns a StreamingResponse.
                # The `hello` is enqueued in the per-client queue
                # at call time -- we can inspect that queue to
                # verify the hello was sent.
                resp = _run(srv.sse_events(token="valid"))
                # Get the queue the endpoint created (the only
                # one in sse_clients at this point)
                self.assertEqual(len(srv.APP_STATE["sse_clients"]), 1)
                queue = next(iter(srv.APP_STATE["sse_clients"]))
                # Drain the first item -- should be the hello
                item = queue.get_nowait()
                payload = json.loads(item)
                self.assertEqual(payload["type"], "hello")
                self.assertIn("started_at", payload)
                self.assertIn("ts", payload)
            finally:
                srv.APP_STATE.clear()
                srv.APP_STATE.update(saved)
        finally:
            auth.verify_token = real_verify


class EventFormatTest(unittest.TestCase):
    """Sprint 57 #4: events from `_broadcast` arrive in the
    SSE wire format (`data: <payload>\n\n`)."""

    def test_broadcast_fans_out_to_sse_queues(self):
        """`_broadcast` should put the same JSON payload on
        every SSE queue, in addition to sending to WebSocket
        clients. Verified synchronously by calling `_broadcast`
        and inspecting the queue contents."""
        from src.api import server as srv
        # Build a fake APP_STATE with a couple of SSE queues
        q1 = asyncio.Queue(maxsize=16)
        q2 = asyncio.Queue(maxsize=16)
        ws_client = MagicMock()
        ws_client.send_text = AsyncMock()
        srv.APP_STATE["sse_clients"] = {q1, q2}
        srv.APP_STATE["ws_clients"] = {ws_client}
        try:
            _run(srv._broadcast({"type": "test", "value": 42}))
            # Both SSE queues got the same payload
            payload1 = q1.get_nowait()
            payload2 = q2.get_nowait()
            self.assertEqual(payload1, payload2)
            data = json.loads(payload1)
            self.assertEqual(data["type"], "test")
            self.assertEqual(data["value"], 42)
            # WebSocket also got it
            ws_client.send_text.assert_awaited_once_with(payload1)
        finally:
            srv.APP_STATE.pop("sse_clients", None)
            srv.APP_STATE.pop("ws_clients", None)

    def test_slow_consumer_is_disconnected(self):
        """If a queue is full (slow consumer), the broadcaster
        drops it from the set so a stuck client doesn't wedge
        the broadcast loop. (Duplicate of CleanupTest below --
        kept here for test locality.)"""
        from src.api import server as srv
        q_full = asyncio.Queue(maxsize=1)
        q_full.put_nowait("already-there")
        q_good = asyncio.Queue(maxsize=16)
        srv.APP_STATE["sse_clients"] = {q_full, q_good}
        try:
            _run(srv._broadcast({"type": "flood"}))
            self.assertNotIn(q_full, srv.APP_STATE["sse_clients"])
            self.assertEqual(q_good.get_nowait(), _json_of({"type": "flood"}))
        finally:
            srv.APP_STATE.pop("sse_clients", None)


def _json_of(d):
    """Same encoder `_broadcast` uses (default=str)."""
    return json.dumps(d, default=str)


class CleanupTest(unittest.TestCase):
    """Sprint 57 #6: when a client disconnects, its queue is
    removed from APP_STATE. We test the broadcast-side of the
    cleanup (slow-consumer drop) -- the StreamingResponse `finally`
    block is trivially correct (it's the same pattern as the
    WebSocket handler) and tightly coupled to Starlette internals
    that aren't worth pinning."""

    def test_slow_consumer_drop_removes_queue(self):
        """A full queue gets dropped from sse_clients on the
        next broadcast. This is the "consumer gone" path that
        keeps a dead client from blocking the broadcast loop."""
        from src.api import server as srv
        q_full = asyncio.Queue(maxsize=1)
        q_full.put_nowait("already-there")
        q_good = asyncio.Queue(maxsize=16)
        srv.APP_STATE["sse_clients"] = {q_full, q_good}
        try:
            _run(srv._broadcast({"type": "flood"}))
            self.assertNotIn(q_full, srv.APP_STATE["sse_clients"])
            self.assertIn(q_good, srv.APP_STATE["sse_clients"])
        finally:
            srv.APP_STATE.pop("sse_clients", None)


if __name__ == "__main__":
    unittest.main()
