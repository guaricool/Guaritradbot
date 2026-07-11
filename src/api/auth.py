"""
Sprint 46A — Token-based auth for the bot HTTP API.

Design goals
------------
- **No external deps beyond stdlib + FastAPI's `Header`**: simple to
  reason about, easy to rotate.
- **One source of truth for the password**: `DASHBOARD_PASSWORD` env
  var. The same password that the Streamlit gate uses (Sprint 45 H8)
  also unlocks API tokens. This means you set the password once and
  both the legacy dashboard AND the new API work.
- **Short-lived tokens**: 12 hours by default. Long enough for a
  working day, short enough that a leaked token expires overnight.
- **Stateless verification**: `verify_token()` is a pure function
  over (token, secret, now). No DB lookup, no session table. The
  token itself encodes the issue timestamp; the secret signs it.
- **Fails CLOSED**: if no `DASHBOARD_PASSWORD` is set, the auth layer
  refuses to issue tokens AND refuses to verify any token. Same
  "minimal but secure" default the Sprint 45 H8 streamlit gate uses.

Token format
------------
    base64url(timestamp_issued) + "." + base64url(HMAC-SHA256(secret, timestamp_issued))

`timestamp_issued` is unix seconds (int). The HMAC is over the raw
integer string, NOT over the base64 form, so it's stable across
re-encoding.

This format follows the same "2-part dot-separated token" pattern
the user already has in memory (from the MedSysVE OTP token work,
which was a "lessons learned" — see cross-project-patterns.md
"Tokens opacos: NUNCA usar `.` como separador de payload"). We use
`.` only between exactly 2 parts (timestamp and signature), never
inside the payload, so email addresses and UUIDs in the payload
can't trip a naive split('.') parser.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

from fastapi import Header, HTTPException, status


DEFAULT_TOKEN_TTL_SECONDS = 12 * 3600  # 12 hours
TOKEN_TTL_SECONDS = int(os.getenv("DASHBOARD_TOKEN_TTL_SECONDS", str(DEFAULT_TOKEN_TTL_SECONDS)))


def _get_password() -> Optional[str]:
    """Return DASHBOARD_PASSWORD from env, or None if not set.

    Fails CLOSED: if not set, the API refuses ALL authenticated
    requests. This forces the operator to set a password rather than
    being able to forget one and silently leave the API open.
    """
    pw = os.getenv("DASHBOARD_PASSWORD")
    if pw is None or pw == "":
        return None
    return pw


def _get_signing_secret() -> bytes:
    """Sprint 46N (audit A9): the HIGH-ENTROPY, PASSWORD-INDEPENDENT
    half of the token-signing key.

    Before this fix, `issue_token`/`verify_token` used the raw
    `DASHBOARD_PASSWORD` string itself as the HMAC key. Two problems
    with that: (1) if the human-chosen password is weak, an attacker
    who observes even one valid `(timestamp, signature)` pair (e.g. a
    token in a proxy log, a leaked screenshot) can brute-force the
    password OFFLINE by recomputing `HMAC(guess, timestamp)` for each
    guess and comparing to the observed signature — no requests to the
    server needed, so the login rate limiter below can't stop it;
    (2) the signing key and the login credential were the exact same
    secret, so there was no way to rotate "what signs tokens" without
    also rotating "what the human types to log in".

    This function returns an independent 256-bit secret, used together
    with the password (see `_signing_key` below) to derive the actual
    HMAC key — so guessing the password alone is no longer enough to
    forge or verify a signature, and the signing secret can be rotated
    on its own (which also has the side effect of invalidating every
    previously-issued token, a useful revocation lever the audit noted
    was missing).

    Preference order:
      1. `DASHBOARD_TOKEN_SECRET` env var, if explicitly set (e.g. via
         a Coolify secret) — no disk write needed, highest operator
         control.
      2. A secret persisted at `DASHBOARD_TOKEN_SECRET_FILE` (default
         `audit/token_secret.key` — inside the `bot_audit` Docker
         volume, so it survives redeploys and every previously-issued
         token keeps verifying across restarts). Generated ONCE with
         `secrets.token_bytes(32)` the first time a process needs it.
         This mirrors the "must work with zero extra operator config"
         pattern already used for `mode_override.json` etc.
      3. If persisting fails (read-only filesystem, permissions), the
         freshly-generated secret is still returned and used for THIS
         process's lifetime — strictly better than the old
         password-as-signing-key behavior even without persistence;
         it just won't survive a restart (a fresh secret is generated
         next boot, invalidating outstanding tokens — annoying but
         safe, never silently falls back to the weak old scheme).
    """
    env_secret = os.getenv("DASHBOARD_TOKEN_SECRET")
    if env_secret:
        return env_secret.encode("utf-8")

    path = os.getenv("DASHBOARD_TOKEN_SECRET_FILE", "audit/token_secret.key")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read().strip()
            if existing:
                return bytes.fromhex(existing)
    except Exception:
        pass

    new_secret = secrets.token_bytes(32)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_secret.hex())
    except Exception:
        pass
    return new_secret


def _signing_key(password: str) -> bytes:
    """Derive the actual HMAC key from the independent high-entropy
    secret AND the login password.

    Combining both (rather than using either alone) means: a wrong
    `password` argument still produces a different key -> signature
    mismatch (preserves the existing "wrong password -> bad_signature"
    contract callers/tests rely on), while an attacker who only
    observes ordinary token traffic can no longer brute-force the
    password offline (see `_get_signing_secret`'s docstring) without
    also knowing the independent secret.
    """
    return hmac.new(_get_signing_secret(), password.encode("utf-8"), hashlib.sha256).digest()


def _b64url(data: bytes) -> str:
    """base64url encode without padding (URL-safe)."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    """base64url decode accepting missing padding."""
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def issue_token(password: Optional[str] = None, ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
    """Issue a signed token for the given password (or env password).

    Returns a string `ts.sig` that verify_token() can check.

    Raises:
        PermissionError: if DASHBOARD_PASSWORD is not set OR if the
        supplied password doesn't match. We raise on mismatch (rather
        than returning a "garbage" token) so callers can't accidentally
        issue a "valid" token for a wrong password via timing analysis
        or retry behavior.
    """
    expected = _get_password()
    if expected is None:
        raise PermissionError("DASHBOARD_PASSWORD not set; auth is disabled (fails closed)")
    # Sprint 46N (audit A9): timing-safe comparison. The signature
    # check below already used `hmac.compare_digest`; this one (the
    # actual login credential check) used a plain `!=`, which leaks
    # information about how many leading characters matched via
    # response timing — small on its own, but there's no reason to
    # accept ANY timing side-channel here when the safe primitive
    # costs nothing. `compare_digest` requires equal-length bytes
    # objects to run in constant time; encode both sides identically.
    if password is None or not hmac.compare_digest(
        password.encode("utf-8"), expected.encode("utf-8")
    ):
        raise PermissionError("invalid password")
    ts = int(time.time())
    payload = str(ts).encode("ascii")
    sig = hmac.new(_signing_key(expected), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


def verify_token(token: str, password: Optional[str] = None) -> Tuple[bool, str]:
    """Verify a token. Returns (ok, reason). Never raises.

    Reasons on failure:
      - "auth_disabled": no DASHBOARD_PASSWORD set
      - "malformed": token doesn't have exactly 2 parts
      - "expired": older than ttl_seconds
      - "bad_signature": HMAC mismatch
    """
    expected = password or _get_password()
    if expected is None:
        return False, "auth_disabled"
    if not token or "." not in token:
        return False, "malformed"
    parts = token.split(".")
    if len(parts) != 2:
        return False, "malformed"
    payload_b64, sig_b64 = parts
    try:
        payload = _b64url_decode(payload_b64)
        sig = _b64url_decode(sig_b64)
    except Exception:
        return False, "malformed"
    expected_sig = hmac.new(_signing_key(expected), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        return False, "bad_signature"
    try:
        ts = int(payload.decode("ascii"))
    except Exception:
        return False, "malformed"
    if (time.time() - ts) > TOKEN_TTL_SECONDS:
        return False, "expired"
    return True, "ok"


# ----------------------------------------------------------------------
# FastAPI dependency
# ----------------------------------------------------------------------

def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency: require a valid bearer token in Authorization header.

    Usage:
        @app.post("/api/mode", dependencies=[Depends(require_auth)])
        def set_mode(...): ...

    Raises HTTP 401 on failure with a reason in the body.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Expect "Bearer <token>"; tolerate whitespace.
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="expected 'Bearer <token>' in Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = parts[1].strip()
    ok, reason = verify_token(token)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"auth failed: {reason}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_auth_query_token(token: Optional[str] = None) -> None:
    """FastAPI dependency for WebSocket auth via `?token=...` query param.

    WebSockets can't easily set Authorization headers from the browser
    EventSource API, so the typical pattern is to pass the token in
    the URL. This dependency validates it.
    """
    ok, reason = verify_token(token or "")
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"auth failed: {reason}",
        )


# ----------------------------------------------------------------------
# Login rate limiting (Sprint 46N — audit A9)
# ----------------------------------------------------------------------
#
# Before this fix, POST /api/auth/login had no throttling at all — an
# attacker (or a misbehaving script) could send unlimited password
# guesses with zero backoff or lockout. This is a small in-memory
# per-key (by default, per client IP) failure tracker: after
# `max_attempts` failures within `window_seconds`, the key is locked
# out for `lockout_seconds`. Deliberately NOT persisted to disk (a
# restart resetting the counters is an acceptable tradeoff for a
# single-process bot — the alternative, disk-persisted lockout state,
# adds complexity for a threat model where the attacker can't force
# bot restarts anyway) and deliberately in-process/thread-safe (the
# dashboard API runs in one process, in a background thread inside
# the bot — see main.py's `_start_api_server`).

class LoginRateLimiter:
    """Naive in-memory brute-force throttle for POST /api/auth/login.

    `check(key)` — call BEFORE attempting the login. Returns
    `(allowed, retry_after_seconds)`.
    `record_failure(key)` — call after a failed login attempt.
    `record_success(key)` — call after a successful login; clears the
    key's history so a legitimate user who mistyped their password a
    couple of times isn't penalized once they get it right.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 15 * 60,
        lockout_seconds: float = 15 * 60,
    ):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._lock = threading.Lock()
        self._state: Dict[str, Dict] = {}

    def check(self, key: str) -> Tuple[bool, float]:
        now = time.time()
        with self._lock:
            entry = self._state.get(key)
            if not entry:
                return True, 0.0
            locked_until = entry.get("locked_until")
            if locked_until and now < locked_until:
                return False, locked_until - now
            return True, 0.0

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            entry = self._state.setdefault(key, {"failures": [], "locked_until": None})
            # Drop failures outside the sliding window before counting.
            entry["failures"] = [t for t in entry["failures"] if now - t < self.window_seconds]
            entry["failures"].append(now)
            if len(entry["failures"]) >= self.max_attempts:
                entry["locked_until"] = now + self.lockout_seconds
                entry["failures"] = []

    def record_success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)

    def reset(self) -> None:
        """Test-only helper: clear all tracked state."""
        with self._lock:
            self._state.clear()


# Module-level singleton — one shared limiter for the whole process,
# matching the module-level TOKEN_TTL_SECONDS convention above.
login_rate_limiter = LoginRateLimiter(
    max_attempts=int(os.getenv("DASHBOARD_LOGIN_MAX_ATTEMPTS", "5")),
    window_seconds=float(os.getenv("DASHBOARD_LOGIN_WINDOW_SECONDS", str(15 * 60))),
    lockout_seconds=float(os.getenv("DASHBOARD_LOGIN_LOCKOUT_SECONDS", str(15 * 60))),
)
