"""
Sprint 54 — MarketAnalystAgent FAULTED resilience.

The pre-54 bug (reported 2026-07-13 on Telegram): yfinance briefly
went sideways around the 15m window, all 9 feeds (3 assets × 3
timeframes) returned None, the agent self-faulted via
`self.fault("all N feeds failed")`, and then every subsequent
30-min cycle aborted with "Agent 'MarketAnalystAgent' is in
state 'FAULTED'". The state machine has no FAULTED → * path
(`recover()` only handles DEGRADED → RUNNING; `start()` refuses
non-READY), so the agent was stuck in FAULTED until the bot
restarted. Telegram got ~10 identical error pings before Carlos
paused the bot manually.

Sprint 54 fix has three pieces, each tested below:
  1. AUTO-RECOVER  — at the top of each cycle, leave FAULTED
     behind (transition to RUNNING) so the next try can succeed.
  2. RETRY ONCE    — on a total-failure cycle, if we haven't
     retried in the last 5 min, sleep 30s and recurse. Catches
     transient rate-limits.
  3. ALERT DEDUP   — when a retry ALSO fails, emit at most one
     SYSTEM_ERROR per 30 min, not one per 30-min cycle.
"""
import sys
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd


def _make_ohlcv_df(n: int = 100, freq: str = "1h") -> pd.DataFrame:
    """A minimal but valid OHLCV DataFrame that survives
    `_validate_or_fault` (which calls `validate_dataframe` and
    checks staleness against the current clock)."""
    idx = pd.date_range(pd.Timestamp.now() - pd.Timedelta(hours=n),
                        periods=n, freq=freq)
    return pd.DataFrame({
        "Open":   [100.0 + 0.1 * i for i in range(n)],
        "High":   [101.0 + 0.1 * i for i in range(n)],
        "Low":    [ 99.0 + 0.1 * i for i in range(n)],
        "Close":  [100.0 + 0.1 * i for i in range(n)],
        "Volume": [1000.0] * n,
    }, index=idx)


class AutoRecoverTest(unittest.TestCase):
    """Sprint 54 #1: auto-recover from FAULTED/DEGRADED at cycle start."""

    def test_faulted_state_auto_recovers_to_running(self):
        """A FAULTED MarketAnalyst must leave FAULTED at the start of
        the next cycle. Pre-54, the agent stayed in FAULTED forever
        and every cycle aborted in the engine's _check_agent_state."""
        from src.agents import market_analyst as ma_mod
        from src.core.component import ComponentState

        ma = ma_mod.MarketAnalystAgent()
        # Simulate the post-bug state: a previous cycle ran
        # self.fault(...). The component should NOT be able to leave
        # FAULTED via start() or recover() alone.
        ma.fault("all 9 feeds failed (simulated prior outage)")
        self.assertEqual(ma.state, ComponentState.FAULTED)
        self.assertFalse(ma.start())  # start() refuses non-READY
        # recover() only handles DEGRADED → RUNNING
        ma.recover()
        self.assertEqual(ma.state, ComponentState.FAULTED,
                         "recover() should not touch FAULTED — that's the pre-54 bug")

        # Now run a fresh cycle with a working feed. It should
        # auto-recover from FAULTED at the top.
        with patch.object(ma_mod, "safe_yf_download", return_value=_make_ohlcv_df()):
            result = ma.fetch_and_analyze(
                inputs={"assets": ["BTC-USD"], "timeframes": ["1h"]},
                state={},
            )
        # After a successful cycle, state must be RUNNING (the
        # cycle's "else: self.recover()" at the bottom) — not
        # FAULTED. This is the contract: a successful fetch
        # proves the upstream is back, so FAULTED is no longer
        # accurate.
        self.assertIn(ma.state, (ComponentState.RUNNING, ComponentState.DEGRADED))
        self.assertNotEqual(ma.state, ComponentState.FAULTED)
        self.assertIn("market_data", result)

    def test_degraded_state_clears_to_running_on_success(self):
        """A DEGRADED MarketAnalyst (e.g. 1 of 9 feeds failed) should
        clear back to RUNNING on a fully successful cycle, not just
        sit in DEGRADED forever."""
        from src.agents import market_analyst as ma_mod
        from src.core.component import ComponentState

        ma = ma_mod.MarketAnalystAgent()
        # `degrade()` is a no-op unless the agent is RUNNING. Move
        # it through the state machine first.
        ma.ready()
        ma.start()
        ma.degrade("1 feed failed but workflow continues")
        self.assertEqual(ma.state, ComponentState.DEGRADED)

        with patch.object(ma_mod, "safe_yf_download", return_value=_make_ohlcv_df()):
            ma.fetch_and_analyze(
                inputs={"assets": ["BTC-USD"], "timeframes": ["1h"]},
                state={},
            )
        self.assertEqual(ma.state, ComponentState.RUNNING)


