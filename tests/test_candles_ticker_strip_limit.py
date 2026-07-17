"""
Bug: the dashboard's TickerStrip requests `GET /api/candles?...&limit=2`
for every asset in the universe on every poll (just enough candles to
compute a % change for the ticker tape), but the endpoint's FastAPI
`Query(200, ge=10, le=1000)` rejected anything below 10 with a 422,
flooding the browser console on every poll for every asset.

Run: python -m unittest tests.test_candles_ticker_strip_limit -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _TempPaths:
    def __enter__(self):
        self._old_env = os.environ.copy()
        self.tmp = tempfile.mkdtemp()
        audit_dir = Path(self.tmp) / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        data_store = Path(self.tmp) / "data_store"
        data_store.mkdir(parents=True, exist_ok=True)
        os.environ["DASHBOARD_AUDIT_PATH"] = str(audit_dir / "audit.jsonl")
        os.environ["DASHBOARD_POSITIONS_PATH"] = str(data_store / "positions.json")
        os.environ["DASHBOARD_CONFIG_PATH"] = str(Path(self.tmp) / "config.yaml")
        os.environ["DASHBOARD_PASSWORD"] = "testpw123"
        os.environ["DASHBOARD_TOKEN_SECRET_FILE"] = str(audit_dir / "token_secret.key")
        os.environ["DASHBOARD_BOT_PID_FILE"] = str(Path(self.tmp) / "nonexistent.pid")
        return self

    def __exit__(self, *a):
        os.environ.clear()
        os.environ.update(self._old_env)


class CandlesTickerStripLimitTest(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from src.api.server import app
        return TestClient(app)

    def _auth_headers(self, client):
        r = client.post("/api/auth/login", json={"password": "testpw123"})
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def _fake_df(self):
        return pd.DataFrame({
            "Open": [100.0, 101.0], "High": [102.0, 103.0],
            "Low": [99.0, 100.0], "Close": [101.0, 102.0], "Volume": [10, 12],
        }, index=pd.date_range("2024-01-01", periods=2, freq="1h"))

    def test_limit_2_no_longer_422s(self):
        with _TempPaths():
            client = self._client()
            headers = self._auth_headers(client)
            with patch("src.data.yf_safe.safe_yf_download", return_value=self._fake_df()):
                r = client.get("/api/candles?asset=BTC-USD&interval=1h&limit=2", headers=headers)
            self.assertEqual(r.status_code, 200, r.text)

    def test_limit_1_still_rejected(self):
        """Floor is 2, not 0 -- a single candle can't compute a % change."""
        with _TempPaths():
            client = self._client()
            headers = self._auth_headers(client)
            r = client.get("/api/candles?asset=BTC-USD&interval=1h&limit=1", headers=headers)
            self.assertEqual(r.status_code, 422)

    def test_limit_200_default_still_works(self):
        with _TempPaths():
            client = self._client()
            headers = self._auth_headers(client)
            with patch("src.data.yf_safe.safe_yf_download", return_value=self._fake_df()):
                r = client.get("/api/candles?asset=BTC-USD&interval=1h", headers=headers)
            self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
