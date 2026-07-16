"""
Wiring test: RiskManagerAgent.category_scoring_enabled applies the
asset-class historical win-rate multiplier to position sizing.

Companion to tests/test_category_scoring.py (which tests the pure
scoring functions in isolation). This test drives the actual
validate_and_size() call path, following the same fixture pattern
as tests/test_risk_agent_sprint18.py.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.risk_agent import RiskManagerAgent
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository


class _FakeBroker:
    def get_usdt_balance(self):
        return 1000.0

    @property
    def exchange(self):
        class _Ex:
            options = {"sandboxMode": True}
        return _Ex()

    def create_market_order(self, symbol, side, qty):
        return {"id": "fake", "symbol": symbol, "side": side, "qty": qty}


class _FakeDecisionLog:
    """Stands in for src.safety.decision_log.DecisionLog — only
    implements what asset_category_multiplier's caller needs."""
    def __init__(self, outcome_records):
        self._records = outcome_records

    def all_decisions(self):
        return list(self._records)


def _crypto_outcome(pnl_usd):
    return {"kind": "outcome", "asset": "BTC-USD", "pnl_usd": pnl_usd, "pnl_pct": pnl_usd}


def _make_agent(tmpdir, decision_log, category_scoring_enabled):
    audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
    repo = PositionRepository(os.path.join(tmpdir, "positions.json"))
    return RiskManagerAgent(
        broker_client=_FakeBroker(),
        risk_per_trade_pct=1.0,
        max_capital_per_trade_pct=50.0,
        atr_stop_multiplier=2.0,
        min_order_usd=10.0,
        audit=audit,
        position_repo=repo,
        correlation_check_enabled=False,
        tail_risk_check_enabled=False,
        category_scoring_enabled=category_scoring_enabled,
        category_scoring_min_trades=10,
        category_scoring_poor_win_rate=0.35,
        category_scoring_reduction_factor=0.5,
        decision_log=decision_log,
    ), audit


class CategoryScoringDisabledByDefaultTest(unittest.TestCase):
    def test_default_off_ignores_bad_track_record(self):
        tmpdir = tempfile.mkdtemp()
        bad_history = _FakeDecisionLog([_crypto_outcome(-1.0)] * 20)
        agent, _audit = _make_agent(tmpdir, bad_history, category_scoring_enabled=False)
        hypothesis = {
            "asset": "BTC-USD", "strategy": "RSI_MeanReversion", "direction": "long",
            "price": 50000.0, "atr_at_signal": 750.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)
        self.assertEqual(len(result["approved_trades"]), 1)
        # No CATEGORY_SIZE_REDUCED event should exist since the gate is off.
        events = [e for e in _audit.read_all() if e.get("event_type") == "CATEGORY_SIZE_REDUCED"]
        self.assertEqual(events, [])


class CategoryScoringEnabledTest(unittest.TestCase):
    def test_poor_track_record_shrinks_notional_and_logs_event(self):
        tmpdir = tempfile.mkdtemp()
        bad_history = _FakeDecisionLog([_crypto_outcome(-1.0)] * 20)  # 0% win rate, 20 trades
        agent, audit = _make_agent(tmpdir, bad_history, category_scoring_enabled=True)
        hypothesis = {
            "asset": "BTC-USD", "strategy": "RSI_MeanReversion", "direction": "long",
            "price": 50000.0, "atr_at_signal": 750.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)
        events = [e for e in audit.read_all() if e.get("event_type") == "CATEGORY_SIZE_REDUCED"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["multiplier"], 0.5)
        self.assertEqual(events[0]["asset_class"], "crypto")

    def test_good_track_record_does_not_shrink_or_boost(self):
        tmpdir = tempfile.mkdtemp()
        good_history = _FakeDecisionLog([_crypto_outcome(1.0)] * 18 + [_crypto_outcome(-1.0)] * 2)
        agent, audit = _make_agent(tmpdir, good_history, category_scoring_enabled=True)
        hypothesis = {
            "asset": "BTC-USD", "strategy": "RSI_MeanReversion", "direction": "long",
            "price": 50000.0, "atr_at_signal": 750.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        agent.validate_and_size({}, state)
        events = [e for e in audit.read_all() if e.get("event_type") == "CATEGORY_SIZE_REDUCED"]
        self.assertEqual(events, [])  # 90% win rate never triggers a reduction

    def test_too_few_trades_does_not_shrink(self):
        tmpdir = tempfile.mkdtemp()
        thin_history = _FakeDecisionLog([_crypto_outcome(-1.0)] * 3)  # 0% win rate but only 3 trades
        agent, audit = _make_agent(tmpdir, thin_history, category_scoring_enabled=True)
        hypothesis = {
            "asset": "BTC-USD", "strategy": "RSI_MeanReversion", "direction": "long",
            "price": 50000.0, "atr_at_signal": 750.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        agent.validate_and_size({}, state)
        events = [e for e in audit.read_all() if e.get("event_type") == "CATEGORY_SIZE_REDUCED"]
        self.assertEqual(events, [])


class CategoryScoringFailsOpenTest(unittest.TestCase):
    def test_decision_log_error_does_not_block_trade(self):
        """If the decision log blows up, sizing must proceed unaffected --
        this is a size adjustment, not a pass/fail gate."""
        class _BrokenDecisionLog:
            def all_decisions(self):
                raise RuntimeError("disk error")

        tmpdir = tempfile.mkdtemp()
        agent, audit = _make_agent(tmpdir, _BrokenDecisionLog(), category_scoring_enabled=True)
        hypothesis = {
            "asset": "BTC-USD", "strategy": "RSI_MeanReversion", "direction": "long",
            "price": 50000.0, "atr_at_signal": 750.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)
        self.assertEqual(len(result["approved_trades"]), 1)


if __name__ == "__main__":
    unittest.main()
