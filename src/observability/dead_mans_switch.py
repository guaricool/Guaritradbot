"""
Sprint 46R (audit M11.4): dead-man's switch ping.

The audit's exact wording:
  "Considerar un dead-man's switch (ping a healthchecks.io por ciclo)."

Translation: the bot should signal liveness to an OUT-OF-BAND
service every cycle. If the signal stops arriving (the bot
process is dead, the host is down, the network is partitioned),
the OOB service sends a separate alert that doesn't depend on
the bot's own Telegram channel. This is critical because
the audit's M11.2 finding is that Telegram is the bot's
SINGLE alert channel — if Telegram goes down, the entire
alert pipeline goes down with it. A dead-man's switch
restores a second channel.

Design:
  - HEALTHCHECKS_PING_URL env var. Empty = disabled (the default
    in tests and local dev). Set it to a healthchecks.io URL
    in production to enable.
  - The ping is a single GET with a 5s timeout. The
    service treats 2xx as "alive" and any other status (or
    timeout / connection error) as "missed ping", so we
    don't need to handle the response body.
  - We DO NOT crash the cycle on a ping failure — a healthchecks.io
    outage is exactly when the bot is operating normally and
    the OOB check is wrong. Best-effort only.
  - Module-level `_last_ping_at` and `_last_ping_error` so
    /api/health can include the most recent ping status in
    its body (Sprint 46R M11.3 follow-up — bonus observability
    that came naturally from this implementation).

Tested via `tests/test_sprint_46r_m11_4_dead_mans_switch.py`.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional, Tuple

import requests

# Default timeout — short on purpose. healthchecks.io expects a
# ping to return within seconds; if it doesn't, something is
# very wrong with the OOB service.
PING_TIMEOUT_S = 5.0

# Thread-local state for the last ping result. Used by /api/health
# to surface the OOB status in its response body. A lock is needed
# because the ping is called from the bot's main thread (job_with_monitor)
# AND the fast_monitor thread (fast_monitor_tick), and /api/health
# reads from the FastAPI request thread.
_state_lock = threading.Lock()
_last_ping_at: float = 0.0
_last_ping_ok: bool = True
_last_ping_error: Optional[str] = None
_last_ping_url: Optional[str] = None

logger = logging.getLogger("DeadMansSwitch")


def get_ping_state() -> dict:
    """Snapshot of the last dead-man's-switch ping (thread-safe).

    Returns dict with: url, last_at, last_ok, last_error.
    Consumed by /api/health to surface the OOB status.
    """
    with _state_lock:
        return {
            "url": _last_ping_url,
            "last_at": _last_ping_at or None,
            "last_ok": _last_ping_ok,
            "last_error": _last_ping_error,
        }


def ping_dead_mans_switch(
    url: Optional[str] = None,
    timeout_s: float = PING_TIMEOUT_S,
) -> Tuple[bool, Optional[str]]:
    """Best-effort GET against the configured healthchecks.io URL.

    Returns (ok, error_message). ok=True iff the server returned
    2xx within timeout_s. error_message is the failure reason on
    ok=False, None otherwise.

    Side effects: updates the module-level _last_ping_at / _last_ping_ok
    / _last_ping_error for /api/health to read.
    """
    # Single `global` declaration at the top of the function: Python
    # forbids declaring `global` for a name AFTER it's been assigned
    # to anywhere in the function body, so the disabled-branch + the
    # active-branch both need to share one declaration.
    global _last_ping_at, _last_ping_ok, _last_ping_error, _last_ping_url

    target = url if url is not None else os.getenv("HEALTHCHECKS_PING_URL", "")
    if not target:
        # No URL configured = feature is disabled. Update state with
        # "skipped" so /api/health can show "disabled" rather than
        # "stale".
        with _state_lock:
            _last_ping_url = None
            # Don't touch _last_ping_at/_last_ping_ok — keep the prior
            # values so the dashboard can still see the last actual
            # ping result. Just clear the error.
            _last_ping_error = "disabled (HEALTHCHECKS_PING_URL not set)"
        return True, None  # Disabled is not an error.

    err: Optional[str] = None
    ok = False
    try:
        resp = requests.get(target, timeout=timeout_s)
        if 200 <= resp.status_code < 300:
            ok = True
        else:
            err = f"HTTP {resp.status_code}"
    except requests.exceptions.Timeout:
        err = f"timeout after {timeout_s}s"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    with _state_lock:
        _last_ping_at = time.time()
        _last_ping_ok = ok
        _last_ping_error = err
        _last_ping_url = target

    if ok:
        logger.debug("Dead-man's switch ping OK (%s)", target)
    else:
        logger.warning("Dead-man's switch ping failed (%s): %s", target, err)
    return ok, err
