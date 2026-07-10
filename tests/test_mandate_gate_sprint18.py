"""
Sprint 18 tests — Mandate Gate fixes (Bug B + Bug C).

Covers:
- Bug B: Phantom Exposure — exposure must come from PositionRepository
         (open positions), not unbounded sum of TRADE_FILLED events.
- Bug C: Punished for Trying — daily_loss is REALIZED P&L from closed
         positions, not theoretical risk_usd of approved trades.

Run: python -m unittest tests.test_mandate_gate_sprint18 -v
"""
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.mandate_gate import MandateGate, MandateConfig
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository, Position


class PhantomExposureTest(unittest.TestCase):
    """
    Bug B: After 5 round-trip trades, exposure should be 0 (all closed),
    not $100 (5x TRADE_FILLED still in the sum).
    """

    def test_exposure_zero_after_round_trip_trades(self):
        tmpdir = tempfile.mkdtemp()
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        repo = PositionRepository(os.path.join(tmpdir, "positions.json"))

        cfg = MandateConfig(
            enabled=True,
            max_position_usd=20.0,
            max_daily_loss_usd=5.0,
            max_total_exposure_usd=100.0,
        )
        gate = MandateGate(cfg, audit_ledger=audit, position_repo=repo)

        # Simulate 5 round-trip trades by appending audit events
        # (this is what was happening before the fix — exposure kept growing)
        for i in range(5):
            pid = f"pos_{i}"
            audit.append("POSITION_OPENED", {"position_id": pid, "notional_usd": 20.0})
            audit.append("TRADE_FILLED", {
                "position_id": pid, "asset": "BTC-USD", "direction": "long",
                "filled_qty": 0.0005, "fill_price": 40000,
            })
            audit.append("TRADE_CLOSED", {
                "position_id": pid, "asset": "BTC-USD", "qty": 0.0005,
                "entry_price": 40000, "close_price": 41000,
                "realized_pnl_usd": 5.0, "reason": "TP_HIT",
            })

        # With position_repo as source of truth, exposure = 0 (all closed)
        exposure = gate._open_exposure_usd()
        self.assertEqual(exposure, 0.0,
                         f"Expected 0 exposure (all closed), got ${exposure}")

    def test_exposure_with_open_positions(self):
        tmpdir = tempfile.mkdtemp()
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        repo = PositionRepository(os.path.join(tmpdir, "positions.json"))

        # Open 2 positions via repo
        repo.add_open(Position(
            asset="BTC-USD", direction="long",
            entry_price=40000, stop_loss=39000, take_profit=42000,
            qty=0.0005, risk_usd=5.0,
            entry_ts=time.time(), strategy="test",
        ))
        repo.add_open(Position(
            asset="ETH-USD", direction="long",
            entry_price=2500, stop_loss=2400, take_profit=2700,
            qty=0.01, risk_usd=10.0,
            entry_ts=time.time(), strategy="test",
        ))

        cfg = MandateConfig(enabled=True, max_position_usd=20.0,
                            max_daily_loss_usd=5.0, max_total_exposure_usd=100.0)
        gate = MandateGate(cfg, audit_ledger=audit, position_repo=repo)

        exposure = gate._open_exposure_usd()
        # BTC: 0.0005 * 40000 = $20, ETH: 0.01 * 2500 = $25 → $45
        self.assertAlmostEqual(exposure, 45.0, places=2)

    def test_legacy_audit_only_path_correctly_subtracts(self):
        """Without position_repo, audit scan must subtract closes."""
        tmpdir = tempfile.mkdtemp()
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))

        # 2 opens, 2 closes → exposure = 0
        for i in range(2):
            audit.append("POSITION_OPENED", {"position_id": f"p_{i}", "notional_usd": 20.0})
            audit.append("TRADE_CLOSED", {
                "position_id": f"p_{i}", "qty": 0.0005, "entry_price": 40000,
            })
        # 1 still open
        audit.append("POSITION_OPENED", {"position_id": "p_open", "notional_usd": 30.0})

        cfg = MandateConfig(enabled=True)
        gate = MandateGate(cfg, audit_ledger=audit, position_repo=None)

        exposure = gate._open_exposure_usd()
        self.assertAlmostEqual(exposure, 30.0, places=2,
                               msg=f"Expected $30 (only p_open), got ${exposure}")


