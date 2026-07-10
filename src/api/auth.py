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
import time
from typing import Optional, Tuple

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
    if password is None or password != expected:
        raise PermissionError("invalid password")
    ts = int(time.time())
    payload = str(ts).encode("ascii")
    sig = hmac.new(expected.encode("utf-8"), payload, hashlib.sha256).digest()
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
    expected_sig = hmac.new(expected.encode("utf-8"), payload, hashlib.sha256).digest()
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
