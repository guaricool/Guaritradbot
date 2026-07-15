"""
Sprint 46A — Tests for the bot HTTP API + WebSocket.

Tests for:
  - src/api/auth.py       (token issue/verify, expiry, fails-closed)
  - src/api/state.py      (snapshot, audit, mode read/write, position close)
  - src/api/server.py     (all REST endpoints, auth gating, WS hello/heartbeat)

Run: python -m unittest tests.test_sprint_46a_api -v
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ============================================================
# Helpers
# ============================================================

class _TempPaths:
    """Context manager: provide a temp dir with audit/ + data_store/ subdirs,
    set env vars to point the API at them, and reset on exit."""

    def __enter__(self):
        self._old_env = os.environ.copy()
        self.tmp = tempfile.mkdtemp()
        self.audit_dir = Path(self.tmp) / "audit"
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.data_store = Path(self.tmp) / "data_store"
        self.data_store.mkdir(parents=True, exist_ok=True)
        self.audit_path = str(self.audit_dir / "audit.jsonl")
        self.positions_path = str(self.data_store / "positions.json")
        self.config_path = str(Path(self.tmp) / "config.yaml")
        os.environ["DASHBOARD_AUDIT_PATH"] = self.audit_path
        os.environ["DASHBOARD_POSITIONS_PATH"] = self.positions_path
        os.environ["DASHBOARD_CONFIG_PATH"] = self.config_path
        os.environ["DASHBOARD_PASSWORD"] = "testpw123"
        # Sprint 46N (audit A9): auth.py now derives the token-signing
        # key from an independent secret persisted at
        # DASHBOARD_TOKEN_SECRET_FILE (default audit/token_secret.key,
        # relative to CWD) if DASHBOARD_TOKEN_SECRET isn't set. Without
        # pointing this at the temp dir like every other path above,
        # test runs would silently create/reuse a stray
        # audit/token_secret.key in the real repo checkout.
        os.environ["DASHBOARD_TOKEN_SECRET_FILE"] = str(self.audit_dir / "token_secret.key")
        os.environ["DASHBOARD_BOT_PID_FILE"] = str(Path(self.tmp) / "guaritradbot_nonexistent.pid")
        return self

    def __exit__(self, *a):
        os.environ.clear()
        os.environ.update(self._old_env)


# ============================================================
# auth.py
# ============================================================

class AuthTokenTest(unittest.TestCase):
    def test_issue_and_verify(self):
        with _TempPaths():
            from src.api import auth
            token = auth.issue_token(password="testpw123")
            ok, reason = auth.verify_token(token, password="testpw123")
            self.assertTrue(ok)
            self.assertEqual(reason, "ok")

    def test_wrong_password_rejected(self):
        with _TempPaths():
            from src.api import auth
            with self.assertRaises(PermissionError):
                auth.issue_token(password="WRONG")

    def test_verify_wrong_password_fails(self):
        with _TempPaths():
            from src.api import auth
            token = auth.issue_token(password="testpw123")
            ok, reason = auth.verify_token(token, password="OTHER")
            self.assertFalse(ok)
            self.assertEqual(reason, "bad_signature")

    def test_no_password_env_fails_closed(self):
        with _TempPaths():
            # Remove the password env var
            del os.environ["DASHBOARD_PASSWORD"]
            from src.api import auth
            with self.assertRaises(PermissionError):
                auth.issue_token(password="testpw123")
            ok, reason = auth.verify_token("any.token", password=None)
            self.assertFalse(ok)
            self.assertEqual(reason, "auth_disabled")

    def test_malformed_token(self):
        with _TempPaths():
            from src.api import auth
            ok, reason = auth.verify_token("not-a-token", password="testpw123")
            self.assertFalse(ok)
            self.assertEqual(reason, "malformed")
            ok, reason = auth.verify_token("a.b.c", password="testpw123")
            self.assertFalse(ok)
            self.assertEqual(reason, "malformed")

    def test_token_format_is_two_parts(self):
        with _TempPaths():
            from src.api import auth
            token = auth.issue_token(password="testpw123")
            parts = token.split(".")
            # Exactly 2 parts: <b64url_ts>.<b64url_sig>
            self.assertEqual(len(parts), 2)
            self.assertGreater(len(parts[0]), 0)
            self.assertGreater(len(parts[1]), 0)


# ============================================================
# state.py — Mode
# ============================================================

class ModeReadWriteTest(unittest.TestCase):
    def test_default_mode_is_paper(self):
        """No override file + no config → mode is 'paper'."""
        with _TempPaths():
            from src.api.state import read_mode
            m = read_mode()
            self.assertEqual(m.mode, "paper")
            self.assertFalse(m.mandate_enabled)

    def test_write_mode_creates_override_file(self):
        with _TempPaths() as t:
            from src.api.state import write_mode, read_mode
            m = write_mode(mandate_enabled=True, switched_by="test")
            self.assertEqual(m.mode, "live")
            self.assertTrue(m.mandate_enabled)
            self.assertTrue(Path(t.audit_dir / "mode_override.json").exists())
            # Re-read from disk to confirm
            m2 = read_mode()
            self.assertEqual(m2.mode, "live")
            self.assertEqual(m2.switched_by, "test")

    def test_mode_toggle_paper_to_live(self):
        with _TempPaths() as t:
            from src.api.state import write_mode, read_mode
            write_mode(mandate_enabled=False)
            self.assertEqual(read_mode().mode, "paper")
            write_mode(mandate_enabled=True)
            self.assertEqual(read_mode().mode, "live")
            write_mode(mandate_enabled=False)
            self.assertEqual(read_mode().mode, "paper")

    def test_malformed_override_file_falls_back(self):
        with _TempPaths() as t:
            from src.api.state import read_mode
            (t.audit_dir / "mode_override.json").write_text("{ this is not json", encoding="utf-8")
            m = read_mode()
            # Should NOT raise; should fall back to defaults.
            self.assertEqual(m.mode, "paper")


# ============================================================
# state.py — Position summary + snapshot
# ============================================================

class StateSnapshotTest(unittest.TestCase):
    def test_empty_book(self):
        with _TempPaths():
            from src.api.state import build_state_snapshot
            snap = build_state_snapshot()
            self.assertEqual(snap.open_count, 0)
            self.assertEqual(snap.positions, [])
            self.assertEqual(snap.total_unrealized_usd, 0.0)
            self.assertEqual(snap.total_exposure_usd, 0.0)

    @patch("src.api.state._fetch_one_price")
    def test_open_position_with_live_price(self, mock_fetch):
        mock_fetch.return_value = (55000.0, "live")
        with _TempPaths() as t:
            from src.api.state import build_state_snapshot
            from src.data_store.positions import PositionRepository, Position
            repo = PositionRepository(path=t.positions_path)
            pos = Position(
                asset="BTC-USD", direction="long",
                entry_price=50000, stop_loss=49000, take_profit=52000,
                qty=0.001, risk_usd=5, entry_ts=time.time() - 3600,
                strategy="momentum",
            )
            repo.add_open(pos)  # writes to disk so build_state_snapshot can read
            snap = build_state_snapshot(positions_path=t.positions_path)
            self.assertEqual(snap.open_count, 1)
            p = snap.positions[0]
            self.assertEqual(p.asset, "BTC-USD")
            # 55000 - 50000 = 5000, * 0.001 = 5.0
            self.assertAlmostEqual(p.unrealized_pnl_usd, 5.0, places=2)
            # 5.0 / 50.0 = 0.10 = 10%
            self.assertAlmostEqual(p.unrealized_pnl_pct, 0.10, places=2)
            self.assertEqual(p.current_price_source, "live")

    @patch("src.api.state._fetch_one_price")
    def test_open_position_with_fetch_failure_falls_back(self, mock_fetch):
        mock_fetch.return_value = (None, "fetch_failed")
        with _TempPaths() as t:
            from src.api.state import build_state_snapshot
            from src.data_store.positions import PositionRepository, Position
            repo = PositionRepository(path=t.positions_path)
            pos = Position(
                asset="BTC-USD", direction="long",
                entry_price=50000, stop_loss=49000, take_profit=52000,
                qty=0.001, risk_usd=5, entry_ts=time.time(),
                strategy="momentum",
            )
            repo.add_open(pos)
            snap = build_state_snapshot(positions_path=t.positions_path)
            p = snap.positions[0]
            # When fetch fails, current_price falls back to entry_price
            self.assertEqual(p.current_price, p.entry_price)
            self.assertEqual(p.current_price_source, "entry_fallback")
            # P&L is 0 because current = entry
            self.assertEqual(p.unrealized_pnl_usd, 0.0)

    @patch("src.api.state._fetch_one_price")
    def test_short_position_pnl(self, mock_fetch):
        mock_fetch.return_value = (2900.0, "live")
        with _TempPaths() as t:
            from src.api.state import build_state_snapshot
            from src.data_store.positions import PositionRepository, Position
            repo = PositionRepository(path=t.positions_path)
            pos = Position(
                asset="ETH-USD", direction="short",
                entry_price=3000, stop_loss=3100, take_profit=2800,
                qty=0.01, risk_usd=1, entry_ts=time.time(),
                strategy="mean_reversion",
            )
            repo.add_open(pos)
            snap = build_state_snapshot(positions_path=t.positions_path)
            p = snap.positions[0]
            # Short: 3000 - 2900 = 100, * 0.01 = 1.0 (gain)
            self.assertAlmostEqual(p.unrealized_pnl_usd, 1.0, places=2)


class AuditBuilderTest(unittest.TestCase):
    def test_empty_audit(self):
        with _TempPaths() as t:
            from src.api.state import build_audit
            out = build_audit(audit_path=t.audit_path)
            self.assertEqual(out, [])

    def test_audit_filters_by_event_type(self):
        with _TempPaths() as t:
            from src.safety.audit_ledger import AuditLedger
            audit = AuditLedger(path=t.audit_path)
            audit.append("TRADE_APPROVED", {"asset": "BTC-USD", "notional": 10.0})
            audit.append("SYSTEM_ERROR", {"kind": "TEST"})
            audit.append("TRADE_APPROVED", {"asset": "ETH-USD", "notional": 20.0})
            from src.api.state import build_audit
            only_trades = build_audit(event_type="TRADE_APPROVED", audit_path=t.audit_path)
            self.assertEqual(len(only_trades), 2)
            self.assertTrue(all(e.event_type == "TRADE_APPROVED" for e in only_trades))

    def test_audit_limit(self):
        with _TempPaths() as t:
            from src.safety.audit_ledger import AuditLedger
            audit = AuditLedger(path=t.audit_path)
            for i in range(50):
                audit.append("EVENT", {"i": i})
            from src.api.state import build_audit
            out = build_audit(limit=10, audit_path=t.audit_path)
            self.assertEqual(len(out), 10)

    def test_audit_after_filter(self):
        with _TempPaths() as t:
            from src.safety.audit_ledger import AuditLedger
            audit = AuditLedger(path=t.audit_path)
            audit.append("FIRST", {})
            time.sleep(0.05)
            cutoff = time.time()
            time.sleep(0.05)
            audit.append("SECOND", {})
            from src.api.state import build_audit
            out = build_audit(after=cutoff, audit_path=t.audit_path)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0].event_type, "SECOND")

    def test_audit_newest_first(self):
        with _TempPaths() as t:
            from src.safety.audit_ledger import AuditLedger
            audit = AuditLedger(path=t.audit_path)
            audit.append("OLD", {})
            time.sleep(0.05)
            audit.append("NEW", {})
            from src.api.state import build_audit
            out = build_audit(audit_path=t.audit_path)
            self.assertEqual(out[0].event_type, "NEW")
            self.assertEqual(out[1].event_type, "OLD")


class ClosePositionTest(unittest.TestCase):
    def test_close_open_position(self):
        with _TempPaths() as t:
            from src.data_store.positions import PositionRepository, Position
            from src.api.state import close_position
            repo = PositionRepository(path=t.positions_path)
            pos = Position(
                asset="BTC-USD", direction="long",
                entry_price=50000, stop_loss=49000, take_profit=52000,
                qty=0.001, risk_usd=5, entry_ts=time.time(),
                strategy="momentum",
            )
            repo.add_open(pos)  # persist to disk so close_position can read
            result = close_position(
                position_id=pos.position_id,
                audit_path=t.audit_path,
                positions_path=t.positions_path,
            )
            self.assertIsNotNone(result)
            self.assertEqual(result["asset"], "BTC-USD")
            # Manual close uses entry price as fallback, so realized P&L = 0
            self.assertEqual(result["realized_pnl_usd"], 0.0)
            # Audit event was written
            from src.safety.audit_ledger import AuditLedger
            audit = AuditLedger(path=t.audit_path)
            events = audit.read_by_type("MANUAL_CLOSE")
            self.assertEqual(len(events), 1)

    def test_close_nonexistent_returns_none(self):
        with _TempPaths() as t:
            from src.api.state import close_position
            result = close_position(
                position_id="pos_does_not_exist",
                audit_path=t.audit_path,
                positions_path=t.positions_path,
            )
            self.assertIsNone(result)


# ============================================================
# server.py — REST endpoints
# ============================================================

class ServerEndpointsTest(unittest.TestCase):
    """Drive the FastAPI app via TestClient to verify the REST surface.

    TestClient wraps the app with a real HTTP loopback so each test
    is a black-box exercise of the routing + auth + serialization.
    """

    def setUp(self):
        self._ctx = _TempPaths()
        self._ctx.__enter__()
        # Importing FastAPI's TestClient inside the test ensures
        # the lifespan handler runs (loads config, etc.).
        from fastapi.testclient import TestClient
        from src.api.server import app
        self.client = TestClient(app)
        # Get a valid token for authenticated tests
        from src.api import auth
        self.token = auth.issue_token(password="testpw123")
        self.auth_headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    # --- Public endpoints ---
    #
    # Sprint 46N (audit C5): only /api/health remains public. Every
    # other GET below used to be public too — the entire trading
    # state was readable by anyone who found the API's URL, no token
    # required. Each test now asserts BOTH halves of the fix: 401
    # without a token, 200 with one (so a future regression that
    # removes the auth dependency, OR one that breaks it so no valid
    # token ever works, both get caught).

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("audit_path", body)

    def test_state_empty(self):
        r = self.client.get("/api/state", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["open_count"], 0)
        self.assertEqual(body["mode"]["mode"], "paper")

    def test_state_without_auth_rejected(self):
        r = self.client.get("/api/state")
        self.assertEqual(r.status_code, 401)

    def test_positions_empty(self):
        r = self.client.get("/api/positions", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    def test_positions_without_auth_rejected(self):
        r = self.client.get("/api/positions")
        self.assertEqual(r.status_code, 401)

    def test_mode_get_default(self):
        r = self.client.get("/api/mode", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"], "paper")

    def test_mode_get_without_auth_rejected(self):
        r = self.client.get("/api/mode")
        self.assertEqual(r.status_code, 401)

    def test_audit_empty(self):
        r = self.client.get("/api/audit", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    def test_audit_without_auth_rejected(self):
        r = self.client.get("/api/audit")
        self.assertEqual(r.status_code, 401)

    def test_stats_empty(self):
        r = self.client.get("/api/stats", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["open_count"], 0)
        self.assertEqual(body["total_exposure_usd"], 0.0)

    def test_stats_without_auth_rejected(self):
        r = self.client.get("/api/stats")
        self.assertEqual(r.status_code, 401)

    def test_equity_empty(self):
        r = self.client.get("/api/equity", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["series"], [])

    def test_equity_without_auth_rejected(self):
        r = self.client.get("/api/equity")
        self.assertEqual(r.status_code, 401)

    def test_config_without_auth_rejected(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 401)

    def test_risk_config_without_auth_rejected(self):
        r = self.client.get("/api/risk-config")
        self.assertEqual(r.status_code, 401)

    def test_trading_pause_without_auth_rejected(self):
        r = self.client.get("/api/trading-pause")
        self.assertEqual(r.status_code, 401)

    def test_signals_without_auth_rejected(self):
        r = self.client.get("/api/signals")
        self.assertEqual(r.status_code, 401)

    # --- Auth ---

    def test_login_success(self):
        r = self.client.post("/api/auth/login", json={"password": "testpw123"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("token", body)
        self.assertEqual(body["token_type"], "Bearer")
        self.assertGreater(body["expires_in_s"], 0)

    def test_login_wrong_password(self):
        r = self.client.post("/api/auth/login", json={"password": "WRONG"})
        self.assertEqual(r.status_code, 401)

    def test_login_missing_password(self):
        r = self.client.post("/api/auth/login", json={})
        self.assertEqual(r.status_code, 422)  # pydantic validation

    def test_set_mode_without_auth_rejected(self):
        r = self.client.post("/api/mode", json={"mode": "live"})
        self.assertEqual(r.status_code, 401)

    def test_set_mode_with_invalid_token_rejected(self):
        r = self.client.post(
            "/api/mode",
            json={"mode": "live"},
            headers={"Authorization": "Bearer not-a-valid-token"},
        )
        self.assertEqual(r.status_code, 401)

    def test_set_mode_paper_to_live(self):
        r = self.client.post(
            "/api/mode",
            json={"mode": "live", "switched_by": "test"},
            headers=self.auth_headers,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"]["mode"], "live")
        self.assertEqual(body["mode"]["switched_by"], "test")

    def test_set_mode_invalid_value(self):
        r = self.client.post(
            "/api/mode",
            json={"mode": "BOGUS"},
            headers=self.auth_headers,
        )
        self.assertEqual(r.status_code, 400)

    def test_set_mode_then_get_persists(self):
        """Set live, get via /api/mode → still live. Disk-persisted."""
        r1 = self.client.post(
            "/api/mode", json={"mode": "live"}, headers=self.auth_headers,
        )
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.get("/api/mode", headers=self.auth_headers)
        self.assertEqual(r2.json()["mode"], "live")

    # --- Position close (auth) ---

    def test_close_position_without_auth_rejected(self):
        r = self.client.post("/api/positions/pos_xxx/close")
        self.assertEqual(r.status_code, 401)

    def test_close_position_not_found(self):
        r = self.client.post(
            "/api/positions/pos_does_not_exist/close",
            headers=self.auth_headers,
        )
        self.assertEqual(r.status_code, 404)

    def test_close_position_success(self):
        # Add a position to the repo, then close via API
        from src.data_store.positions import PositionRepository, Position
        repo = PositionRepository(path=self._ctx.positions_path)
        pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=50000, stop_loss=49000, take_profit=52000,
            qty=0.001, risk_usd=5, entry_ts=time.time(),
            strategy="test",
        )
        repo.add_open(pos)  # persist to disk
        r = self.client.post(
            f"/api/positions/{pos.position_id}/close",
            headers=self.auth_headers,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["asset"], "BTC-USD")

    # --- Restart ---

    def test_restart_no_pid_file(self):
        r = self.client.post(
            "/api/restart", headers=self.auth_headers,
        )
        # Either 404 (no pid file) or 500 (read error). Both are
        # acceptable — the point is the auth gate works.
        self.assertIn(r.status_code, (404, 500))

    def test_restart_without_auth_rejected(self):
        r = self.client.post("/api/restart")
        self.assertEqual(r.status_code, 401)

    @unittest.skipIf(
        sys.platform == "win32",
        "os.kill(SIGTERM) is a no-op on Windows (WinError 87); the restart "
        "endpoint is exercised end-to-end on the Linux VPS deployment, "
        "where SIGTERM works as expected. CI on Linux should run this.",
    )
    def test_restart_with_fake_pid(self):
        # Create a fake pid file with a non-existent pid → ProcessLookupError → 404
        pid_path = "/tmp/guaritradbot_dashboard_test_fake.pid"
        if os.path.exists(pid_path):
            os.unlink(pid_path)
        Path(pid_path).write_text("9999999", encoding="utf-8")
        try:
            os.environ["DASHBOARD_BOT_PID_FILE"] = pid_path
            r = self.client.post(
                "/api/restart", headers=self.auth_headers,
            )
            self.assertEqual(r.status_code, 404)
        finally:
            os.environ.pop("DASHBOARD_BOT_PID_FILE", None)
            if os.path.exists(pid_path):
                os.unlink(pid_path)

    # --- Allocation / risk ---

    def test_allocation_empty(self):
        r = self.client.get("/api/allocation", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("actual_weights", body)
        self.assertIn("target_weights", body)

    def test_allocation_without_auth_rejected(self):
        r = self.client.get("/api/allocation")
        self.assertEqual(r.status_code, 401)

    def test_risk_stress_empty(self):
        r = self.client.get("/api/risk/stress", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("scenarios", body)

    def test_risk_stress_without_auth_rejected(self):
        r = self.client.get("/api/risk/stress")
        self.assertEqual(r.status_code, 401)

    def test_risk_correlation_empty(self):
        r = self.client.get("/api/risk/correlation", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("assets", body)

    def test_risk_correlation_without_auth_rejected(self):
        r = self.client.get("/api/risk/correlation")
        self.assertEqual(r.status_code, 401)

    def test_risk_cvar_empty(self):
        r = self.client.get("/api/risk/cvar", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("note", body)

    def test_risk_cvar_without_auth_rejected(self):
        r = self.client.get("/api/risk/cvar")
        self.assertEqual(r.status_code, 401)


# ============================================================
# WebSocket
# ============================================================

class WebSocketTest(unittest.TestCase):
    def setUp(self):
        self._ctx = _TempPaths()
        self._ctx.__enter__()
        from src.api import auth
        self.token = auth.issue_token(password="testpw123")

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def test_ws_with_valid_token(self):
        """The client receives a 'hello' message immediately on connect."""
        from fastapi.testclient import TestClient
        from src.api.server import app
        client = TestClient(app)
        with client.websocket_connect(f"/ws/live?token={self.token}") as ws:
            msg = ws.receive_text()
            data = json.loads(msg)
            self.assertEqual(data["type"], "hello")
            self.assertIn("ts", data)

    def test_ws_with_invalid_token_rejected(self):
        from fastapi.testclient import TestClient
        from src.api.server import app
        client = TestClient(app)
        with self.assertRaises(Exception):
            with client.websocket_connect("/ws/live?token=garbage.token.value"):
                pass

    def test_ws_ping_pong(self):
        from fastapi.testclient import TestClient
        from src.api.server import app
        client = TestClient(app)
        with client.websocket_connect(f"/ws/live?token={self.token}") as ws:
            # Drain the hello
            ws.receive_text()
            ws.send_text("ping")
            data = ws.receive_text()
            self.assertEqual(data, "pong")


if __name__ == "__main__":
    unittest.main()
