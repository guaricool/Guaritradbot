"""
Sprint 43 C5 fix tests — no more ghost positions.

The previous flow:
  1. RiskManagerAgent.validate_and_size() added positions to the repo
     + audited POSITION_OPENED + published TRADE_OPENED.
  2. ExecutionNode.execute_order() THEN called the broker.
  3. If the broker call failed (ALPACA_NOT_CONFIGURED,
     SYMBOL_NOT_TRADEABLE, timeout, insufficient balance, etc.),
     the position was already in the repo — a "ghost" that counted
     toward max_open_trades, mandate exposure, and SL/TP monitoring.

The fix:
  - RiskManagerAgent no longer touches the repo or publishes
    TRADE_OPENED. It only returns approved_trades.
  - ExecutionNode._persist_filled_position() is called from the 4
    success paths (NO_BROKER, PAPER_MODE, crypto FILLED, equity
    FILLED). On any FAILED status, no position is added.

These tests verify:
  - risk_agent: no repo mutation after validate_and_size
  - execution_node: repo gets a Position ONLY on FILLED status
  - execution_node: no repo mutation on FAILED status
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.risk_agent import RiskManagerAgent
from src.execution.execution_node import ExecutionNode
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository


class _FakeBroker:
    """Captures market orders without hitting a real exchange."""
    def __init__(self):
        self.orders = []
    def get_usdt_balance(self):
        return 100.0
    @property
    def exchange(self):
        class _Ex:
            options = {"sandboxMode": True}
        return _Ex()
    def create_market_order(self, symbol, side, qty=None, **kwargs):
        self.orders.append({"symbol": symbol, "side": side, "qty": qty, "kwargs": kwargs})
        return {"id": "FAKE", "status": "filled"}


def _make_mode_override(tmpdir, mandate_enabled):
    """Write a mode_override.json so the paper-mode gate is deterministic."""
    import json
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled, "alpaca_paper": True}, f)
    return path


class RiskAgentNoLongerPersistsTest(unittest.TestCase):
    """
    C5 fix: RiskManagerAgent must NOT add positions to the repo.
    It only returns approved_trades. The repo is owned by
    ExecutionNode now (post-broker-fill).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.broker = _FakeBroker()
        self.event_bus = MagicMock()
        self.agent = RiskManagerAgent(
            broker_client=self.broker,
            risk_per_trade_pct=1.0,
            max_capital_per_trade_pct=50.0,
            atr_stop_multiplier=2.0,
            min_order_usd=10.0,
            audit=self.audit,
            position_repo=self.repo,
            event_bus=self.event_bus,
            # Sprint 45: network-dependent portfolio gates off in this pre-existing test (not what it's testing).
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
)

    def test_approved_trade_does_NOT_add_to_repo(self):
        """
        After C5, calling validate_and_size() should leave the repo
        empty. Position persistence moved to ExecutionNode.
        """
        self.assertEqual(self.repo.count_open(), 0)
        hyp = {
            "asset": "BTC-USD",
            "strategy": "momentum",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 500.0,
            "expected_move_pct": 3.0,
        }
        result = self.agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": [hyp]}})
        self.assertEqual(len(result["approved_trades"]), 1, "Trade should still be approved")
        # C5 fix: repo remains empty — ExecutionNode will add on fill
        self.assertEqual(
            self.repo.count_open(), 0,
            "RiskAgent must NOT add to repo (C5 fix). ExecutionNode owns persistence.",
        )

    def test_approved_trade_does_NOT_publish_TRADE_OPENED(self):
        hyp = {
            "asset": "BTC-USD",
            "strategy": "momentum",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 500.0,
            "expected_move_pct": 3.0,
        }
        self.agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": [hyp]}})
        # C5 fix: TRADE_OPENED should NOT be published from risk_agent.
        # It's published from ExecutionNode on confirmed fill.
        published = [c.args[0] for c in self.event_bus.publish.call_args_list]
        self.assertNotIn(
            "TRADE_OPENED", published,
            "RiskAgent must NOT publish TRADE_OPENED (C5 fix). "
            f"Published: {published}",
        )

    def test_approved_trade_does_NOT_audit_POSITION_OPENED(self):
        hyp = {
            "asset": "BTC-USD",
            "strategy": "momentum",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 500.0,
            "expected_move_pct": 3.0,
        }
        self.agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": [hyp]}})
        opened = [e for e in self.audit.read_all() if e.get("event_type") == "POSITION_OPENED"]
        self.assertEqual(
            len(opened), 0,
            f"RiskAgent must NOT audit POSITION_OPENED (C5 fix). Got: {opened}",
        )


