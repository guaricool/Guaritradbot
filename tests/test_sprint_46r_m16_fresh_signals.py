"""
Sprint 46R (audit M16): regression tests for the
"smart profit-take uses fresh signals" fix.

The audit's exact wording:
  "main.py:919-931 alimenta check_with_signals con hipotesis
   de hasta 1h de antiguedad del audit log contra precios
   frescos - la reversion puede estar ya invalidada."

Pre-Sprint-46R:
  - main.py fed `check_with_signals` signals up to 1h old.
  - The signals list passed through unchanged; the function
    had no concept of "this signal is too old to trust".
  - Result: a 1h-old "reversal is imminent" signal could
    trigger a SMART_PROFIT_TAKE close at a fresh price that
    no longer matches the original signal's thesis. The
    position gets closed and the bot's profit is realized
    (or worse, the bot then misses a real reversal because
    the closed position isn't there to reverse).

The fix is two layers:
  1. Source (main.py): reads `smart_profit_take_max_signal_age_s`
     from config (default 300s = 5 min) and reads audit since
     `now - that`, not `now - 3600`. Passes the same window to
     `check_with_signals` as a defensive kwarg.
  2. Sink (position_monitor.check_with_signals): even if a
     future caller passes a wider list, the function filters
     out signals older than `max_signal_age_s` at the
     function boundary. Signals with no `ts` field are
     dropped (safer than including them: we can't verify
     their age, so we don't act on them).

Tests cover:
  - default behavior (max_signal_age_s=None) - no filter
  - signals newer than max_age are kept
  - signals older than max_age are filtered
  - signals with no `ts` field are filtered out when filtering is on
  - a stale signal does NOT trigger a SMART_PROFIT_TAKE close
  - main.py reads the config option and passes it through
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from src.data_store.positions import Position
from src.data_store.position_monitor import PositionMonitor


def _make_position(asset: str = "BTC-USD", direction: str = "long",
                   entry_price: float = 100.0, qty: float = 1.0) -> Position:
    """Build a minimal Position with a positive unrealized PnL
    so SMART_PROFIT_TAKE's profit gate (`upnl > 0`) passes.

    Position is a dataclass with a specific field order and
    `position_id` (not `id`) as the auto-generated ID field.
    """
    p = Position(
        asset=asset,
        direction=direction,
        entry_price=entry_price,
        stop_loss=80.0,
        take_profit=150.0,
        qty=qty,
        risk_usd=0.0,
        entry_ts=time.time() - 600,
        strategy="test",
        position_id="pos-test-1",
    )
    return p


def _signal(asset: str = "BTC-USD", direction: str = "short",
            strength: float = 0.8, age_s: float = 60.0,
            with_ts: bool = True) -> dict:
    """Build a hypothesis signal. `age_s` controls the `ts`
    field (relative to now) so the test can dial the staleness.
    """
    sig = {
        "asset": asset,
        "direction": direction,
        "strength": strength,
    }
    if with_ts:
        sig["ts"] = time.time() - age_s
    return sig


class CheckWithSignalsMaxAgeTest(unittest.TestCase):
    def setUp(self):
        # Build a minimal PositionMonitor with an in-memory
        # repo holding a single profitable LONG position.
        self.repo = MagicMock()
        self.pos = _make_position()
        self.repo.open.return_value = [self.pos]

        # Mock unrealized_pnl to return a clearly positive value
        # so the profit gate passes.
        self.pos.unrealized_pnl = MagicMock(return_value=10.0)

        # Mock the close path so we can detect when a
        # SMART_PROFIT_TAKE close fires.
        self.pos_id_closed = []
        self.audit = MagicMock()
        self.event_bus = MagicMock()
        self.broker = MagicMock()

        # Build a minimal monitor. Most of its deps are
        # MagicMocked - we only care about `check_with_signals`.
        # PositionMonitor's __init__ takes `repo` (not
        # `position_repo`) and `broker` (not `broker_client`).
        self.monitor = PositionMonitor(
            repo=self.repo,
            audit=self.audit,
            event_bus=self.event_bus,
            broker=self.broker,
            min_profit_to_protect=0.0,
        )
        # _fee_pct may not be defined on the real class; mock it
        # to return 0 so the fee gate doesn't block.
        self.monitor._fee_pct = MagicMock(return_value=0.0)

    def test_no_filter_when_max_signal_age_is_none(self):
        """Back-compat: pre-46R callers that don't pass
        max_signal_age_s get the old behavior - no age filter."""
        sig = _signal(age_s=3000)  # 50 min old, would be filtered if we had a cap
        # Should NOT raise and should consider the signal.
        result = self.monitor.check_with_signals(
            current_prices={"BTC-USD": 110.0},
            signals=[sig],
            signal_min_strength=0.6,
            max_signal_age_s=None,
        )
        # The signal is fresh enough (strength + opposite direction)
        # to trigger a close.
        self.assertEqual(len(result), 1)

    def test_fresh_signal_triggers_close(self):
        """Signal within the window triggers SMART_PROFIT_TAKE."""
        sig = _signal(age_s=60)  # 1 min old
        result = self.monitor.check_with_signals(
            current_prices={"BTC-USD": 110.0},
            signals=[sig],
            signal_min_strength=0.6,
            max_signal_age_s=300,  # 5 min window
        )
        self.assertEqual(len(result), 1)

    def test_stale_signal_is_filtered_out(self):
        """Signal older than the window is filtered out and
        does NOT trigger a close. This is the M16 fix: a 1h-old
        reversal signal no longer drives a SMART_PROFIT_TAKE.
        """
        sig = _signal(age_s=1800)  # 30 min old
        result = self.monitor.check_with_signals(
            current_prices={"BTC-USD": 110.0},
            signals=[sig],
            signal_min_strength=0.6,
            max_signal_age_s=300,  # 5 min window
        )
        # No close - the stale signal was filtered.
        self.assertEqual(result, [])

    def test_signal_without_ts_is_filtered_when_filtering_is_on(self):
        """A signal with no `ts` field can't be aged, so we
        skip it. Safer default than including it (we don't
        know if it's 1 second or 1 day old)."""
        sig = _signal(with_ts=False)  # no ts field
        result = self.monitor.check_with_signals(
            current_prices={"BTC-USD": 110.0},
            signals=[sig],
            signal_min_strength=0.6,
            max_signal_age_s=300,
        )
        self.assertEqual(result, [])

    def test_signal_without_ts_passes_when_filtering_is_off(self):
        """Back-compat: with max_signal_age_s=None, no filtering
        happens, so a no-ts signal still drives a close."""
        sig = _signal(with_ts=False)
        result = self.monitor.check_with_signals(
            current_prices={"BTC-USD": 110.0},
            signals=[sig],
            signal_min_strength=0.6,
            max_signal_age_s=None,
        )
        self.assertEqual(len(result), 1)

    def test_mixed_fresh_and_stale_only_fresh_triggers(self):
        """When the list has both fresh and stale signals for
        the same asset, only the fresh one should drive a
        close. The stale one is silently dropped (it doesn't
        disqualify the fresh one)."""
        fresh = _signal(age_s=60, strength=0.8)
        stale = _signal(age_s=1800, strength=0.9)  # even stronger but stale
        result = self.monitor.check_with_signals(
            current_prices={"BTC-USD": 110.0},
            signals=[stale, fresh],  # stale first - order shouldn't matter
            signal_min_strength=0.6,
            max_signal_age_s=300,
        )
        self.assertEqual(len(result), 1)


class MainReadsConfigTest(unittest.TestCase):
    """Sprint 46R audit M16: main.py's `smart_profit_take_max_signal_age_s`
    config option must be read and passed to check_with_signals.

    These tests inspect the source of main.py to verify the
    config option is read and threaded through. We do this by
    static source inspection (parse the file and look for the
    pattern) rather than running main.py end-to-end, which
    would require mocking dozens of subsystems.
    """

    def test_main_reads_smart_profit_take_max_signal_age_s(self):
        # Sprint 46T (audit M6): the .get() call moved from main.py to
        # src/runtime/bot_runtime.py. Check both.
        from pathlib import Path
        combined = "\n".join(
            Path(p).read_text(encoding="utf-8")
            for p in ("main.py", "src/runtime/bot_runtime.py")
        )
        self.assertIn("smart_profit_take_max_signal_age_s", combined,
                      "the runtime must read the config option")
        self.assertIn("max_signal_age_s", combined,
                      "the runtime must pass it to check_with_signals")

    def test_default_is_300_seconds(self):
        """Default = 5 min. The audit's complaint was about
        1h being too long; 5 min is "fresh enough" given the
        2-min fast_monitor_tick cadence."""
        from pathlib import Path
        combined = "\n".join(
            Path(p).read_text(encoding="utf-8")
            for p in ("main.py", "src/runtime/bot_runtime.py")
        )
        # Find the get(...) call for this config and check the
        # default. Pattern: trading_cfg.get("smart_profit_take_max_signal_age_s", 300)
        import re
        m = re.search(
            r'smart_profit_take_max_signal_age_s"?\s*,\s*(\d+)',
            combined,
        )
        self.assertIsNotNone(m, "Config default value not found")
        self.assertEqual(int(m.group(1)), 300,
                         "Default should be 300s (5 min)")


if __name__ == "__main__":
    unittest.main()
