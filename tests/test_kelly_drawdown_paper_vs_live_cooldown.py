"""
Carlos: 24h cooldown after a drawdown trip is right for real money
(live), but freezes paper exploration for a full day over a cycle that
risked nothing real. DrawdownKillSwitch now accepts paper_overrides
(cooldown_hours) + mode_override_path, same dual-profile pattern as
RiskManagerAgent/StrategyAgent/MandateGate -- checked fresh every
update() call so toggling paper/live switches cooldowns immediately.

Run: python -m unittest tests.test_kelly_drawdown_paper_vs_live_cooldown -v
"""
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.kelly_drawdown import DrawdownKillSwitch


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


class DrawdownCooldownPaperVsLiveTest(unittest.TestCase):
    def test_paper_mode_uses_shorter_cooldown(self):
        tmpdir = tempfile.mkdtemp()
        ds = DrawdownKillSwitch(
            threshold_pct=15.0,
            cooldown_hours=24.0,
            mode_override_path=_write_mode_override(tmpdir, mandate_enabled=False),
            paper_overrides={"cooldown_hours": 0.001},  # ~3.6s, for a fast test
        )
        ds.update(100.0)
        ds.update(120.0)
        ds.update(80.0)  # triggers
        self.assertTrue(ds.is_triggered())
        time.sleep(4.0)
        state = ds.update(80.0)  # still deep in drawdown
        # With the paper override's tiny cooldown, this should have
        # released + rebased already -- NOT still be blocked.
        self.assertFalse(state.triggered)
        self.assertTrue(state.peak_rebased)

    def test_live_mode_keeps_full_cooldown(self):
        tmpdir = tempfile.mkdtemp()
        ds = DrawdownKillSwitch(
            threshold_pct=15.0,
            cooldown_hours=24.0,
            mode_override_path=_write_mode_override(tmpdir, mandate_enabled=True),
            paper_overrides={"cooldown_hours": 0.001},
        )
        ds.update(100.0)
        ds.update(120.0)
        ds.update(80.0)  # triggers
        self.assertTrue(ds.is_triggered())
        time.sleep(4.0)
        state = ds.update(80.0)
        # Live ignores the paper override entirely -- 24h hasn't
        # remotely elapsed, must still be triggered.
        self.assertTrue(state.triggered)
        self.assertFalse(state.peak_rebased)

    def test_switching_mode_switches_cooldown_immediately(self):
        tmpdir = tempfile.mkdtemp()
        mode_path = _write_mode_override(tmpdir, mandate_enabled=True)
        ds = DrawdownKillSwitch(
            threshold_pct=15.0,
            cooldown_hours=24.0,
            mode_override_path=mode_path,
            paper_overrides={"cooldown_hours": 0.001},
        )
        ds.update(100.0)
        ds.update(120.0)
        ds.update(80.0)
        self.assertTrue(ds.is_triggered())

        # Flip to paper mid-drawdown -- no restart needed.
        with open(mode_path, "w", encoding="utf-8") as f:
            json.dump({"mandate_enabled": False}, f)
        time.sleep(4.0)
        state = ds.update(80.0)
        self.assertFalse(state.triggered)
        self.assertTrue(state.peak_rebased)

    def test_no_overrides_configured_behaves_exactly_like_before(self):
        """Back-compat: omitting paper_overrides (the default) must
        never change behavior for any existing caller."""
        tmpdir = tempfile.mkdtemp()
        ds = DrawdownKillSwitch(
            threshold_pct=15.0,
            cooldown_hours=0.001,
            mode_override_path=_write_mode_override(tmpdir, mandate_enabled=False),
        )
        ds.update(100.0)
        ds.update(120.0)
        ds.update(80.0)
        self.assertTrue(ds.is_triggered())
        time.sleep(4.0)
        state = ds.update(80.0)
        self.assertFalse(state.triggered)  # released via the base cooldown itself


if __name__ == "__main__":
    unittest.main()
