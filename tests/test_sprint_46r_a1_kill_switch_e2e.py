"""
Sprint 46R (audit A1 verification) — DrawdownKillSwitch end-to-end
test.

Sprint 46N (commit 7c33bf7) shipped the kill-switch fix that
the audit's A1 finding asked for:
  - drawdown source from `equity_tracker.latest().total_equity`,
    not from `prices.get(pos.asset, 0.0)` (which used a missing
    price as a 100% loss)
  - persisted to disk so a restart doesn't silently clear an
    active kill switch
  - cooldown of 24h before auto-reset

The unit tests in tests/test_kelly_drawdown.py cover the basic
mechanics, but a more end-to-end test was missing — one that
exercises:
  1. The full state machine (normal -> trigger -> cooldown ->
     auto-reset) with the real time.time() flow
  2. The persistence across save/load (the "restart doesn't
     silently clear an active kill switch" guarantee)
  3. The "no trigger during warmup" edge case (first equity
     reading should be the peak, not trigger a 100% drawdown)
  4. The "auto-reset only when NOT in drawdown" guard
     (otherwise we'd reset and immediately re-trigger)

This file pins all four. Run: python -m unittest
tests.test_sprint_46r_a1_kill_switch_e2e -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.kelly_drawdown import DrawdownKillSwitch


class KillSwitchStateMachineTest(unittest.TestCase):
    """Walk the kill switch through its full lifecycle with a
    mocked clock so the cooldown is exercisable in milliseconds,
    not 24 hours."""

    def setUp(self):
        self.kds = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
        # A frozen clock we can advance manually so cooldown is
        # testable in ms not days.
        self.now = 1_700_000_000.0  # arbitrary epoch
        self.time_patches = [patch("time.time", side_effect=self._fake_time)]

    def _fake_time(self):
        return self.now

    def _advance(self, seconds: float):
        self.now += seconds

    def test_normal_equity_does_not_trigger(self):
        """Equity climbing from $20 to $25 should never trigger
        the kill switch (no drawdown to measure)."""
        with self.time_patches[0]:
            for v in (20.0, 22.0, 24.0, 25.0):
                state = self.kds.update(v)
                self.assertFalse(state.triggered,
                                 f"equity at ${v} with peak ${self.kds.peak_equity} should not trigger")
                self.assertGreaterEqual(state.drawdown_pct, 0.0)

    def test_small_drawdown_below_threshold_does_not_trigger(self):
        """A 5% drawdown (well below 15% threshold) should NOT
        trigger. Verifies the threshold comparison (not just
        any negative drawdown)."""
        with self.time_patches[0]:
            self.kds.update(100.0)
            state = self.kds.update(95.0)  # -5%
            self.assertFalse(state.triggered)
            self.assertAlmostEqual(state.drawdown_pct, -5.0, places=2)

    def test_drawdown_at_threshold_triggers(self):
        """A drawdown >= threshold MUST trigger. The audit's
        exact concern was that pre-Sprint-46N, the kill switch
        either didn't fire or fired at the wrong threshold. This
        test pins the boundary at -15%."""
        with self.time_patches[0]:
            self.kds.update(100.0)
            state = self.kds.update(85.0)  # exactly -15%
            self.assertTrue(state.triggered,
                            f"drawdown of {state.drawdown_pct:.2f}% should trigger at threshold -15%")
            self.assertIsNotNone(self.kds.triggered_at)

    def test_drawdown_worse_than_threshold_keeps_triggered(self):
        """Once triggered, deeper drawdowns should keep the
        kill switch active (the trigger is sticky, not
        self-clearing)."""
        with self.time_patches[0]:
            self.kds.update(100.0)
            self.kds.update(80.0)  # -20%, triggers
            self.assertTrue(self.kds.triggered)
            # Deeper drawdown: still triggered
            state = self.kds.update(70.0)  # -30%
            self.assertTrue(state.triggered)
            # And the peak should not move down (peak_equity stays
            # at the highest seen, not the latest)
            self.assertAlmostEqual(self.kds.peak_equity, 100.0, places=2)

    def test_cooldown_still_in_drawdown_rebases_peak_instead_of_deadlocking(self):
        """Bug fix (deadlock): this test used to assert the kill switch
        stays triggered forever once cooldown elapses while still in
        drawdown -- but recovering equity requires NEW trades, which
        this switch itself blocks while triggered, so that old
        contract meant the switch could NEVER release once equity fell
        far enough (no way to out-earn its own lockout). It now
        releases anyway once the cooldown genuinely elapses, rebasing
        `peak_equity` to the current equity (a fresh, reachable
        baseline) rather than requiring recovery to the old peak."""
        with self.time_patches[0]:
            self.kds.update(100.0)
            self.kds.update(80.0)  # triggers
            self._advance(self.kds.cooldown_hours * 3600 + 60)
            state = self.kds.update(80.0)  # equity unchanged
            self.assertFalse(state.triggered,
                             "cooldown elapsed -> kill switch must release even if "
                             "still nominally 'in drawdown' vs the OLD peak, by "
                             "rebasing the peak instead of deadlocking forever")
            self.assertTrue(state.peak_rebased)
            self.assertEqual(state.peak_equity, 80.0)

    def test_cooldown_with_recovery_auto_resets(self):
        """The happy-path recovery: cooldown elapsed AND equity
        has recovered above the threshold -> kill switch
        auto-clears and trading resumes."""
        with self.time_patches[0]:
            self.kds.update(100.0)
            self.kds.update(80.0)  # triggers
            self._advance(self.kds.cooldown_hours * 3600 + 60)
            state = self.kds.update(95.0)  # recovered to -5%
            self.assertFalse(state.triggered,
                             f"cooldown elapsed + equity recovered, should "
                             f"auto-reset (got drawdown={state.drawdown_pct:.2f}%)")

    def test_cooldown_partial_does_not_auto_reset(self):
        """Cooldown not yet elapsed -> no auto-reset even if
        equity has recovered. (The pre-Sprint-46N code reset on
        first recovery, which let a single good day wipe out the
        24h safety window.)"""
        with self.time_patches[0]:
            self.kds.update(100.0)
            self.kds.update(80.0)  # triggers at t=0
            self._advance(self.kds.cooldown_hours * 3600 / 2)  # 12h
            state = self.kds.update(95.0)  # equity recovers
            self.assertTrue(state.triggered,
                            f"cooldown only half-elapsed -> must NOT auto-reset "
                            f"(remaining {self.kds.cooldown_hours * 0.5:.1f}h)")

    def test_cooldown_remaining_is_computed(self):
        """The Sprint 45 fix (N2) added `cooldown_remaining_hours`
        so the dashboard can show "kill switch active, 18.5h
        remaining". Verify the calculation is non-zero when
        triggered, and 0 when not triggered."""
        with self.time_patches[0]:
            self.kds.update(100.0)
            self.kds.update(80.0)  # triggers
            self._advance(5 * 3600)  # 5h elapsed
            state = self.kds.update(80.0)
            self.assertTrue(state.triggered)
            self.assertAlmostEqual(state.cooldown_remaining_hours,
                                    24.0 - 5.0, places=2)
            # After auto-reset, remaining should be 0
            self._advance(25 * 3600)  # total 30h, past cooldown
            state = self.kds.update(95.0)  # recovery
            self.assertFalse(state.triggered)
            self.assertEqual(state.cooldown_remaining_hours, 0.0)


class KillSwitchPersistenceTest(unittest.TestCase):
    """The audit's A1 key concern: 'a restart (including POST
    /api/restart) borra silenciosamente un kill switch activo.'
    The fix (Sprint 46N) persists `peak_equity`, `triggered`,
    `triggered_at` to a JSON file. Verify the save/load roundtrip
    preserves the active state across a 'restart' (i.e. a
    new DrawdownKillSwitch instance loaded from disk)."""

    def test_active_kill_switch_survives_restart(self):
        """The single most important audit guarantee: an active
        kill switch MUST persist across a process restart. If
        this test fails, the audit's A1 finding is back."""
        tmpdir = tempfile.mkdtemp()
        state_path = os.path.join(tmpdir, "drawdown_killswitch.json")

        # 1. Pre-restart: trigger the kill switch
        pre = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
        pre.update(100.0)
        pre.update(80.0)  # -20% triggers
        self.assertTrue(pre.triggered)
        self.assertAlmostEqual(pre.peak_equity, 100.0, places=2)
        pre.persist(state_path)  # what the bot does on every update

        # 2. "Restart": a brand-new instance loaded via the
        # classmethod `load(path, threshold_pct, cooldown_hours)`.
        # The peak/triggered/triggered_at MUST come back. Note
        # that the threshold and cooldown come from the CURRENT
        # config (the function args), NOT the persisted file —
        # Sprint 46N's class-level comment makes this explicit.
        post = DrawdownKillSwitch.load(
            state_path, threshold_pct=15.0, cooldown_hours=24.0,
        )
        self.assertTrue(post.triggered,
                        "kill switch did NOT survive restart — A1 regression")
        self.assertAlmostEqual(post.peak_equity, 100.0, places=2)
        self.assertIsNotNone(post.triggered_at)

    def test_inactive_kill_switch_does_not_trigger_after_restart(self):
        """The opposite case: if the kill switch was NOT active
        when the bot stopped, the new instance must NOT spuriously
        trigger just because the saved peak is high. (Without
        this test, a `load()` bug that hardcodes `triggered=True`
        would slip through.)"""
        tmpdir = tempfile.mkdtemp()
        state_path = os.path.join(tmpdir, "drawdown_killswitch.json")

        # 1. Pre-restart: high equity, no trigger
        pre = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
        pre.update(100.0)
        pre.update(110.0)  # new peak, no drawdown
        pre.persist(state_path)
        self.assertFalse(pre.triggered)

        # 2. Restart: new instance via load(), should still be untriggered
        post = DrawdownKillSwitch.load(
            state_path, threshold_pct=15.0, cooldown_hours=24.0,
        )
        self.assertFalse(post.triggered)
        self.assertAlmostEqual(post.peak_equity, 110.0, places=2)

    def test_load_with_no_state_file_does_not_raise(self):
        """First boot (no state file) must be a clean no-op. A
        load() that raised on missing file would crash the bot
        on every cold start — the audit's concern was robustness
        on a fresh deployment."""
        tmpdir = tempfile.mkdtemp()
        state_path = os.path.join(tmpdir, "nonexistent.json")
        kds = DrawdownKillSwitch.load(
            state_path, threshold_pct=15.0, cooldown_hours=24.0,
        )
        self.assertFalse(kds.triggered)
        self.assertEqual(kds.peak_equity, 0.0)

    def test_threshold_and_cooldown_come_from_caller_not_file(self):
        """Sprint 46N's deliberate design: threshold_pct and
        cooldown_hours are NOT persisted because they're config
        values that can legitimately change between restarts. If
        the operator bumps threshold from 15% to 20% via
        config.yaml + restart, the new threshold must take effect
        even if the persisted file still has the old value."""
        tmpdir = tempfile.mkdtemp()
        state_path = os.path.join(tmpdir, "drawdown_killswitch.json")

        # Persist a state that was at threshold=15%, cooldown=24h
        pre = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
        pre.update(100.0)
        pre.update(85.0)  # -15%, triggers
        pre.persist(state_path)

        # "Restart" with NEW threshold=20% and cooldown=48h
        post = DrawdownKillSwitch.load(
            state_path, threshold_pct=20.0, cooldown_hours=48.0,
        )
        self.assertEqual(post.threshold_pct, 20.0)
        self.assertEqual(post.cooldown_hours, 48.0)
        # But the persistence state (peak, triggered) was preserved
        self.assertAlmostEqual(post.peak_equity, 100.0, places=2)
        self.assertTrue(post.triggered)

    def test_corrupt_state_file_returns_fresh_instance(self):
        """A damaged persistence file (truncated, garbage bytes)
        must NOT crash startup — the load() helper is fail-open
        by design (Sprint 46N). The bot starts with peak=0,
        not triggered, same as a fresh deployment."""
        tmpdir = tempfile.mkdtemp()
        state_path = os.path.join(tmpdir, "drawdown_killswitch.json")
        with open(state_path, "w") as f:
            f.write("{garbage, not valid json at all")

        kds = DrawdownKillSwitch.load(
            state_path, threshold_pct=15.0, cooldown_hours=24.0,
        )
        self.assertFalse(kds.triggered)
        self.assertEqual(kds.peak_equity, 0.0)
        self.assertIsNone(kds.triggered_at)


