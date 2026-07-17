"""
Mandate Gate is now enabled (config.yaml `mandate.enabled: true`), but
its live caps (max_position_usd=$20, max_total_exposure_usd=$100) would
otherwise block the aggressive paper-exploration profile (up to 80% of
paper balance per trade). MandateGate now accepts a `paper_overrides`
dict, active only while in paper mode, re-checked fresh on every
validate() call — same pattern as RiskManagerAgent.paper_overrides /
StrategyAgent.paper_params_overrides.

Run: python -m unittest tests.test_mandate_gate_paper_vs_live_profiles -v
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.mandate_gate import MandateGate, MandateConfig


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


LIVE_CFG = MandateConfig(
    enabled=True,
    max_position_usd=20.0,
    max_daily_loss_usd=5.0,
    max_total_exposure_usd=100.0,
)

PAPER_OVERRIDES = {
    "max_position_usd": 800.0,
    "max_total_exposure_usd": 900.0,
    "max_daily_loss_usd": 100.0,
}


class MandateGatePaperVsLiveProfileTest(unittest.TestCase):
    def _make_gate(self, tmpdir, live: bool):
        return MandateGate(
            LIVE_CFG,
            mode_override_path=_write_mode_override(tmpdir, live),
            paper_overrides=PAPER_OVERRIDES,
        )

    def test_paper_mode_adopts_aggressive_caps(self):
        tmpdir = tempfile.mkdtemp()
        gate = self._make_gate(tmpdir, live=False)
        # $500 notional would be rejected under the live $20 cap, but
        # must pass under the paper override's $800 cap.
        v = gate.validate({"asset": "BTC-USD", "notional_usd": 500.0, "risk_usd": 1.0})
        self.assertTrue(v.ok, v.reason)

    def test_live_mode_keeps_recommended_caps(self):
        tmpdir = tempfile.mkdtemp()
        gate = self._make_gate(tmpdir, live=True)
        v = gate.validate({"asset": "BTC-USD", "notional_usd": 500.0, "risk_usd": 1.0})
        self.assertFalse(v.ok)
        self.assertIn("notional_exceeds_max", v.reason)

    def test_switching_mode_switches_caps_immediately(self):
        tmpdir = tempfile.mkdtemp()
        mode_path = _write_mode_override(tmpdir, False)
        gate = MandateGate(LIVE_CFG, mode_override_path=mode_path, paper_overrides=PAPER_OVERRIDES)

        v_paper = gate.validate({"asset": "BTC-USD", "notional_usd": 500.0, "risk_usd": 1.0})
        self.assertTrue(v_paper.ok)

        with open(mode_path, "w", encoding="utf-8") as f:
            json.dump({"mandate_enabled": True}, f)
        v_live = gate.validate({"asset": "BTC-USD", "notional_usd": 500.0, "risk_usd": 1.0})
        self.assertFalse(v_live.ok)

    def test_no_overrides_configured_behaves_exactly_like_before(self):
        tmpdir = tempfile.mkdtemp()
        gate = MandateGate(LIVE_CFG, mode_override_path=_write_mode_override(tmpdir, False))
        v = gate.validate({"asset": "BTC-USD", "notional_usd": 500.0, "risk_usd": 1.0})
        self.assertFalse(v.ok)
        self.assertIn("notional_exceeds_max", v.reason)

    def test_partial_override_only_changes_listed_fields(self):
        tmpdir = tempfile.mkdtemp()
        gate = MandateGate(
            LIVE_CFG,
            mode_override_path=_write_mode_override(tmpdir, False),
            paper_overrides={"max_position_usd": 800.0},  # only this field
        )
        # total exposure cap should remain the LIVE $100 (unchanged)
        v = gate.validate({"asset": "BTC-USD", "notional_usd": 500.0, "risk_usd": 1.0})
        self.assertFalse(v.ok)
        self.assertIn("exposure_cap", v.reason)


if __name__ == "__main__":
    unittest.main()