class PunishedForTryingTest(unittest.TestCase):
    """
    Bug C: 5 winning trades (each $1 risk) should NOT trigger kill switch.
    daily_loss should be 0 since all realized P&L is positive.
    """

    def test_daily_loss_zero_after_winning_trades(self):
        tmpdir = tempfile.mkdtemp()
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        repo = PositionRepository(os.path.join(tmpdir, "positions.json"))

        # 5 winning closed positions in the last hour
        for i in range(5):
            pos = Position(
                asset="BTC-USD", direction="long",
                entry_price=40000, stop_loss=39000, take_profit=42000,
                qty=0.001, risk_usd=10.0,
                entry_ts=time.time() - 3600,
                strategy="test",
            )
            # Manually close with positive PnL
            repo.add_open(pos)
            repo.close_position(pos.position_id, close_price=41000, reason="TP_HIT")

        cfg = MandateConfig(enabled=True, max_daily_loss_usd=5.0)
        gate = MandateGate(cfg, audit_ledger=audit, position_repo=repo)

        daily_loss = gate._daily_loss_usd()
        self.assertEqual(daily_loss, 0.0,
                         f"Wins should NOT count as loss; got ${daily_loss}")

    def test_daily_loss_sums_realized_losses(self):
        """3 losing trades totaling -$4 → daily_loss = $4."""
        tmpdir = tempfile.mkdtemp()
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        repo = PositionRepository(os.path.join(tmpdir, "positions.json"))

        pnls = [-1.0, -2.0, -1.0]
        for i, pnl in enumerate(pnls):
            pos = Position(
                asset="BTC-USD", direction="long",
                entry_price=40000, stop_loss=39000, take_profit=42000,
                qty=0.001, risk_usd=10.0,
                entry_ts=time.time() - 3600,
                strategy="test",
            )
            close_price = 40000 + (pnl / 0.001)
            repo.add_open(pos)
            repo.close_position(pos.position_id, close_price=close_price, reason="STOP_HIT")

        cfg = MandateConfig(enabled=True, max_daily_loss_usd=5.0)
        gate = MandateGate(cfg, audit_ledger=audit, position_repo=repo)

        daily_loss = gate._daily_loss_usd()
        self.assertAlmostEqual(daily_loss, 4.0, places=2)

    def test_old_behavior_would_have_triggered_kill_switch(self):
        """
        Regression test: under the OLD logic (summing TRADE_APPROVED risk_usd),
        5 trades with $1 risk each would total $5 = max_daily_loss_usd → kill.
        The new logic only counts REALIZED losses, so the same scenario is $0.
        """
        tmpdir = tempfile.mkdtemp()
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        # Note: no position_repo — only audit
        for i in range(5):
            audit.append("TRADE_APPROVED", {
                "asset": "BTC-USD", "risk_usd": 1.0, "notional_usd": 10.0,
            })

        cfg = MandateConfig(enabled=True, max_daily_loss_usd=5.0)
        gate = MandateGate(cfg, audit_ledger=audit, position_repo=None)

        daily_loss = gate._daily_loss_usd()
        # Under old logic this would be $5 (5 trades * $1 risk = $5 == max_daily_loss_usd → kill)
        # Under new logic (realized P&L only): $0 (no TRADE_CLOSED events)
        self.assertEqual(daily_loss, 0.0,
                         "New logic should NOT count theoretical risk as loss")


