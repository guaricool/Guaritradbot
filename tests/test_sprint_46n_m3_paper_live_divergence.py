"""
Sprint 46N — audit M3: paper/live divergence.

Two independent fixes, each with its own test class:

A. RiskManagerAgent: equity "short" hypotheses (SPY/QQQ/GLD/USO) used to
   pass every gate and simulate a clean fill in PAPER mode -- but Alpaca
   cannot open a short position via fractional/notional orders (this
   bot's equity sizing always uses notional_usd), so the exact same
   trade is a guaranteed rejection in LIVE mode. Mirrors the existing
   Sprint 46M crypto-short gate (tests/test_risk_agent_sprint46m.py).

B. ExecutionNode: simulated fills (paper mode AND the no-broker dev
   path) were always recorded at the EXACT signal price with zero
   slippage -- optimistic by construction. `_apply_paper_slippage` now
   shifts the recorded fill price against the trader by
   `paper_slippage_pct` (longs fill higher, shorts fill lower). Default
   0.0 preserves the old exact-price behavior for any caller that
   doesn't opt in.

Run: python -m unittest tests.test_sprint_46n_m3_paper_live_divergence -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.risk_agent import RiskManagerAgent
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository
from src.execution.execution_node import ExecutionNode
from src.core.event_bus import EventBus


# ============================================================
# A. RiskManagerAgent -- equity short gate
# ============================================================

_BROKERS_CONFIG = {
    "crypto": {"symbols": ["BTC-USD", "ETH-USD", "SOL-USD"]},
    "equity": {"symbols": ["SPY", "QQQ", "GLD", "USO"]},
}


def _make_agent(tmpdir, **overrides):
    audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
    repo = PositionRepository(os.path.join(tmpdir, "positions.json"))
    kwargs = dict(
        risk_per_trade_pct=1.0,
        max_capital_per_trade_pct=50.0,
        atr_stop_multiplier=2.0,
        min_order_usd=10.0,
        audit=audit,
        position_repo=repo,
        correlation_check_enabled=False,
        tail_risk_check_enabled=False,
        portfolio_stress_check=False,
        asset_concentration_check=False,
        brokers_config=_BROKERS_CONFIG,
    )
    kwargs.update(overrides)
    return RiskManagerAgent(**kwargs), audit, repo


class EquityShortBlockedTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _spy_short_hypothesis(self):
        return {
            "asset": "SPY",
            "strategy": "Resistance_Fade",
            "direction": "short",
            "price": 500.0,
            "atr_at_signal": 3.0,
        }

    def test_equity_short_rejected_by_default(self):
        agent, audit, repo = _make_agent(self.tmpdir)
        state = {"generate_hypotheses": {"hypotheses": [self._spy_short_hypothesis()]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(len(result["approved_trades"]), 0)
        self.assertEqual(len(result["rejected_trades"]), 1)
        self.assertEqual(
            result["rejected_trades"][0]["reason"], "equity_short_not_supported"
        )

    def test_equity_short_allowed_when_opted_in(self):
        agent, audit, repo = _make_agent(self.tmpdir, allow_equity_short=True)
        state = {"generate_hypotheses": {"hypotheses": [self._spy_short_hypothesis()]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(
            len(result["approved_trades"]), 1,
            f"Expected approval with allow_equity_short=True, got {result['rejected_trades']}",
        )
        self.assertEqual(result["approved_trades"][0]["direction"], "short")

    def test_crypto_short_unaffected_by_equity_gate(self):
        """BTC-USD shorts must still be caught by the (separate, existing)
        crypto-short gate -- not silently let through by the new equity
        gate, and not double-rejected either."""
        agent, audit, repo = _make_agent(self.tmpdir)
        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "Resistance_Fade",
            "direction": "short",
            "price": 64000.0,
            "atr_at_signal": 200.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(len(result["rejected_trades"]), 1)
        self.assertEqual(
            result["rejected_trades"][0]["reason"], "crypto_short_not_supported"
        )

    def test_equity_short_not_blocked_when_no_brokers_config(self):
        """Backward compat: an agent built WITHOUT brokers_config (every
        pre-M3 call site) has an empty asset_to_class map, so the new
        gate can never resolve an asset to "equity" and must not
        change behavior for existing callers/tests."""
        agent, audit, repo = _make_agent(self.tmpdir, brokers_config=None)
        state = {"generate_hypotheses": {"hypotheses": [self._spy_short_hypothesis()]}}
        result = agent.validate_and_size({}, state)

        reasons = [r.get("reason") for r in result["rejected_trades"]]
        self.assertNotIn("equity_short_not_supported", reasons)


# ============================================================
# B. ExecutionNode -- paper-mode slippage
# ============================================================

def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


class PaperSlippageTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.bus = EventBus()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.audit_events = []
        self.audit = MagicMock()
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))

    def _order(self, direction="long", entry_price=50000.0):
        return {
            "asset": "BTC-USD",
            "strategy": "momentum",
            "direction": direction,
            "position_size": 0.001,
            "entry_price": entry_price,
            "stop_loss": 49000,
            "take_profit": 52000,
            "risk_usd": 1.0,
        }

    def test_zero_slippage_preserves_exact_price(self):
        """Default (paper_slippage_pct=0.0) must be byte-for-byte the old
        behavior -- the fill price recorded is exactly the signal price."""
        node = ExecutionNode(
            self.bus,
            brokers_config={"crypto": {"symbols": ["BTC-USD"]}},
            broker_client=None,
            audit=self.audit,
            position_repo=self.repo,
            mode_override_path=_write_mode_override(self.tmpdir, False),
            paper_slippage_pct=0.0,
        )
        node.execute_order(self._order(direction="long", entry_price=50000.0))
        opens = self.repo.open()
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0].entry_price, 50000.0)

    def test_long_fill_slips_higher_no_broker_path(self):
        node = ExecutionNode(
            self.bus,
            brokers_config={"crypto": {"symbols": ["BTC-USD"]}},
            broker_client=None,
            audit=self.audit,
            position_repo=self.repo,
            mode_override_path=_write_mode_override(self.tmpdir, False),
            paper_slippage_pct=0.001,  # 0.1%
        )
        node.execute_order(self._order(direction="long", entry_price=50000.0))
        opens = self.repo.open()
        self.assertEqual(len(opens), 1)
        self.assertAlmostEqual(opens[0].entry_price, 50000.0 * 1.001, places=6)
        # Audit reflects the slipped fill price, with the original
        # requested price preserved separately for transparency.
        fills = [e for e in self.audit_events if e[0] == "TRADE_FILLED"]
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0][1]["entry_price"], 50000.0 * 1.001, places=6)
        self.assertEqual(fills[0][1]["requested_entry_price"], 50000.0)

    def test_short_fill_slips_lower_no_broker_path(self):
        node = ExecutionNode(
            self.bus,
            brokers_config={"crypto": {"symbols": ["BTC-USD"]}},
            broker_client=None,
            audit=self.audit,
            position_repo=self.repo,
            mode_override_path=_write_mode_override(self.tmpdir, False),
            paper_slippage_pct=0.001,
        )
        node.execute_order(self._order(direction="short", entry_price=50000.0))
        opens = self.repo.open()
        self.assertEqual(len(opens), 1)
        self.assertAlmostEqual(opens[0].entry_price, 50000.0 * 0.999, places=6)

    def test_paper_mode_with_broker_configured_also_gets_slippage(self):
        """Paper mode (mandate_enabled=false) with a REAL broker object
        configured must still simulate the fill locally (never call the
        broker -- that's the pre-existing B033 gate) AND apply
        slippage, same as the no-broker path."""
        fake_broker = MagicMock()
        node = ExecutionNode(
            self.bus,
            brokers_config={"crypto": {"symbols": ["BTC-USD"]}},
            broker_client=fake_broker,
            audit=self.audit,
            position_repo=self.repo,
            mode_override_path=_write_mode_override(self.tmpdir, False),  # paper
            paper_slippage_pct=0.001,
        )
        node.execute_order(self._order(direction="long", entry_price=50000.0))
        fake_broker.create_market_order.assert_not_called()
        opens = self.repo.open()
        self.assertEqual(len(opens), 1)
        self.assertAlmostEqual(opens[0].entry_price, 50000.0 * 1.001, places=6)

    def test_apply_paper_slippage_helper_directly(self):
        node = ExecutionNode(self.bus, paper_slippage_pct=0.01)
        self.assertAlmostEqual(node._apply_paper_slippage(100.0, "long"), 101.0, places=6)
        self.assertAlmostEqual(node._apply_paper_slippage(100.0, "short"), 99.0, places=6)
        # Defensive: non-positive slippage/price never raises, never improves.
        node.paper_slippage_pct = 0.0
        self.assertEqual(node._apply_paper_slippage(100.0, "long"), 100.0)
        self.assertEqual(node._apply_paper_slippage(-5.0, "long"), -5.0)


if __name__ == "__main__":
    unittest.main()
