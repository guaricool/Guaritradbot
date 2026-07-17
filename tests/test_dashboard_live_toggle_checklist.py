"""
Gap fix: `PaperToLiveChecklist` used to run ONLY at bot process
startup (main.py). Toggling paper -> live from the dashboard
(`POST /api/mode`) wrote `mode_override.json` directly and never ran
it, so an open paper position could silently start being tracked as
live with no exchange counterpart. `set_mode()` in src/api/server.py
now runs the same checklist inline before writing the override to
live, whenever there are open paper positions.

Run: python -m unittest tests.test_dashboard_live_toggle_checklist -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _TempPaths:
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
        os.environ["DASHBOARD_TOKEN_SECRET_FILE"] = str(self.audit_dir / "token_secret.key")
        os.environ["DASHBOARD_BOT_PID_FILE"] = str(Path(self.tmp) / "guaritradbot_nonexistent.pid")
        return self

    def __exit__(self, *a):
        os.environ.clear()
        os.environ.update(self._old_env)


def _add_open_position(positions_path, position_id="p1", asset="BTC/USDT"):
    import time
    from src.data_store.positions import PositionRepository, Position
    repo = PositionRepository(positions_path)
    repo.add_open(Position(
        asset=asset,
        direction="long",
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit=52000.0,
        qty=0.001,
        risk_usd=10.0,
        entry_ts=time.time(),
        strategy="test",
        position_id=position_id,
    ))
    return repo


class SetModeLiveToggleChecklistTest(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from src.api.server import app
        return TestClient(app)

    def _auth_headers(self, client):
        r = client.post("/api/auth/login", json={"password": "testpw123"})
        token = r.json()["token"]
        return {"Authorization": f"Bearer {token}"}

    def test_no_open_positions_toggles_live_without_checklist(self):
        with _TempPaths() as t:
            from src.api import state
            state.set_brokers(broker_client=None, alpaca_broker=None)
            client = self._client()
            headers = self._auth_headers(client)
            r = client.post("/api/mode", json={"mode": "live"}, headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["mode"]["mode"], "live")

    def test_open_paper_positions_default_abort_blocks_transition(self):
        with _TempPaths() as t:
            from src.api import state
            state.set_brokers(broker_client=None, alpaca_broker=None)
            _add_open_position(t.positions_path)
            client = self._client()
            headers = self._auth_headers(client)
            r = client.post("/api/mode", json={"mode": "live"}, headers=headers)
            self.assertEqual(r.status_code, 409)
            # Mode override must NOT have flipped to live.
            self.assertEqual(state.read_mode(audit_path=t.audit_path).mode, "paper")

    def test_open_paper_positions_close_auto_action_proceeds_and_closes(self):
        with _TempPaths() as t:
            from src.api import state
            fake_broker = MagicMock()
            fake_broker.get_usdt_balance.return_value = 1000.0
            state.set_brokers(broker_client=fake_broker, alpaca_broker=None)
            with open(t.config_path, "w", encoding="utf-8") as f:
                json.dump({"live_transition": {"auto_action": "close"}}, f)
            os.environ["DASHBOARD_CONFIG_PATH"] = t.config_path
            _add_open_position(t.positions_path)

            client = self._client()
            headers = self._auth_headers(client)
            # APP_STATE["config"] is populated at app startup from
            # DASHBOARD_CONFIG_PATH; set it directly here so the test
            # doesn't depend on the app's startup event ordering.
            from src.api.server import APP_STATE
            APP_STATE["config"] = {"live_transition": {"auto_action": "close"}}

            r = client.post("/api/mode", json={"mode": "live"}, headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["mode"]["mode"], "live")
            self.assertIn("Closed 1 paper position", r.json()["note"])

            repo = state.get_position_repo(t.positions_path)
            self.assertEqual(repo.count_open(), 0)

    def test_broker_unreachable_blocks_transition(self):
        with _TempPaths() as t:
            from src.api import state
            state.set_brokers(broker_client=None, alpaca_broker=None)
            _add_open_position(t.positions_path)
            client = self._client()
            headers = self._auth_headers(client)
            r = client.post("/api/mode", json={"mode": "live"}, headers=headers)
            self.assertEqual(r.status_code, 409)
            self.assertIn("broker_unreachable", r.json()["detail"])

    def test_live_to_paper_never_triggers_checklist(self):
        """Only paper -> live is guarded; going back to paper is always safe."""
        with _TempPaths() as t:
            from src.api import state
            state.set_brokers(broker_client=None, alpaca_broker=None)
            _add_open_position(t.positions_path)
            client = self._client()
            headers = self._auth_headers(client)
            r = client.post("/api/mode", json={"mode": "paper"}, headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["mode"]["mode"], "paper")


if __name__ == "__main__":
    unittest.main()