class TotalFailureRetryTest(unittest.TestCase):
    """Sprint 54 #2: retry once on total failure, with cooldown."""

    def test_total_failure_triggers_one_retry_then_succeeds(self):
        """First attempt: all feeds return None. Agent should sleep
        30s and recurse; on the recursive call, the (mocked) feed
        works, so the cycle ends in RUNNING with real data — and
        the 30s sleep is a no-op via patch."""
        from src.agents import market_analyst as ma_mod
        from src.core.component import ComponentState

        ma = ma_mod.MarketAnalystAgent()
        ma._last_total_failure_retry_at = 0.0  # first attempt ever

        # First call: feed returns None (total failure).
        # Second call (after retry): feed returns a real df.
        call_count = {"n": 0}
        def flaky_feed(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return None
            return _make_ohlcv_df()

        with patch.object(ma_mod, "safe_yf_download", side_effect=flaky_feed), \
             patch.object(ma_mod, "_time") as mock_time:
            mock_time.time.return_value = 1_000_000.0
            mock_time.sleep.return_value = None
            result = ma.fetch_and_analyze(
                inputs={"assets": ["BTC-USD"], "timeframes": ["1h"]},
                state={},
            )

        # We expected the retry to fire exactly once (call_count == 2)
        self.assertEqual(call_count["n"], 2,
                         "expected 1 retry after the initial failure")
        # And we should have slept 30s exactly once
        mock_time.sleep.assert_called_once_with(30)
        # And the agent should be RUNNING (recovery at the bottom
        # of the successful recursive call)
        self.assertIn(ma.state, (ComponentState.RUNNING, ComponentState.DEGRADED))
        self.assertIn("market_data", result)

    def test_total_failure_retry_exhausted_goes_to_faulted(self):
        """If the retry ALSO fails, the agent goes to FAULTED — and
        the retry timestamp is recorded so the NEXT cycle's
        retry is gated by the 5-min cooldown."""
        from src.agents import market_analyst as ma_mod
        from src.core.component import ComponentState

        ma = ma_mod.MarketAnalystAgent()
        ma._last_total_failure_retry_at = 0.0

        bus = MagicMock()
        ma.event_bus = bus

        with patch.object(ma_mod, "safe_yf_download", return_value=None), \
             patch.object(ma_mod, "_time") as mock_time:
            mock_time.time.return_value = 1_000_000.0
            mock_time.sleep.return_value = None
            result = ma.fetch_and_analyze(
                inputs={"assets": ["BTC-USD"], "timeframes": ["1h"]},
                state={},
            )

        # 2 calls to safe_yf_download (initial + retry), then FAULTED
        self.assertEqual(ma.state, ComponentState.FAULTED)
        # Exactly ONE SYSTEM_ERROR (not 0 = no alert; not 2 = dedup
        # broken; and we have to filter the trailing
        # MARKET_DATA_READY emit that fetch_and_analyze always
        # publishes at the end of the method, even on fault).
        system_error_calls = [
            c for c in bus.publish.call_args_list
            if c[0][0] == "SYSTEM_ERROR"
        ]
        self.assertEqual(len(system_error_calls), 1,
                         f"expected 1 SYSTEM_ERROR, got {len(system_error_calls)}: "
                         f"{bus.publish.call_args_list}")
        self.assertEqual(system_error_calls[0][0][1]["kind"],
                         "MARKET_DATA_TOTAL_FAILURE")
        # Retry timestamp recorded → next cycle within 5 min will
        # NOT re-retry (cooldown)
        self.assertGreater(ma._last_total_failure_retry_at, 0.0)

    def test_retry_cooldown_blocks_immediate_retry_in_next_cycle(self):
        """A fresh cycle started 2 minutes after a faulted cycle
        should NOT retry again — the cooldown is 5 min. This
        prevents burning a recursive call + 30s sleep on every
        30-min cycle during a sustained outage."""
        from src.agents import market_analyst as ma_mod
        from src.core.component import ComponentState

        ma = ma_mod.MarketAnalystAgent()
        # Pretend a previous cycle already retried 2 min ago
        ma._last_total_failure_retry_at = 1_000_000.0

        bus = MagicMock()
        ma.event_bus = bus

        call_count = {"n": 0}
        def always_fail(*args, **kwargs):
            call_count["n"] += 1
            return None

        with patch.object(ma_mod, "safe_yf_download", side_effect=always_fail), \
             patch.object(ma_mod, "_time") as mock_time:
            # Cycle happens 2 min (120s) after the last retry — well
            # inside the 5-min cooldown.
            mock_time.time.return_value = 1_000_000.0 + 120.0
            mock_time.sleep.return_value = None
            ma.fetch_and_analyze(
                inputs={"assets": ["BTC-USD"], "timeframes": ["1h"]},
                state={},
            )

        # No retry happened (cooldown blocked it) — exactly 1 call
        self.assertEqual(call_count["n"], 1,
                         "retry should be blocked by 5-min cooldown")
        # sleep should NOT have been called (no retry → no wait)
        mock_time.sleep.assert_not_called()
        # Agent went to FAULTED
        self.assertEqual(ma.state, ComponentState.FAULTED)


class AlertDedupTest(unittest.TestCase):
    """Sprint 54 #3: SYSTEM_ERROR dedup — 1 alert per 30 min, not 1 per cycle."""

    def test_alert_suppressed_within_30min_window(self):
        """Two consecutive faulted cycles within the dedup window
        should only fire ONE SYSTEM_ERROR. The second cycle's
        recovery (auto-recover at top + retry cooldown) means the
        agent cycles FAULTED→RUNNING→FAULTED, but the alert is
        suppressed on the second fault."""
        from src.agents import market_analyst as ma_mod
        from src.core.component import ComponentState

        ma = ma_mod.MarketAnalystAgent()
        bus = MagicMock()
        ma.event_bus = bus

        # Simulate: cycle 1 faulted (alert emitted, alert ts = T0).
        ma._last_total_failure_alert_at = 1_000_000.0
        # And the retry was also attempted 10s ago, so the next
        # cycle's retry is blocked by the 5-min cooldown → the
        # cycle will go straight to fault+alert.
        ma._last_total_failure_retry_at = 1_000_000.0

        with patch.object(ma_mod, "safe_yf_download", return_value=None), \
             patch.object(ma_mod, "_time") as mock_time:
            # 60s after the previous alert — well within the
            # 30-min dedup window.
            mock_time.time.return_value = 1_000_000.0 + 60.0
            mock_time.sleep.return_value = None
            ma.fetch_and_analyze(
                inputs={"assets": ["BTC-USD"], "timeframes": ["1h"]},
                state={},
            )

        # Faulted but NO new alert (the dedup branch ran, not the
        # publish branch). Filter out the trailing MARKET_DATA_READY
        # that the method always emits.
        self.assertEqual(ma.state, ComponentState.FAULTED)
        system_error_calls = [
            c for c in bus.publish.call_args_list
            if c[0][0] == "SYSTEM_ERROR"
        ]
        self.assertEqual(len(system_error_calls), 0,
                         f"alert should be suppressed; got {system_error_calls}")

    def test_alert_fires_after_30min_window_elapses(self):
        """When the 30-min window has elapsed, a fresh faulted cycle
        should fire a NEW SYSTEM_ERROR — operator gets one ping per
        outage, then silence, then a fresh ping if the outage
        extends another 30 min."""
        from src.agents import market_analyst as ma_mod
        from src.core.component import ComponentState

        ma = ma_mod.MarketAnalystAgent()
        bus = MagicMock()
        ma.event_bus = bus

        # Previous alert was 31 min ago (> dedup window).
        ma._last_total_failure_alert_at = 1_000_000.0
        # Retry was also 6 min ago (> 5-min cooldown), so the cycle
        # will retry once → still fails → fault → fresh alert.
        ma._last_total_failure_retry_at = 1_000_000.0

        with patch.object(ma_mod, "safe_yf_download", return_value=None), \
             patch.object(ma_mod, "_time") as mock_time:
            # 31 min (1860s) after the previous alert
            mock_time.time.return_value = 1_000_000.0 + 31 * 60.0
            mock_time.sleep.return_value = None
            ma.fetch_and_analyze(
                inputs={"assets": ["BTC-USD"], "timeframes": ["1h"]},
                state={},
            )

        # Faulted + 1 fresh alert
        self.assertEqual(ma.state, ComponentState.FAULTED)
        system_error_calls = [
            c for c in bus.publish.call_args_list
            if c[0][0] == "SYSTEM_ERROR"
        ]
        self.assertEqual(len(system_error_calls), 1)
        self.assertEqual(system_error_calls[0][0][1]["kind"],
                         "MARKET_DATA_TOTAL_FAILURE")

    def test_successful_fetch_resets_cooldowns(self):
        """A successful fetch must clear the dedup/retry timestamps,
        otherwise a long-ago outage would permanently silence the
        next real one (or permanently block its retry)."""
        from src.agents import market_analyst as ma_mod

        ma = ma_mod.MarketAnalystAgent()
        # Pretend we had a recent outage
        ma._last_total_failure_alert_at = 1_000_000.0
        ma._last_total_failure_retry_at = 1_000_000.0

        with patch.object(ma_mod, "safe_yf_download", return_value=_make_ohlcv_df()), \
             patch.object(ma_mod, "_time") as mock_time:
            mock_time.time.return_value = 1_000_000.0
            ma.fetch_and_analyze(
                inputs={"assets": ["BTC-USD"], "timeframes": ["1h"]},
                state={},
            )

        self.assertEqual(ma._last_total_failure_alert_at, 0.0)
        self.assertEqual(ma._last_total_failure_retry_at, 0.0)


if __name__ == "__main__":
    unittest.main()
