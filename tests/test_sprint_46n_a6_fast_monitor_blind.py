"""
Sprint 46N tests — audit finding A6 (AUDITORIA_COMPLETA_2026-07-11.md).

A6: `fast_monitor_tick()` (main.py) previously returned silently when
price fetching failed for EVERY open position — meaning SL/TP
protection silently did not run that tick, with no signal to Carlos
that anything was wrong. The fix adds a consecutive-blind-tick
counter plus a threshold-based SYSTEM_ERROR alert (mirroring the
existing MARKET_DATA_TOTAL_FAILURE pattern from the hourly cycle),
implemented as the pure, directly-testable function
`_should_alert_fast_monitor_blind(consecutive_blind_ticks, threshold)`.

`fast_monitor_tick` itself is a closure defined inside `main()` and
can't be unit-tested directly without running the whole bot startup
sequence, so this test file exercises the extracted decision function
in isolation.

Run: python -m unittest tests.test_sprint_46n_a6_fast_monitor_blind -v
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from main import _should_alert_fast_monitor_blind


class ShouldAlertFastMonitorBlindTest(unittest.TestCase):
    def test_no_alert_below_threshold(self):
        self.assertFalse(_should_alert_fast_monitor_blind(1, 3))
        self.assertFalse(_should_alert_fast_monitor_blind(2, 3))

    def test_alert_exactly_at_threshold(self):
        self.assertTrue(_should_alert_fast_monitor_blind(3, 3))

    def test_no_alert_between_threshold_multiples(self):
        self.assertFalse(_should_alert_fast_monitor_blind(4, 3))
        self.assertFalse(_should_alert_fast_monitor_blind(5, 3))

    def test_alert_again_at_next_multiple(self):
        self.assertTrue(_should_alert_fast_monitor_blind(6, 3))

    def test_alert_at_third_multiple(self):
        self.assertTrue(_should_alert_fast_monitor_blind(9, 3))

    def test_no_alert_at_zero(self):
        self.assertFalse(_should_alert_fast_monitor_blind(0, 3))

    def test_threshold_of_one_alerts_every_tick(self):
        for n in range(1, 6):
            self.assertTrue(_should_alert_fast_monitor_blind(n, 1))

    def test_defensive_non_positive_threshold_alerts_only_on_first_tick(self):
        self.assertTrue(_should_alert_fast_monitor_blind(1, 0))
        self.assertFalse(_should_alert_fast_monitor_blind(2, 0))
        self.assertTrue(_should_alert_fast_monitor_blind(1, -1))
        self.assertFalse(_should_alert_fast_monitor_blind(3, -1))


if __name__ == "__main__":
    unittest.main()
