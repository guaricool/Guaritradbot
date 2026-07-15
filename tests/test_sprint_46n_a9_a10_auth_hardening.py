"""
Sprint 46N — audit A9/A10 tests: dashboard API auth hardening.

Covers (see AUDITORIA_COMPLETA_2026-07-11.md, findings A9/A10):
  A9(a) CORS defaults to no origins allowed (was "*").
  A9(b) token signing key is independent of DASHBOARD_PASSWORD (a
        rotatable, high-entropy secret combined with the password),
        instead of the password itself being the HMAC key.
  A9(c) the login password comparison is timing-safe
        (hmac.compare_digest, matching the signature check).
  A9(d) POST /api/auth/login is rate-limited/locked-out after
        repeated failures.
  A10   POST /api/restart has a cooldown between accepted restarts.

Run: python -m unittest tests.test_sprint_46n_a9_a10_auth_hardening -v
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _TempPaths:
    """Same pattern as test_sprint_46a_api.py's helper — isolated temp
    dir for audit/data_store/config paths AND the token-secret file,
    so these tests never touch the real repo checkout."""

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


# ============================================================
# A9(b): independent signing secret
# ============================================================

class SigningSecretIndependenceTest(unittest.TestCase):
    def test_signing_secret_is_not_the_password(self):
        with _TempPaths():
            from src.api import auth
            secret = auth._get_signing_secret()
            self.assertNotEqual(secret, b"testpw123")
            # High-entropy: 32 raw bytes (256 bits), not a short/guessable value.
            self.assertEqual(len(secret), 32)

    def test_signing_secret_persists_across_calls(self):
        """Same process, same secret file -> same secret each time
        (tokens issued minutes apart must still verify)."""
        with _TempPaths():
            from src.api import auth
            s1 = auth._get_signing_secret()
            s2 = auth._get_signing_secret()
            self.assertEqual(s1, s2)

    def test_signing_secret_persisted_to_disk(self):
        with _TempPaths() as t:
            from src.api import auth
            auth._get_signing_secret()
            secret_path = os.environ["DASHBOARD_TOKEN_SECRET_FILE"]
            self.assertTrue(os.path.exists(secret_path))

    def test_env_secret_overrides_persisted_file(self):
        with _TempPaths():
            os.environ["DASHBOARD_TOKEN_SECRET"] = "my-explicit-high-entropy-secret"
            from src.api import auth
            secret = auth._get_signing_secret()
            self.assertEqual(secret, b"my-explicit-high-entropy-secret")

    def test_rotating_secret_invalidates_existing_tokens(self):
        """Changing DASHBOARD_TOKEN_SECRET must invalidate tokens
        issued under the old secret -- this is the revocation lever
        the audit noted was missing."""
        with _TempPaths():
            from src.api import auth
            os.environ["DASHBOARD_TOKEN_SECRET"] = "secret-v1"
            token = auth.issue_token(password="testpw123")
            ok, _ = auth.verify_token(token, password="testpw123")
            self.assertTrue(ok)

            os.environ["DASHBOARD_TOKEN_SECRET"] = "secret-v2-rotated"
            ok2, reason2 = auth.verify_token(token, password="testpw123")
            self.assertFalse(ok2)
            self.assertEqual(reason2, "bad_signature")

    def test_wrong_password_still_fails_verification(self):
        """Preserve the pre-existing contract: a wrong password must
        still produce bad_signature, even though the signing key is no
        longer JUST the password (see _signing_key's docstring)."""
        with _TempPaths():
            from src.api import auth
            token = auth.issue_token(password="testpw123")
            ok, reason = auth.verify_token(token, password="WRONG")
            self.assertFalse(ok)
            self.assertEqual(reason, "bad_signature")


# ============================================================
# A9(c): timing-safe login compare
# ============================================================

class TimingSafeLoginCompareTest(unittest.TestCase):
    def test_correct_password_issues_token(self):
        with _TempPaths():
            from src.api import auth
            token = auth.issue_token(password="testpw123")
            self.assertIsInstance(token, str)

    def test_wrong_password_raises_permission_error(self):
        with _TempPaths():
            from src.api import auth
            with self.assertRaises(PermissionError):
                auth.issue_token(password="WRONG")

    def test_prefix_matching_password_still_rejected(self):
        """A password that shares a long prefix with the real one must
        still be rejected outright -- guards against a naive `!=`
        having been swapped for something that short-circuits on
        prefix match instead of a real compare_digest call."""
        with _TempPaths():
            from src.api import auth
            with self.assertRaises(PermissionError):
                auth.issue_token(password="testpw12")  # one char short
            with self.assertRaises(PermissionError):
                auth.issue_token(password="testpw1234")  # one char extra


# ============================================================
# A9(a): CORS fails closed
# ============================================================

class CorsFailsClosedTest(unittest.TestCase):
    def test_no_origins_configured_means_no_origins_allowed(self):
        with _TempPaths():
            os.environ.pop("DASHBOARD_CORS_ORIGINS", None)
            # Re-import server fresh so the module-level _CORS_ORIGINS
            # is recomputed from the current env.
            import importlib
            import src.api.server as server_module
            importlib.reload(server_module)
            self.assertEqual(server_module._CORS_ORIGINS, [])

    def test_configured_origins_are_parsed(self):
        with _TempPaths():
            os.environ["DASHBOARD_CORS_ORIGINS"] = "http://a.example.com, http://b.example.com"
            import importlib
            import src.api.server as server_module
            importlib.reload(server_module)
            self.assertEqual(
                server_module._CORS_ORIGINS,
                ["http://a.example.com", "http://b.example.com"],
            )
            # Restore module state for subsequent tests in this process.
            os.environ.pop("DASHBOARD_CORS_ORIGINS", None)
            importlib.reload(server_module)


# ============================================================
# A9(d): login rate limiting
# ============================================================

class LoginRateLimiterUnitTest(unittest.TestCase):
    def test_allows_until_threshold(self):
        from src.api.auth import LoginRateLimiter
        rl = LoginRateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)
        key = "1.2.3.4"
        for _ in range(2):
            allowed, _ = rl.check(key)
            self.assertTrue(allowed)
            rl.record_failure(key)
        # 2 failures recorded, threshold is 3 -> still allowed
        allowed, _ = rl.check(key)
        self.assertTrue(allowed)

    def test_locks_out_after_max_attempts(self):
        from src.api.auth import LoginRateLimiter
        rl = LoginRateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)
        key = "1.2.3.4"
        for _ in range(3):
            rl.record_failure(key)
        allowed, retry_after = rl.check(key)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_success_clears_history(self):
        from src.api.auth import LoginRateLimiter
        rl = LoginRateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)
        key = "1.2.3.4"
        rl.record_failure(key)
        rl.record_failure(key)
        rl.record_success(key)
        # History cleared -> two MORE failures shouldn't lock out yet
        rl.record_failure(key)
        rl.record_failure(key)
        allowed, _ = rl.check(key)
        self.assertTrue(allowed)

    def test_different_keys_tracked_independently(self):
        from src.api.auth import LoginRateLimiter
        rl = LoginRateLimiter(max_attempts=2, window_seconds=60, lockout_seconds=60)
        for _ in range(2):
            rl.record_failure("attacker_ip")
        allowed_attacker, _ = rl.check("attacker_ip")
        allowed_legit, _ = rl.check("legit_ip")
        self.assertFalse(allowed_attacker)
        self.assertTrue(allowed_legit)

    def test_old_failures_outside_window_dont_count(self):
        from src.api.auth import LoginRateLimiter
        rl = LoginRateLimiter(max_attempts=2, window_seconds=0.05, lockout_seconds=60)
        key = "1.2.3.4"
        rl.record_failure(key)
        time.sleep(0.1)  # window expires
        rl.record_failure(key)
        # Only 1 failure counted within the (expired+reset) window
        allowed, _ = rl.check(key)
        self.assertTrue(allowed)


