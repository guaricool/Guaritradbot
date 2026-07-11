"""
Sprint 43 C4 fix tests — close failures must NOT mark position closed.

The previous behavior in `position_monitor._execute_close` and
`risk_agent._try_replace_position`:
  1. Send close order to broker.
  2. Catch any exception silently.
  3. The old in-code comment said "Aún así marcamos como cerrada
     localmente" — "we still mark it as closed locally".
  4. Call `repo.close_position()` regardless of broker outcome.

The fix: broker call happens FIRST. If the broker rejects or
throws, the position stays open in the repo (will be retried
on the next cycle). PositionMonitor keeps watching the SL/TP,
MandateGate keeps counting the exposure, the position is
consistent between repo and ground truth (what the broker has).

These tests verify:
  - position_monitor: broker failure → position stays open
  - position_monitor: broker success → position is closed
  - position_monitor: SYSTEM_ERROR published on failure
  - position_monitor: CLOSE_FAILED audit event on failure
  - risk_agent: replacement broker failure → no replacement
  - risk_agent: replacement broker success → position closed + new
    trade approved
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.risk_agent import RiskManagerAgent
from src.data_store.position_monitor import PositionMonitor
from src.data_store.positions import Position, PositionRepository
from src.safety.audit_ledger import AuditLedger


def _make_position(asset="BTC-USD", direction="long", qty=0.01,
                   entry_price=50000.0, stop_loss=49000.0,
                   take_profit=52000.0):
    return Position(
        asset=asset,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        qty=qty,
        risk_usd=0.5,
        entry_ts=time.time() - 3600,
        strategy="test",
    )


class _FailingBroker:
    """Broker that always raises on create_market_order."""
    def create_market_order(self, symbol, side, qty=None, **kwargs):
        raise RuntimeError("simulated_network_error")


class _RejectingBroker:
    """Broker that returns a 'failed' status dict (no exception)."""
    def create_market_order(self, symbol, side, qty=None, **kwargs):
        return {"id": None, "status": "failed", "error": "INSUFFICIENT_FUNDS"}


class _SuccessBroker:
    def __init__(self):
        self.calls = []
    def create_market_order(self, symbol, side, qty=None, **kwargs):
        self.calls.append({"symbol": symbol, "side": side, "qty": qty})
        return {"id": "FAKE_OK", "status": "filled"}


def _live_mode_override_path(tmpdir) -> str:
    """Sprint 46N: `_execute_close`/`_try_replace_position` now skip the
    real broker entirely in PAPER mode (audit finding C2) — and default
    to paper if `mode_override.json` is missing (same fail-safe as
    `ExecutionNode`). These C4 tests are specifically about what
    happens when a REAL broker call is attempted and fails, so they
    need an explicit, isolated "live" override file — otherwise they'd
    silently read whatever `audit/mode_override.json` happens to say
    in Carlos's actual repo checkout (or "paper" if absent), which
    would make broker.create_market_order never even get called."""
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": True}, f)
    return path


class PositionMonitorCloseFailureTest(unittest.TestCase):
    """
    C4 fix: if the broker fails to close a position, the position
    must remain open in the repo. MandateGate, PositionMonitor, and
    P&L calculations continue to treat it as live.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.event_bus = MagicMock()
        self.pos = _make_position()
        self.repo.add_open(self.pos)

    def _make_monitor(self, broker):
        return PositionMonitor(
            repo=self.repo,
            audit=self.audit,
            event_bus=self.event_bus,
            broker=broker,
            # Sprint 46N: these tests exercise a broker call that's
            # actually attempted, so force LIVE mode explicitly (see
            # _live_mode_override_path's docstring).
            mode_override_path=_live_mode_override_path(self.tmpdir),
        )

    def test_broker_exception_keeps_position_open(self):
        """The audit's claim: even with broker errors, position was marked closed."""
        monitor = self._make_monitor(_FailingBroker())
        result = monitor._execute_close(self.pos, price=50500.0, reason="STOP_HIT")
        # C4: must return None because the broker failed
        self.assertIsNone(result, "Broker failure should return None, not the closed position")
        # C4: position must STILL be in the repo
        self.assertEqual(self.repo.count_open(), 1, "Position must remain open after broker failure")
        opened = [p for p in self.repo.open() if p.position_id == self.pos.position_id]
        self.assertEqual(len(opened), 1, "Original position must be unchanged")

    def test_broker_exception_logs_close_failed_audit(self):
        monitor = self._make_monitor(_FailingBroker())
        monitor._execute_close(self.pos, price=50500.0, reason="STOP_HIT")
        close_failed = [e for e in self.audit_events if e[0] == "CLOSE_FAILED"]
        self.assertEqual(len(close_failed), 1)
        self.assertEqual(close_failed[0][1]["position_id"], self.pos.position_id)
        self.assertEqual(close_failed[0][1]["reason_attempted"], "STOP_HIT")
        self.assertEqual(close_failed[0][1]["action"], "position_remains_open")
        # TRADE_CLOSED must NOT be in audit
        closed = [e for e in self.audit_events if e[0] == "TRADE_CLOSED"]
        self.assertEqual(len(closed), 0, "No TRADE_CLOSED audit on broker failure")

    def test_broker_exception_publishes_system_error(self):
        """
        C4 + C6 fix: broker-side failures must publish SYSTEM_ERROR so
        NotificationAgent alerts via Telegram regardless of paper/live.
        """
        monitor = self._make_monitor(_FailingBroker())
        monitor._execute_close(self.pos, price=50500.0, reason="STOP_HIT")
        publishes = [c.args[0] for c in self.event_bus.publish.call_args_list]
        self.assertIn("SYSTEM_ERROR", publishes)
        # Find the SYSTEM_ERROR publish and verify it has the right kind
        sys_errs = [c.args[1] for c in self.event_bus.publish.call_args_list if c.args[0] == "SYSTEM_ERROR"]
        self.assertEqual(len(sys_errs), 1)
        self.assertEqual(sys_errs[0]["kind"], "CLOSE_FAILED")
        self.assertEqual(sys_errs[0]["asset"], "BTC-USD")

    def test_broker_failed_status_keeps_position_open(self):
        """If broker returns status='failed' (no exception), same handling."""
        monitor = self._make_monitor(_RejectingBroker())
        result = monitor._execute_close(self.pos, price=50500.0, reason="TP_HIT")
        self.assertIsNone(result)
        self.assertEqual(self.repo.count_open(), 1, "Position must remain open on broker status=failed")

    def test_broker_success_closes_position(self):
        """Happy path: broker accepts, position is closed locally."""
        monitor = self._make_monitor(_SuccessBroker())
        result = monitor._execute_close(self.pos, price=50500.0, reason="TP_HIT")
        self.assertIsNotNone(result, "Successful close should return the closed position")
        self.assertEqual(self.repo.count_open(), 0, "Position should be removed from open")
        closed = [e for e in self.audit_events if e[0] == "TRADE_CLOSED"]
        self.assertEqual(len(closed), 1)

    def test_no_broker_closes_position(self):
        """Paper mode: no broker → close_position called directly (existing behavior)."""
        monitor = self._make_monitor(broker=None)
        result = monitor._execute_close(self.pos, price=50500.0, reason="TP_HIT")
        self.assertIsNotNone(result)
        self.assertEqual(self.repo.count_open(), 0)


