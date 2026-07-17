"""
Bug: MandateGate's total-exposure cap only checked
position_repo.total_exposure_usd() (already-FILLED positions). Trades
approved earlier in the SAME RiskManagerAgent.validate_and_size() batch
aren't persisted to position_repo until ExecutionNode's later step, so
N hypotheses evaluated in one cycle were each checked against the same
stale open_exp — all could pass individually while collectively
blowing through max_total_exposure_usd.

Fix: MandateGate.validate() now accepts extra_pending_exposure_usd,
and RiskManagerAgent.validate_and_size() accumulates the
notional_with_fees_usd of every trade approved earlier in the same
cycle and passes it through.

Run: python -m unittest tests.test_mandate_gate_same_cycle_exposure -v
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.mandate_gate import MandateGate, MandateConfig
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository
from src.agents.risk_agent import RiskManagerAgent


class MandateGateExtraPendingExposureTest(unittest.TestCase):
    def _gate(self, tmpdir, max_total_exposure_usd=100.0):
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        repo = PositionRepository(os.path.join(tmpdir, "positions.json"))
        cfg = MandateConfig(
            enabled=True,
            max_position_usd=1000.0,
            max_daily_loss_usd=1000.0,
            max_total_exposure_usd=max_total_exposure_usd,
        )
        return MandateGate(cfg, audit_ledger=audit, position_repo=repo)

    def test_no_pending_exposure_behaves_like_before(self):
        tmpdir = tempfile.mkdtemp()
        gate = self._gate(tmpdir, max_total_exposure_usd=100.0)
        v = gate.validate({"asset": "BTC-USD", "notional_usd": 80.0, "risk_usd": 1.0})
        self.assertTrue(v.ok)

    def test_pending_exposure_from_same_cycle_is_added(self):
        """80 open + 0 pending -> ok. But 80 open + 60 pending (already
        approved earlier this cycle) + 80 new = 220 > 100 -> blocked,
        even though position_repo alone still only shows the 80."""
        tmpdir = tempfile.mkdtemp()
        gate = self._gate(tmpdir, max_total_exposure_usd=100.0)
        v = gate.validate(
            {"asset": "ETH-USD", "notional_usd": 80.0, "risk_usd": 1.0},
            extra_pending_exposure_usd=60.0,
        )
        self.assertFalse(v.ok)
        self.assertIn("exposure_cap", v.reason)

    def test_three_hypotheses_in_one_cycle_cannot_collectively_bust_cap(self):
        """Three $80 trades in the same cycle against a $100 cap and 0
        currently-open exposure: only the first may pass; the second
        and third must be rejected by the cumulative same-cycle check."""
        tmpdir = tempfile.mkdtemp()
        gate = self._gate(tmpdir, max_total_exposure_usd=100.0)

        pending = 0.0
        results = []
        for i in range(3):
            v = gate.validate(
                {"asset": f"ASSET{i}", "notional_usd": 80.0, "risk_usd": 1.0},
                extra_pending_exposure_usd=pending,
            )
            results.append(v.ok)
            if v.ok:
                pending += 80.0

        self.assertEqual(results, [True, False, False])


class RiskManagerAgentSameCycleExposureTest(unittest.TestCase):
    """End-to-end: validate_and_size() must wire the running pending
    total through to the mandate for every hypothesis in the batch."""

    def _make_agent(self, tmpdir, mandate):
        return RiskManagerAgent(
            broker_client=None,
            risk_per_trade_pct=1.0,
            max_capital_per_trade_pct=100,
            atr_stop_multiplier=2.0,
            atr_take_profit_multiplier=4.0,
            max_open_trades=10,
            audit=AuditLedger(os.path.join(tmpdir, "audit.jsonl")),
            position_repo=PositionRepository(os.path.join(tmpdir, "positions.json")),
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            mandate_gate=mandate,
        )

    def test_second_hypothesis_blocked_by_first_approved_in_same_cycle(self):
        tmpdir = tempfile.mkdtemp()
        cfg = MandateConfig(
            enabled=True,
            max_position_usd=1000.0,
            max_daily_loss_usd=1000.0,
            max_total_exposure_usd=100.0,
        )
        mandate = MandateGate(
            cfg,
            audit_ledger=AuditLedger(os.path.join(tmpdir, "mandate_audit.jsonl")),
            position_repo=PositionRepository(os.path.join(tmpdir, "positions.json")),
        )
        agent = self._make_agent(tmpdir, mandate)

        # Deterministic sizing: risk_per_trade_pct=1% of a $10,000
        # balance -> risk_usd=$100. distance = atr(1.0) * atr_stop_
        # multiplier(2.0) = 2.0 -> qty = 100/2 = 50. price=1.6 ->
        # notional = 50 * 1.6 = $80 per trade. max_capital_per_trade_pct
        # =100% of $10,000 doesn't cap it. Two such trades ($160 total)
        # exceed the $100 mandate cap even though each is individually
        # under it and position_repo alone (0 filled) wouldn't catch it.
        hypotheses = [
            {"asset": "BTC-USD", "direction": "long", "price": 1.6,
             "atr_at_signal": 1.0, "strategy": "test"},
            {"asset": "ETH-USD", "direction": "long", "price": 1.6,
             "atr_at_signal": 1.0, "strategy": "test"},
        ]
        from unittest.mock import patch
        with patch.object(agent, "get_account_balance", return_value=(10000.0, "test")):
            result = agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": hypotheses}})

        approved = result.get("approved_trades") or result.get("approved") or []
        rejected = result.get("rejected_trades") or result.get("rejected") or []
        self.assertEqual(len(approved), 1, f"approved={approved!r} rejected={rejected!r}")
        self.assertTrue(len(rejected) >= 1)
        self.assertTrue(
            any("exposure_cap" in str(r.get("reason", "")) for r in rejected),
            f"expected an exposure_cap rejection, got: {rejected!r}",
        )


if __name__ == "__main__":
    unittest.main()
