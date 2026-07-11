"""
Sprint 46N tests — audit finding A2 (AUDITORIA_COMPLETA_2026-07-11.md).

A2: `risk_agent.py`'s min_order_usd auto-adjust (Sprint 18) bumps a
too-small trade's notional up to `min_order_usd` whenever the raw
risk-sized notional (risk_amount_usd / stop_distance * entry_price)
would fall below it — necessary, since exchange minimums exist
regardless of what risk_per_trade_pct would size. But bumping the
notional also bumps the QUANTITY, which multiplies the EFFECTIVE risk
(quantity * stop_distance) above what risk_per_trade_pct actually
intended — silently, with only a CAP_AUTO_ADJUSTED audit breadcrumb
and no enforcement.

Audit's concrete example: $20 account, 1% risk ($0.20 intended),
2xATR stop of 4-10% (typical crypto) → effective risk becomes
$0.40-$1.00 per trade = 2-5x intended. Five simultaneously-inflated
positions could risk ~25% of the account while risk_per_trade_pct=1%
suggests 5%.

Fix: after the auto-adjust, if the resulting effective risk exceeds
`max_auto_adjust_risk_multiplier` (config.yaml `trading.
max_auto_adjust_risk_multiplier`, default 2.0) times the originally
intended risk_amount_usd, reject the trade (CAP_AUTO_ADJUSTED_REJECTED)
instead of opening it.

Run: python -m unittest tests.test_sprint_46n_a2_risk_multiplier_cap -v
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
    def __init__(self, balance=20.0):
        self._balance = balance
        self.orders = []

    def get_usdt_balance(self):
        return self._balance

    @property
    def exchange(self):
        class _Ex:
            options = {"sandboxMode": True}
        return _Ex()

    def create_market_order(self, symbol, side, qty):
        self.orders.append({"symbol": symbol, "side": side, "qty": qty})
        return {"id": "fake", "symbol": symbol, "side": side, "qty": qty}


def _make_agent(**overrides):
    tmpdir = tempfile.mkdtemp()
    audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
    repo = PositionRepository(os.path.join(tmpdir, "positions.json"))
    broker = _FakeBroker(balance=overrides.pop("balance", 20.0))
    kwargs = dict(
        broker_client=broker,
        risk_per_trade_pct=1.0,
        max_capital_per_trade_pct=50.0,
        atr_stop_multiplier=2.0,
        min_order_usd=10.0,
        audit=audit,
        position_repo=repo,
        correlation_check_enabled=False,
        tail_risk_check_enabled=False,
    )
    kwargs.update(overrides)
    agent = RiskManagerAgent(**kwargs)
    return agent, audit, repo


class AuditA2ScenarioTest(unittest.TestCase):
    """Reproduce the audit's own worked example: $20 account, 1% risk,
    a wide (~8%) crypto stop -> effective risk 4x intended -> must now
    be REJECTED instead of silently opened."""

    def test_wide_stop_on_small_account_is_rejected_by_default(self):
        agent, audit, repo = _make_agent()
        # risk_amount_usd = $20 * 1% = $0.20
        # stop_distance = 2 * ATR = 2 * 2000 = $4000 (8% of $50000 entry)
        # quantity = 0.20 / 4000 = 0.00005 -> notional = $2.50 < $10
        # -> auto-adjust bumps quantity to 10/50000 = 0.0002
        # -> effective risk = 0.0002 * 4000 = $0.80 = 4.0x intended $0.20
        # -> exceeds default max_auto_adjust_risk_multiplier=2.0 -> reject
        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "RSI_MeanReversion",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 2000.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(result["approved_trades"], [])
        self.assertEqual(len(result["rejected_trades"]), 1)
        self.assertEqual(
            result["rejected_trades"][0]["reason"],
            "auto_adjust_risk_multiplier_exceeded",
        )

    def test_rejection_is_audited_with_multiplier_details(self):
        agent, audit, repo = _make_agent()
        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "RSI_MeanReversion",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 2000.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        agent.validate_and_size({}, state)

        events = audit.read_by_type("CAP_AUTO_ADJUSTED_REJECTED")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["asset"], "BTC-USD")
        self.assertEqual(event["auto_adjust_reason"], "risk_below_min_order")
        # Effective risk should be ~4x the intended $0.20.
        self.assertAlmostEqual(event["risk_multiplier"], 4.0, delta=0.01)
        self.assertAlmostEqual(event["intended_risk_usd"], 0.20, delta=0.001)
        self.assertAlmostEqual(event["effective_risk_usd"], 0.80, delta=0.001)
        self.assertEqual(event["max_allowed_multiplier"], 2.0)


class ModerateAutoAdjustStillApprovedTest(unittest.TestCase):
    """A tighter stop that only mildly inflates risk (within the 2x
    cap) must still be approved -- the gate only blocks EXCESSIVE
    multiplication, not the auto-adjust mechanism itself (Sprint 18)."""

    def test_moderate_multiplier_is_approved(self):
        agent, audit, repo = _make_agent()
        # stop_distance = 2 * 750 = $1500 (3% of entry)
        # quantity = 0.20/1500 = 0.0001333 -> notional = $6.67 < $10
        # -> bump to 10/50000 = 0.0002 -> effective risk = 0.0002*1500 = $0.30
        # -> multiplier = 0.30/0.20 = 1.5x <= 2.0 cap -> approved
        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "RSI_MeanReversion",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 750.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(len(result["approved_trades"]), 1)
        self.assertGreaterEqual(result["approved_trades"][0]["notional_usd"], 10.0)


class ConfigurableMultiplierTest(unittest.TestCase):
    """The cap is a config value (trading.max_auto_adjust_risk_multiplier)
    -- an operator who deliberately accepts higher per-trade risk on a
    small account can raise it, and the same audit-example trade that
    was rejected under the 2.0 default is then approved."""

    def test_raising_the_cap_allows_the_audit_example_trade(self):
        agent, audit, repo = _make_agent(max_auto_adjust_risk_multiplier=10.0)
        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "RSI_MeanReversion",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 2000.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(len(result["approved_trades"]), 1)

    def test_default_multiplier_is_2x(self):
        agent, audit, repo = _make_agent()
        self.assertEqual(agent.max_auto_adjust_risk_multiplier, 2.0)


class NoAutoAdjustNoGateTest(unittest.TestCase):
    """A normally-sized trade (notional already >= min_order_usd, no
    auto-adjust fired) must never be touched by this gate, regardless
    of its risk multiplier relative to some hypothetical baseline."""

    def test_normal_trade_not_subject_to_multiplier_cap(self):
        agent, audit, repo = _make_agent(balance=1000.0)
        # risk_amount_usd = 1000*1% = $10; stop_distance = 2*50=$100 (2%)
        # quantity = 10/100 = 0.1 -> notional = 0.1*500=$50 (well above min,
        # no auto-adjust needed at all)
        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "RSI_MeanReversion",
            "direction": "long",
            "price": 500.0,
            "atr_at_signal": 25.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(len(result["approved_trades"]), 1)


if __name__ == "__main__":
    unittest.main()