class RiskAgentReplacementFailureTest(unittest.TestCase):
    """
    C4 fix: position replacement must abort cleanly if the broker
    can't close the existing position. The repo must NOT mark the
    position as closed (so PositionMonitor keeps watching it),
    and the replacement must report failure (so the new trade
    is NOT approved into the now-empty slot).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.event_bus = MagicMock()
        # Worst position: ETH-USD long, currently underwater
        self.worst = Position(
            asset="ETH-USD",
            direction="long",
            entry_price=3000.0,
            stop_loss=2950.0,
            take_profit=3150.0,
            qty=0.01,
            risk_usd=0.5,
            entry_ts=time.time() - 86400,
            strategy="old_loser",
        )
        self.repo.add_open(self.worst)
        # Second position (so we have 2 open, hitting max_open_trades=2)
        self.winner = Position(
            asset="GLD",
            direction="long",
            entry_price=180.0,
            stop_loss=178.0,
            take_profit=185.0,
            qty=0.1,
            risk_usd=0.2,
            entry_ts=time.time() - 86400 * 3,
            strategy="old_winner",
        )
        self.repo.add_open(self.winner)

    def _new_trade(self):
        return {
            "asset": "BTC-USD",
            "direction": "long",
            "position_size": 0.001,
            "entry_price": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "notional_usd": 50.0,
            "risk_usd": 0.5,
            "strategy": "momentum",
        }

    def _new_score(self, pos, agent):
        return 0.95  # very high — would normally trigger replacement

    def _score_worst(self, pos, agent):
        return 0.10  # low

    def _new_trade(self):
        return {
            "asset": "BTC-USD",
            "direction": "long",
            "position_size": 0.001,
            "entry_price": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "notional_usd": 50.0,
            "risk_usd": 0.5,
            "strategy": "momentum",
        }

    def _new_hyp(self):
        # High-quality new hypothesis that should trigger replacement
        return {
            "asset": "BTC-USD",
            "strategy": "momentum",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 500.0,
            "expected_move_pct": 5.0,  # strong expected move
        }

    def _new_score_inputs(self):
        return {
            "entry_price": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "atr_at_signal": 500.0,
            "expected_move_pct": 5.0,
        }

    def test_replacement_broker_failure_keeps_position_open(self):
        """If broker fails, replacement must NOT close the position in repo."""
        broker = _FailingBroker()
        agent = RiskManagerAgent(
            broker_client=broker,
            audit=self.audit,
            position_repo=self.repo,
            event_bus=self.event_bus,
            max_open_trades=2,
            enable_position_replacement=True,
            replacement_score_threshold=0.20,
            current_prices={"ETH-USD": 2900.0, "GLD": 182.0},
            # Sprint 45: network-dependent portfolio gates off in this pre-existing test (not what it's testing).
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            # Sprint 46N: force LIVE mode explicitly — see
            # _live_mode_override_path's docstring.
            mode_override_path=_live_mode_override_path(self.tmpdir),
)
        # Patch score methods so the replacement is triggered
        agent.score_position = MagicMock(side_effect=lambda p, current_price=None: 0.10 if p.asset == "ETH-USD" else 0.50)
        result = agent._try_replace_position(
            new_hyp=self._new_hyp(),
            new_trade=self._new_trade(),
            new_score_inputs=self._new_score_inputs(),
        )
        # C4: replacement must FAIL because broker failed
        self.assertFalse(result, "Replacement must return False on broker failure")
        # C4: the worst position is STILL open
        self.assertEqual(
            self.repo.count_open(), 2,
            "Worst position must remain open after broker failure (C4 fix)",
        )
        opens = self.repo.open()
        eth_open = any(p.asset == "ETH-USD" for p in opens)
        self.assertTrue(eth_open, "ETH-USD must still be in the repo")

    def test_replacement_broker_failure_logs_replacement_failed(self):
        broker = _FailingBroker()
        agent = RiskManagerAgent(
            broker_client=broker,
            audit=self.audit,
            position_repo=self.repo,
            event_bus=self.event_bus,
            max_open_trades=2,
            enable_position_replacement=True,
            replacement_score_threshold=0.20,
            current_prices={"ETH-USD": 2900.0, "GLD": 182.0},
            # Sprint 45: network-dependent portfolio gates off in this pre-existing test (not what it's testing).
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            # Sprint 46N: force LIVE mode explicitly — see
            # _live_mode_override_path's docstring.
            mode_override_path=_live_mode_override_path(self.tmpdir),
)
        agent.score_position = MagicMock(side_effect=lambda p, current_price=None: 0.10 if p.asset == "ETH-USD" else 0.50)
        agent._try_replace_position(
            new_hyp=self._new_hyp(),
            new_trade=self._new_trade(),
            new_score_inputs=self._new_score_inputs(),
        )
        rep_failed = [e for e in self.audit_events if e[0] == "REPLACEMENT_FAILED"]
        self.assertEqual(len(rep_failed), 1)
        self.assertEqual(rep_failed[0][1]["worst_asset"], "ETH-USD")
        self.assertEqual(rep_failed[0][1]["action"], "worst_position_remains_open")
        # POSITION_REPLACED must NOT be in audit
        replaced = [e for e in self.audit_events if e[0] == "POSITION_REPLACED"]
        self.assertEqual(len(replaced), 0, "No POSITION_REPLACED on broker failure")

    def test_replacement_broker_failure_publishes_system_error(self):
        broker = _FailingBroker()
        agent = RiskManagerAgent(
            broker_client=broker,
            audit=self.audit,
            position_repo=self.repo,
            event_bus=self.event_bus,
            max_open_trades=2,
            enable_position_replacement=True,
            replacement_score_threshold=0.20,
            current_prices={"ETH-USD": 2900.0, "GLD": 182.0},
            # Sprint 45: network-dependent portfolio gates off in this pre-existing test (not what it's testing).
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            # Sprint 46N: force LIVE mode explicitly — see
            # _live_mode_override_path's docstring.
            mode_override_path=_live_mode_override_path(self.tmpdir),
)
        agent.score_position = MagicMock(side_effect=lambda p, current_price=None: 0.10 if p.asset == "ETH-USD" else 0.50)
        agent._try_replace_position(
            new_hyp=self._new_hyp(),
            new_trade=self._new_trade(),
            new_score_inputs=self._new_score_inputs(),
        )
        publishes = [c.args[0] for c in self.event_bus.publish.call_args_list]
        self.assertIn("SYSTEM_ERROR", publishes)
        sys_errs = [c.args[1] for c in self.event_bus.publish.call_args_list if c.args[0] == "SYSTEM_ERROR"]
        self.assertEqual(len(sys_errs), 1)
        self.assertEqual(sys_errs[0]["kind"], "REPLACEMENT_FAILED")

    def test_replacement_broker_success_closes_position(self):
        """Happy path: broker accepts close, position is closed locally."""
        broker = _SuccessBroker()
        agent = RiskManagerAgent(
            broker_client=broker,
            audit=self.audit,
            position_repo=self.repo,
            event_bus=self.event_bus,
            max_open_trades=2,
            enable_position_replacement=True,
            replacement_score_threshold=0.20,
            current_prices={"ETH-USD": 2900.0, "GLD": 182.0},
            # Sprint 45: network-dependent portfolio gates off in this pre-existing test (not what it's testing).
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            # Sprint 46N: force LIVE mode explicitly — see
            # _live_mode_override_path's docstring.
            mode_override_path=_live_mode_override_path(self.tmpdir),
)
        agent.score_position = MagicMock(side_effect=lambda p, current_price=None: 0.10 if p.asset == "ETH-USD" else 0.50)
        result = agent._try_replace_position(
            new_hyp=self._new_hyp(),
            new_trade=self._new_trade(),
            new_score_inputs=self._new_score_inputs(),
        )
        self.assertTrue(result, "Successful replacement should return True")
        self.assertEqual(self.repo.count_open(), 1, "Worst position closed, winner remains")
        opens = self.repo.open()
        self.assertEqual(opens[0].asset, "GLD", "Only the winner should remain")
        replaced = [e for e in self.audit_events if e[0] == "POSITION_REPLACED"]
        self.assertEqual(len(replaced), 1)

    def test_replacement_no_broker_works(self):
        """Paper mode: no broker, replacement proceeds with local close."""
        agent = RiskManagerAgent(
            broker_client=None,
            audit=self.audit,
            position_repo=self.repo,
            event_bus=self.event_bus,
            max_open_trades=2,
            enable_position_replacement=True,
            replacement_score_threshold=0.20,
            current_prices={"ETH-USD": 2900.0, "GLD": 182.0},
            # Sprint 45: network-dependent portfolio gates off in this pre-existing test (not what it's testing).
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
)
        agent.score_position = MagicMock(side_effect=lambda p, current_price=None: 0.10 if p.asset == "ETH-USD" else 0.50)
        result = agent._try_replace_position(
            new_hyp=self._new_hyp(),
            new_trade=self._new_trade(),
            new_score_inputs=self._new_score_inputs(),
        )
        self.assertTrue(result)
        self.assertEqual(self.repo.count_open(), 1)


if __name__ == "__main__":
    unittest.main()