class KillSwitchWarmupTest(unittest.TestCase):
    """Edge case the audit specifically called out: 'a missing
    price transients as a 100% loss, sends equity to 0, and
    triggers the kill switch from a single bad read'. The
    Sprint 46N fix uses EquityTracker.latest().total_equity as
    the source — but the KillSwitch's own warmup behavior
    (first equity reading is the peak) is what protects against
    a fresh-start 0-read triggering immediately."""

    def test_first_equity_reading_becomes_peak(self):
        """The very first update() call must NOT trigger, even
        if the equity is low — the peak is set from the first
        reading, so drawdown is 0%. This protects the bot from
        a transient 0-balance read at startup."""
        kds = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
        state = kds.update(0.0)  # worst case: 0 balance
        self.assertFalse(state.triggered,
                         "first equity reading must set peak, not trigger")
        self.assertEqual(state.drawdown_pct, 0.0)
        self.assertAlmostEqual(kds.peak_equity, 0.0, places=2)

    def test_first_nonzero_reading_becomes_peak(self):
        """More realistic: a fresh deployment reads $18.06 as
        the first equity. That should become the peak, not
        trigger."""
        kds = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
        state = kds.update(18.06)
        self.assertFalse(state.triggered)
        self.assertEqual(state.drawdown_pct, 0.0)


if __name__ == "__main__":
    unittest.main()
