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
        """Sprint 45 fix (H4 test gap): this test used to mock
        `create_market_order` to fail, and asserted it was called.
        But Sprint 43's H4 fix made `_validate_dry_run` use a
        READ-ONLY `get_usdt_balance()` check by default instead of
        placing a real order (the old behavior left a few cents of
        real BTC bought and never sold/registered). The new default
        path never calls `create_market_order` at all, so this test
        was silently testing dead code — it kept "passing" for the
        wrong reason until the re-audit actually ran it and found
        `decision.proceed` was True (the mocked `create_market_order`
        failure was simply never exercised). Now it fails the
        dry-run the way it actually fails in production: the
        connectivity check (`_check_broker_connection`, step 1)
        succeeds with a valid balance, but the dry-run validation
        (`_validate_dry_run`, step 4) gets an invalid balance back.
        """
        broker = _make_fake_broker(balance=20.0)
        # First call (connectivity check) succeeds with $20; second
        # call (dry-run validation) returns an invalid balance.
        broker.get_usdt_balance.side_effect = [20.0, None]
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
        # Read-only balance check was called for both connectivity AND
        # the dry-run — no real order should ever be placed by default.
        self.assertEqual(broker.get_usdt_balance.call_count, 2)
        broker.create_market_order.assert_not_called()

    def test_legacy_destructive_dry_run_opt_in(self):
        """The old "place a real order" dry-run still exists as an
        explicit opt-in (`live_transition.dry_run_placement: true`),
        for brokers where a balance read alone doesn't prove the
        account can actually trade. Verify the opt-in path still
        works and that it's NOT used unless explicitly requested."""
        broker = _make_fake_broker(balance=20.0, dry_run_success=False)
        checklist = PaperToLiveChecklist(
            position_repo=self.repo,
            audit=self.audit,
            broker=broker,
            interactive=False,
            auto_action="abort",
        )
        checklist._config = {"live_transition": {"dry_run_placement": True}}
        decision = checklist.run(dry_run=True)
        self.assertFalse(decision.proceed)
        self.assertEqual(decision.reason, "dry_run_validation_failed")
        # Opted in explicitly -> the legacy path DOES place a real order.
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