class ExecutionNodePersistsOnFillTest(unittest.TestCase):
    """
    C5 fix: ExecutionNode._persist_filled_position() is the ONLY
    place positions are added to the repo. It runs only on FILLED
    status — never on FAILED.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.event_bus = MagicMock()
        # MagicMock broker with explicit supported symbols so the
        # SYMBOL_NOT_SUPPORTED pre-flight check passes in live mode.
        self.broker = MagicMock()
        self.broker.exchange.symbols = ["BTC/USD", "ETH/USD"]
        self.broker.exchange.options = {"sandboxMode": True}
        self.broker.create_market_order.return_value = {"id": "FAKE_1", "status": "filled"}
        # Paper mode (default) — fills are simulated, broker NOT called
        self.mode_override_path = _make_mode_override(self.tmpdir, False)
        self.node = ExecutionNode(
            self.event_bus,
            broker_client=self.broker,
            brokers_config={"crypto": {"symbols": ["BTC-USD"]}, "equity": {"symbols": []}},
            audit=self.audit,
            mode_override_path=self.mode_override_path,
            position_repo=self.repo,
        )
        self.order = {
            "asset": "BTC-USD",
            "direction": "long",
            "position_size": 0.001,
            "entry_price": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "risk_usd": 0.5,
            "notional_usd": 50.0,
            "strategy": "momentum",
        }

    def test_paper_mode_fill_adds_position_to_repo(self):
        self.assertEqual(self.repo.count_open(), 0)
        self.node.execute_order(self.order)
        # C5: repo now has 1 position
        self.assertEqual(
            self.repo.count_open(), 1,
            f"FILLED status must add a position. Audit: {self.audit_events}",
        )
        pos = self.repo.open()[0]
        self.assertEqual(pos.asset, "BTC-USD")
        self.assertEqual(pos.direction, "long")
        self.assertEqual(pos.entry_price, 50000.0)
        self.assertEqual(pos.qty, 0.001)
        self.assertEqual(pos.strategy, "momentum")

    def test_paper_mode_fill_audits_POSITION_OPENED(self):
        self.node.execute_order(self.order)
        opened = [e for e in self.audit_events if e[0] == "POSITION_OPENED"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0][1]["asset"], "BTC-USD")
        self.assertIn("status", opened[0][1])

    def test_paper_mode_fill_publishes_TRADE_OPENED(self):
        self.node.execute_order(self.order)
        publishes = [c.args[0] for c in self.event_bus.publish.call_args_list]
        self.assertIn("TRADE_OPENED", publishes)

    def test_failed_broker_call_does_NOT_add_position(self):
        """Critical: if broker returns FAILED, NO ghost position is added."""
        # Switch to live mode
        self.mode_override_path = _make_mode_override(self.tmpdir, True)
        self.node.mode_override_path = self.mode_override_path
        # Broker returns failure
        self.broker.create_market_order.return_value = {"id": None, "status": "failed"}
        self.assertEqual(self.repo.count_open(), 0)
        self.node.execute_order(self.order)
        # C5: even though audit recorded TRADE_FAILED, repo stays empty
        self.assertEqual(
            self.repo.count_open(), 0,
            f"FAILED status must NOT add a position. Audit: {self.audit_events}",
        )
        failed = [e for e in self.audit_events if e[0] == "TRADE_FAILED"]
        self.assertEqual(len(failed), 1)
        opened = [e for e in self.audit_events if e[0] == "POSITION_OPENED"]
        self.assertEqual(len(opened), 0)

    def test_live_mode_fill_adds_position_to_repo(self):
        self.mode_override_path = _make_mode_override(self.tmpdir, True)
        self.node.mode_override_path = self.mode_override_path
        self.assertEqual(self.repo.count_open(), 0)
        self.node.execute_order(self.order)
        self.assertEqual(
            self.repo.count_open(), 1,
            f"LIVE FILLED must add a position. Audit: {self.audit_events}",
        )
        # Verify broker was actually called
        self.broker.create_market_order.assert_called_once()

    def test_no_position_repo_silently_skips_persistence(self):
        """
        If position_repo is not injected (some test setups), the
        helper should NOT raise. The fill still happens; only the
        repo mutation is skipped.
        """
        node = ExecutionNode(
            self.event_bus,
            broker_client=self.broker,
            brokers_config={"crypto": {"symbols": ["BTC-USD"]}, "equity": {"symbols": []}},
            audit=self.audit,
            mode_override_path=self.mode_override_path,
            position_repo=None,  # explicitly None
        )
        node.execute_order(self.order)  # must not raise


if __name__ == "__main__":
    unittest.main()