class LoginEndpointRateLimitTest(unittest.TestCase):
    """Exercise the rate limiter through the actual HTTP endpoint."""

    def setUp(self):
        self._ctx = _TempPaths()
        self._ctx.__enter__()
        os.environ["DASHBOARD_LOGIN_MAX_ATTEMPTS"] = "3"
        os.environ["DASHBOARD_LOGIN_WINDOW_SECONDS"] = "60"
        os.environ["DASHBOARD_LOGIN_LOCKOUT_SECONDS"] = "60"
        from src.api import auth
        auth.login_rate_limiter.reset()
        # Rebuild the limiter with the smaller test thresholds.
        auth.login_rate_limiter.max_attempts = 3
        auth.login_rate_limiter.window_seconds = 60
        auth.login_rate_limiter.lockout_seconds = 60
        from fastapi.testclient import TestClient
        from src.api.server import app
        self.client = TestClient(app)

    def tearDown(self):
        from src.api import auth
        auth.login_rate_limiter.reset()
        self._ctx.__exit__(None, None, None)

    def test_locked_out_after_repeated_failures(self):
        for _ in range(3):
            r = self.client.post("/api/auth/login", json={"password": "WRONG"})
            self.assertEqual(r.status_code, 401)
        # 4th attempt (even with the CORRECT password) is blocked by
        # the rate limiter before the password is even checked.
        r = self.client.post("/api/auth/login", json={"password": "testpw123"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)

    def test_successful_login_clears_lockout_history(self):
        r = self.client.post("/api/auth/login", json={"password": "WRONG"})
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/api/auth/login", json={"password": "testpw123"})
        self.assertEqual(r.status_code, 200)
        # History cleared -> fresh failures shouldn't immediately lock out
        r = self.client.post("/api/auth/login", json={"password": "WRONG"})
        self.assertEqual(r.status_code, 401)


# ============================================================
# A10: restart cooldown
# ============================================================

class RestartCooldownTest(unittest.TestCase):
    def setUp(self):
        self._ctx = _TempPaths()
        self._ctx.__enter__()
        os.environ["DASHBOARD_RESTART_COOLDOWN_SECONDS"] = "3600"  # long, deterministic
        import importlib
        import src.api.server as server_module
        importlib.reload(server_module)
        self.server_module = server_module
        from fastapi.testclient import TestClient
        self.client = TestClient(server_module.app)
        from src.api import auth
        self.token = auth.issue_token(password="testpw123")
        self.auth_headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        self.server_module._last_restart_ts = None
        self._ctx.__exit__(None, None, None)

    def test_second_restart_within_cooldown_is_blocked(self):
        # Simulate a prior successful restart just now.
        self.server_module._last_restart_ts = time.time()
        r = self.client.post("/api/restart", headers=self.auth_headers)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)

    def test_no_prior_restart_is_not_blocked_by_cooldown(self):
        # No _last_restart_ts set -> cooldown doesn't apply; the
        # request proceeds to the (missing) pid file check instead.
        self.server_module._last_restart_ts = None
        r = self.client.post("/api/restart", headers=self.auth_headers)
        self.assertIn(r.status_code, (404, 500))

    def test_restart_after_cooldown_elapsed_is_not_blocked(self):
        self.server_module._last_restart_ts = time.time() - 4000  # older than 3600s cooldown
        r = self.client.post("/api/restart", headers=self.auth_headers)
        self.assertIn(r.status_code, (404, 500))  # proceeds past the cooldown gate


if __name__ == "__main__":
    unittest.main()
