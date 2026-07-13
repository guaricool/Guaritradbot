"""
Sprint 46E — Startup self-tests for safety-critical checks.

Why this exists: the drawdown kill switch (Sprint 43 H3) was silently
dead for its entire lifetime — a scoping bug in main.py's
`job_with_monitor()` made every single call raise `UnboundLocalError`,
caught by a bare `except Exception: print(...)` that only logged to
stdout. Nothing about the bot's behavior looked different: it kept
trading, kept printing normal-looking cycle logs, and the ONE safety
net meant to catch "you've lost too much, stop opening new positions"
never actually ran. That bug went unnoticed through several audit
passes because nobody was checking "does this safety mechanism still
fire", only "is the code that implements it present".

This module runs a handful of fast, synthetic-data self-tests against
the actual safety classes at bot startup, BEFORE the main trading loop
begins. If a self-test raises or produces a wrong answer, that's
reported LOUDLY (print banner + audit event + SYSTEM_ERROR publish),
but does NOT block the bot from starting — consistent with this
codebase's existing philosophy for every other best-effort safety
check (portfolio-risk gates, correlation/stress/CVaR checks: a broken
CHECK should never itself become a reason to halt trading). The point
is visibility: if a self-test fails, Carlos finds out immediately
instead of discovering months later that a safety net was never
strung up.

Uses a THROWAWAY instance of each safety class (never the real one
that's tracking live equity) so the test can't corrupt production
state.
"""
from __future__ import annotations

import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.safety.kelly_drawdown import DrawdownKillSwitch

from src.core.logging_setup import get_logger
logger = get_logger(__name__)


def _test_drawdown_triggers_on_breach() -> Tuple[bool, str]:
    """A drawdown past the threshold MUST trigger the kill switch."""
    dd = DrawdownKillSwitch(threshold_pct=10.0, cooldown_hours=1.0)
    dd.update(1000.0)          # establishes peak = 1000
    state = dd.update(880.0)   # -12% — past the 10% threshold
    if not state.triggered:
        return False, f"expected triggered=True at -12% dd (threshold 10%), got {state.triggered}"
    return True, "ok"


def _test_drawdown_does_not_trigger_below_threshold() -> Tuple[bool, str]:
    """A drawdown UNDER the threshold must NOT trigger — otherwise the
    kill switch would be pausing the bot on completely normal
    volatility, which is its own kind of failure (false positives
    erode trust and get "temporarily" disabled by a frustrated user)."""
    dd = DrawdownKillSwitch(threshold_pct=10.0, cooldown_hours=1.0)
    dd.update(1000.0)
    state = dd.update(950.0)   # -5% — under the 10% threshold
    if state.triggered:
        return False, f"expected triggered=False at -5% dd (threshold 10%), got {state.triggered}"
    return True, "ok"


def _test_drawdown_handles_no_open_positions() -> Tuple[bool, str]:
    """The exact shape main.py's job_with_monitor() feeds in on every
    cycle: `position_repo.total_realized_pnl_usd() + sum(unrealized
    for open positions)`, which is just a plain float — must not raise
    regardless of position count (0 open positions => equity is just
    realized PnL, a valid float, possibly negative)."""
    dd = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
    for equity in (0.0, -5.0, 10.0, 0.0):
        dd.update(equity)
    return True, "ok"


# Registry of (name, test_fn). Add new safety-critical self-tests here
# as they're built — each just needs to return (passed: bool, detail: str)
# and never raise on its own (exceptions are caught by the runner below).
_SELFTESTS: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
    ("drawdown_kill_switch_triggers_on_breach", _test_drawdown_triggers_on_breach),
    ("drawdown_kill_switch_ignores_normal_volatility", _test_drawdown_does_not_trigger_below_threshold),
    ("drawdown_kill_switch_handles_empty_book", _test_drawdown_handles_no_open_positions),
]


def run_startup_selftests(
    audit: Optional[Any] = None,
    event_bus: Optional[Any] = None,
) -> bool:
    """Run all registered self-tests. Returns True iff every test passed.

    Best-effort by design: logs failures loudly (print + audit event +
    SYSTEM_ERROR) but never raises — a bug in the self-test harness
    itself must not be able to crash bot startup.
    """
    results: Dict[str, Dict[str, Any]] = {}
    all_ok = True
    for name, fn in _SELFTESTS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok = False
            detail = f"self-test raised {type(e).__name__}: {e}"
            traceback.print_exc()
        results[name] = {"ok": ok, "detail": detail}
        if not ok:
            all_ok = False

    if all_ok:
        logger.info(f'[SelfTest] ✅ {len(_SELFTESTS)}/{len(_SELFTESTS)} startup self-tests passed.')
    else:
        failed = [n for n, r in results.items() if not r["ok"]]
        logger.warning(f"\n{'=' * 70}\n⚠️  STARTUP SELF-TEST FAILURE ⚠️\n{len(failed)}/{len(_SELFTESTS)} safety self-test(s) FAILED: {failed}\nThe bot will continue starting (best-effort — a broken TEST is not\nitself a reason to halt trading), but this means a safety mechanism\nmay not be working correctly. Check the details below and fix before\ntrusting the affected safety net.\n{'=' * 70}\n")
        for n in failed:
            logger.error(f"  ❌ {n}: {results[n]['detail']}")
        if audit is not None:
            try:
                audit.append("STARTUP_SELFTEST_FAILED", {
                    "failed": failed,
                    "details": {n: results[n]["detail"] for n in failed},
                })
            except Exception:
                pass
        if event_bus is not None:
            try:
                event_bus.publish("SYSTEM_ERROR", {
                    "kind": "STARTUP_SELFTEST_FAILED",
                    "error": (
                        f"⚠️ {len(failed)} startup self-test(s) failed: {failed}. "
                        f"A safety mechanism (drawdown kill switch) may not be working. "
                        f"Bot is still running — please investigate."
                    ),
                })
            except Exception:
                pass
    return all_ok
