"""
Sprint 22 tests — Paper-to-Live Transition Safe Mode.

Run: python -m unittest tests.test_paper_to_live -v
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.paper_to_live import (
    PaperToLiveChecklist, TransitionDecision, run_preflight, DRY_RUN_MIN_QTY,
)
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository, Position


def _make_fake_broker(balance=20.0, dry_run_success=True):
    """Mock broker that simulates successful connection + dry-run."""
    broker = MagicMock()
    broker.get_usdt_balance.return_value = balance
    if dry_run_success:
        broker.create_market_order.return_value = {"id": "DRY_TEST_123", "status": "filled"}
    else:
        broker.create_market_order.return_value = {"id": None, "status": "failed"}
    return broker


def _make_failing_broker(error_msg="connection refused"):
    """Mock broker that simulates connection failure."""
    broker = MagicMock()
    broker.get_usdt_balance.side_effect = Exception(error_msg)
    return broker


class PaperToLiveHappyPathTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.broker = _make_fake_broker()

    def test_no_paper_positions_proceeds_to_live(self):
        """Clean state (no paper positions) → safe to proceed."""
        checklist = PaperToLiveChecklist(
            position_repo=self.repo,
            audit=self.audit,
            broker=self.broker,
            interactive=False,
            auto_action="abort",  # shouldn't matter, no positions to act on
        )
        decision = checklist.run(dry_run=True)
        self.assertTrue(decision.proceed)
        self.assertEqual(decision.paper_positions_closed, 0)
        self.assertEqual(decision.broker_balance, 20.0)
        self.assertTrue(decision.dry_run_validated)

        # Audit recorded the transition
        events = self.audit.read_all()
        approved = [e for e in events if e.get("event_type") == "LIVE_TRANSITION_APPROVED"]
        self.assertEqual(len(approved), 1)


class PaperPositionsHandlingTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.broker = _make_fake_broker()
        # Add 2 paper positions
        self.repo.add_open(Position(
            asset="BTC-USD", direction="long",
            entry_price=50000, stop_loss=49000, take_profit=52000,
            qty=0.001, risk_usd=10.0,
            entry_ts=time.time(), strategy="test",
        ))
        self.repo.add_open(Position(
            asset="ETH-USD", direction="long",
            entry_price=3000, stop_loss=2950, take_profit=3150,
            qty=0.01, risk_usd=5.0,
            entry_ts=time.time(), strategy="test",
        ))

    def test_auto_action_close_removes_paper_positions(self):
        """auto_action='close' should close all paper positions and proceed."""
        checklist = PaperToLiveChecklist(
            position_repo=self.repo,
            audit=self.audit,
            broker=self.broker,
            interactive=False,
            auto_action="close",
        )
        decision = checklist.run(dry_run=True)
        self.assertTrue(decision.proceed)
        self.assertEqual(decision.paper_positions_closed, 2)
        self.assertEqual(self.repo.count_open(), 0)

    def test_auto_action_ignore_keeps_paper_positions_with_warning(self):
        """auto_action='ignore' should keep paper positions but log a warning."""
        checklist = PaperToLiveChecklist(
            position_repo=self.repo,
            audit=self.audit,
            broker=self.broker,
            interactive=False,
            auto_action="ignore",
        )
        decision = checklist.run(dry_run=True)
        self.assertTrue(decision.proceed)
        self.assertEqual(decision.paper_positions_closed, 0)
        self.assertEqual(self.repo.count_open(), 2, "Paper positions should remain")

        # Audit should record the warning
        events = self.audit.read_all()
        warnings = [e for e in events if e.get("event_type") == "LIVE_TRANSITION_PAPER_IGNORED"]
        self.assertEqual(len(warnings), 1)

    def test_auto_action_abort_blocks_live(self):
        """auto_action='abort' (default) should refuse to proceed."""
        checklist = PaperToLiveChecklist(
            position_repo=self.repo,
            audit=self.audit,
            broker=self.broker,
            interactive=False,
            auto_action="abort",
        )
        decision = checklist.run(dry_run=True)
        self.assertFalse(decision.proceed)
        self.assertIn("aborted", decision.reason.lower())
        self.assertEqual(self.repo.count_open(), 2, "Paper positions should remain unchanged")


class BrokerConnectivityTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))

    def test_broker_connection_failure_blocks_live(self):
        broker = _make_failing_broker("network timeout")
        checklist = PaperToLiveChecklist(
            position_repo=self.repo,
            audit=self.audit,
            broker=broker,
            interactive=False,
            auto_action="abort",
        )
        decision = checklist.run(dry_run=True)
        self.assertFalse(decision.proceed)
        self.assertEqual(decision.reason, "broker_unreachable")
        self.assertFalse(decision.broker_connected)

    def test_broker_zero_balance_blocks_live(self):
        broker = _make_fake_broker(balance=0.0)
        checklist = PaperToLiveChecklist(
            position_repo=self.repo,
            audit=self.audit,
            broker=broker,
            interactive=False,
            auto_action="abort",
        )
        decision = checklist.run(dry_run=True)
        self.assertFalse(decision.proceed)
        self.assertEqual(decision.reason, "broker_unreachable")

    def test_no_broker_blocks_live(self):
        checklist = PaperToLiveChecklist(
            position_repo=self.repo,
            audit=self.audit,
            broker=None,
            interactive=False,
            auto_action="abort",
        )
        decision = checklist.run(dry_run=True)
        self.assertFalse(decision.proceed)
        self.assertEqual(decision.reason, "broker_unreachable")


class DryRunValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))

    def test_dry_run_failure_blocks_live(self):
        broker = _make_fake_broker(balance=20.0, dry_run_success=False)
        checklist = PaperToLiveChecklist(
            position_repo=self.repo,
            audit=self.audit,
            broker=broker,
            interactive=False,
            auto_action="abort",
        )
        decision = checklist.run(dry_run=True)
        self.assertFalse(decision.proceed)
        self.assertEqual(decision.reason, "dry_run_validation_failed")
        # Broker was called for the dry-run
        broker.create_market_order.assert_called_once()

    def test_dry_run_skipped_when_disabled(self):
        broker = _make_fake_broker(balance=20.0)
        checklist = PaperToLiveChecklist(
            position_repo=self.repo,
            audit=self.audit,
            broker=broker,
            interactive=False,
            auto_action="abort",
        )
        decision = checklist.run(dry_run=False)
        self.assertTrue(decision.proceed)
        self.assertFalse(decision.dry_run_validated)
        # Dry-run was NOT called
        broker.create_market_order.assert_not_called()


class RunPreflightConvenienceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))

    def test_run_preflight_with_minimal_config(self):
        """run_preflight should work with a minimal config dict."""
        broker = _make_fake_broker()
        config = {
            "live_transition": {"auto_action": "close", "dry_run_qty": 0.00001},
        }
        decision = run_preflight(
            config=config,
            position_repo=self.repo,
            audit=self.audit,
            broker=broker,
            interactive=False,
        )
        self.assertTrue(decision.proceed)


import time  # at module level for the helper
if __name__ == "__main__":
    unittest.main()