class MandateIntegrationTest(unittest.TestCase):
    """End-to-end: trade proposal should respect both bugs fixed."""

    def test_winning_trades_pass_daily_loss_check(self):
        """A new trade after 5 winners should NOT be blocked by daily_loss."""
        tmpdir = tempfile.mkdtemp()
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
        repo = PositionRepository(os.path.join(tmpdir, "positions.json"))

        # 5 closed winners
        for i in range(5):
            pos = Position(
                asset="BTC-USD", direction="long",
                entry_price=40000, stop_loss=39000, take_profit=42000,
                qty=0.001, risk_usd=10.0,
                entry_ts=time.time() - 3600,
                strategy="test",
            )
            repo.add_open(pos)
            repo.close_position(pos.position_id, close_price=42000, reason="TP_HIT")

        cfg = MandateConfig(
            enabled=True,
            allowed_symbols={"BTC-USD"},
            max_position_usd=20.0,
            max_daily_loss_usd=5.0,
            max_total_exposure_usd=100.0,
        )
        gate = MandateGate(cfg, audit_ledger=audit, position_repo=repo)

        verdict = gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 15.0,
            "risk_usd": 1.0,
        })
        self.assertTrue(verdict.ok, f"Should pass; got {verdict.reason}")
        self.assertEqual(verdict.daily_loss_so_far_usd, 0.0)


class MandateGateC3FixTest(unittest.TestCase):
    """
    Sprint 43 C3 fix: NaN/Inf in notional_usd or risk_usd must be
    rejected explicitly, not silently fail-open.

    Python's IEEE 754 behavior: `NaN > x` returns False, so a NaN
    notional would pass all 3 cap checks (per-trade size, daily
    loss, total exposure) and the mandate would approve a trade
    with undefined size. The audit flagged this as a fail-open
    vulnerability.
    """

    def _gate(self):
        cfg = MandateConfig(
            enabled=True,
            allowed_symbols={"BTC-USD"},
            max_position_usd=20.0,
            max_daily_loss_usd=5.0,
            max_total_exposure_usd=100.0,
        )
        return MandateGate(cfg)

    def test_nan_notional_rejected(self):
        gate = self._gate()
        v = gate.validate({
            "asset": "BTC-USD",
            "notional_usd": float("nan"),
            "risk_usd": 1.0,
        })
        self.assertFalse(v.ok)
        self.assertIn("non_finite_notional_or_risk", v.reason)

    def test_nan_risk_rejected(self):
        gate = self._gate()
        v = gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 10.0,
            "risk_usd": float("nan"),
        })
        self.assertFalse(v.ok)
        self.assertIn("non_finite_notional_or_risk", v.reason)

    def test_inf_notional_rejected(self):
        gate = self._gate()
        v = gate.validate({
            "asset": "BTC-USD",
            "notional_usd": float("inf"),
            "risk_usd": 1.0,
        })
        self.assertFalse(v.ok)
        self.assertIn("non_finite_notional_or_risk", v.reason)

    def test_negative_inf_risk_rejected(self):
        gate = self._gate()
        v = gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 10.0,
            "risk_usd": float("-inf"),
        })
        self.assertFalse(v.ok)
        self.assertIn("non_finite_notional_or_risk", v.reason)

    def test_finite_zero_still_works(self):
        """
        Regression guard: the C3 fix must NOT change behavior for
        legitimate zero notional (a $0 trade is not a NaN and should
        be evaluated normally). $0 notional is below max_position_usd
        so it should pass the per-trade cap and only fail later checks
        (which is fine — the test just confirms we don't reject $0).
        """
        gate = self._gate()
        v = gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 0.0,
            "risk_usd": 0.0,
        })
        # $0 notional passes per-trade cap; daily-loss cap (0+0 > 5) is False; exposure (0+0 > 100) is False.
        # So it should pass.
        self.assertTrue(v.ok, f"$0 trade should pass: {v.reason}")


if __name__ == "__main__":
    unittest.main()