"""
Carlos: "aunque sea ganar poco pero con muchas entradas" -- an optional
scalp profile (tighter take-profit, smaller per-trade cap so more
positions fit, more sensitive SMART_PROFIT_TAKE reversal-exit), layered
on top of the paper/live profile, toggled independently via the
dashboard (audit/scalp_mode_override.json) so it can be A/B tested
against the normal swing profile without a restart. Paper-only.

Run: python -m unittest tests.test_scalp_mode -v
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.risk_agent import RiskManagerAgent
from src.execution.broker_routing import is_scalp_mode_enabled
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


def _write_scalp_override(tmpdir, scalp_mode_enabled: bool) -> str:
    path = os.path.join(tmpdir, "scalp_mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"scalp_mode_enabled": scalp_mode_enabled}, f)
    return path


SCALP_OVERRIDES = {
    "atr_take_profit_multiplier": 1.0,
    "max_capital_per_trade_pct": 20,
    "max_open_trades": 10,
}


class IsScalpModeEnabledTest(unittest.TestCase):
    def test_missing_file_defaults_off(self):
        self.assertFalse(is_scalp_mode_enabled("/nonexistent/path.json"))

    def test_reads_true(self):
        tmpdir = tempfile.mkdtemp()
        path = _write_scalp_override(tmpdir, True)
        self.assertTrue(is_scalp_mode_enabled(path))

    def test_reads_false(self):
        tmpdir = tempfile.mkdtemp()
        path = _write_scalp_override(tmpdir, False)
        self.assertFalse(is_scalp_mode_enabled(path))

    def test_corrupt_file_fails_closed(self):
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "scalp_mode_override.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self.assertFalse(is_scalp_mode_enabled(path))


class RiskManagerAgentScalpModeTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))

    def _make_agent(self, live: bool, scalp_enabled: bool):
        return RiskManagerAgent(
            broker_client=None,
            risk_per_trade_pct=1.0,
            max_capital_per_trade_pct=50,
            atr_stop_multiplier=2.0,
            atr_take_profit_multiplier=4.0,
            max_open_trades=5,
            mode_override_path=_write_mode_override(self.tmpdir, live),
            scalp_overrides=SCALP_OVERRIDES,
            scalp_mode_path=_write_scalp_override(self.tmpdir, scalp_enabled),
            audit=self.audit,
            position_repo=self.repo,
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
        )

    def test_paper_plus_scalp_on_applies_scalp_profile(self):
        agent = self._make_agent(live=False, scalp_enabled=True)
        agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.atr_take_profit_multiplier, 1.0)
        self.assertEqual(agent.max_capital_per_trade_pct, 20)
        self.assertEqual(agent.max_open_trades, 10)

    def test_paper_plus_scalp_off_keeps_normal_profile(self):
        agent = self._make_agent(live=False, scalp_enabled=False)
        agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.atr_take_profit_multiplier, 4.0)
        self.assertEqual(agent.max_capital_per_trade_pct, 50)
        self.assertEqual(agent.max_open_trades, 5)

    def test_live_ignores_scalp_toggle_even_if_enabled(self):
        """Scalp mode is paper-only -- must never apply in live, even
        if the toggle file says enabled (e.g. left on from a paper
        session before switching to live)."""
        agent = self._make_agent(live=True, scalp_enabled=True)
        agent.broker = None
        import unittest.mock as mock
        with mock.patch.object(RiskManagerAgent, "get_account_balance", return_value=(1000.0, "test")):
            agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.atr_take_profit_multiplier, 4.0)
        self.assertEqual(agent.max_capital_per_trade_pct, 50)
        self.assertEqual(agent.max_open_trades, 5)

    def test_toggling_scalp_mode_between_calls_switches_immediately(self):
        mode_path = _write_mode_override(self.tmpdir, False)
        scalp_path = os.path.join(self.tmpdir, "scalp_mode_override.json")
        with open(scalp_path, "w", encoding="utf-8") as f:
            json.dump({"scalp_mode_enabled": False}, f)
        agent = RiskManagerAgent(
            broker_client=None, risk_per_trade_pct=1.0, max_capital_per_trade_pct=50,
            atr_stop_multiplier=2.0, atr_take_profit_multiplier=4.0, max_open_trades=5,
            mode_override_path=mode_path, scalp_overrides=SCALP_OVERRIDES,
            scalp_mode_path=scalp_path,
            audit=self.audit, position_repo=self.repo,
            correlation_check_enabled=False, tail_risk_check_enabled=False,
        )
        agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.max_capital_per_trade_pct, 50)  # scalp off

        with open(scalp_path, "w", encoding="utf-8") as f:
            json.dump({"scalp_mode_enabled": True}, f)
        agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.max_capital_per_trade_pct, 20)  # scalp on, no restart needed

    def test_no_scalp_overrides_configured_behaves_exactly_like_before(self):
        """Back-compat: omitting scalp_overrides (the default) must
        never change behavior, even if the toggle file says enabled."""
        agent = RiskManagerAgent(
            broker_client=None, risk_per_trade_pct=1.0, max_capital_per_trade_pct=50,
            atr_stop_multiplier=2.0, atr_take_profit_multiplier=4.0, max_open_trades=5,
            mode_override_path=_write_mode_override(self.tmpdir, False),
            scalp_mode_path=_write_scalp_override(self.tmpdir, True),
            audit=self.audit, position_repo=self.repo,
            correlation_check_enabled=False, tail_risk_check_enabled=False,
        )
        agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.max_capital_per_trade_pct, 50)
        self.assertEqual(agent.max_open_trades, 5)


class ScalpModeStateApiTest(unittest.TestCase):
    def test_read_write_roundtrip(self):
        from src.api.state import read_scalp_mode, write_scalp_mode
        tmpdir = tempfile.mkdtemp()
        audit_path = os.path.join(tmpdir, "audit.jsonl")

        info = read_scalp_mode(audit_path=audit_path)
        self.assertFalse(info.scalp_mode_enabled)

        written = write_scalp_mode(scalp_mode_enabled=True, switched_by="test", audit_path=audit_path)
        self.assertTrue(written.scalp_mode_enabled)
        self.assertEqual(written.switched_by, "test")

        reread = read_scalp_mode(audit_path=audit_path)
        self.assertTrue(reread.scalp_mode_enabled)


if __name__ == "__main__":
    unittest.main()
