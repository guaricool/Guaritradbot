"""
Regression test: the Sprint 46N/A2 risk-multiplier cap must also catch
inflation coming from the exchange lot-size/min-notional quantize block
(Sprint 46N/A3), not just from the min_order_usd auto-adjust (Sprint 18).

Bug: risk_agent.py's multiplier check at ~line 860 used to run only
`if auto_adjust_reason is not None` (i.e. only when Step 2's
min_order_usd bump fired). But the exchange-quantize block further
down can ALSO inflate quantity/notional via its own `buffered_floor`,
completely independent of whether Step 2 fired -- a trade whose raw
notional already cleared min_order_usd but not the exchange's real
min-notional could silently bypass the cap entirely. Fixed by running
the multiplier check unconditionally whenever there's an intended risk
to compare against.
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


class _FakeMarket:
    """Minimal ccxt-style exchange with a real (high) min-notional, so
    the quantize block's `buffered_floor` bump actually fires -- unlike
    the plain `_FakeBroker` used elsewhere in the suite, whose
    `.exchange` has no `market()`/`load_markets()` at all and always
    falls into the quantize block's except-and-skip path."""

    def __init__(self, min_notional):
        self.options = {"sandboxMode": True}
        self.markets = None
        self._min_notional = min_notional

    def load_markets(self):
        self.markets = {"BTC/USDT": {}}

    def market(self, symbol):
        return {"limits": {"cost": {"min": self._min_notional}}}

    def amount_to_precision(self, symbol, amount):
        # No truncation -- just pass the quantity through so the test
        # isolates the buffered_floor bump, not step-size rounding.
        return amount


class _FakeBrokerWithRealExchange:
    def __init__(self, balance, min_notional):
        self._balance = balance
        self._exchange = _FakeMarket(min_notional)

    def get_usdt_balance(self):
        return self._balance

    @property
    def exchange(self):
        return self._exchange

    def create_market_order(self, symbol, side, qty):
        return {"id": "fake", "symbol": symbol, "side": side, "qty": qty}


def _make_agent(min_notional, balance=1000.0, max_auto_adjust_risk_multiplier=2.0):
    tmpdir = tempfile.mkdtemp()
    audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
    repo = PositionRepository(os.path.join(tmpdir, "positions.json"))
    broker = _FakeBrokerWithRealExchange(balance, min_notional)
    agent = RiskManagerAgent(
        broker_client=broker,
        risk_per_trade_pct=1.0,
        max_capital_per_trade_pct=50.0,
        atr_stop_multiplier=2.0,
        min_order_usd=10.0,
        max_auto_adjust_risk_multiplier=max_auto_adjust_risk_multiplier,
        audit=audit,
        position_repo=repo,
        correlation_check_enabled=False,
        tail_risk_check_enabled=False,
    )
    return agent, audit, repo


class ExchangeQuantizeBypassFixedTest(unittest.TestCase):
    def test_high_exchange_min_notional_inflating_risk_is_rejected(self):
        # balance=$1000, 1% risk -> risk_amount_usd = $10
        # entry=$500, atr=25 -> stop_distance = 2*25 = $50 (10%)
        # quantity = 10/50 = 0.2 -> notional = 0.2*500 = $100
        # $100 > min_order_usd=$10, so Step 2 (auto_adjust_reason) never
        # fires -- but the exchange's real min-notional is $500, so the
        # quantize block's buffered_floor = max(10, 500)*1.05 = $525
        # bumps quantity to 525/500 = 1.05 -> effective risk =
        # 1.05*50 = $52.5 = 5.25x the intended $10, over the 2.0x cap.
        agent, audit, repo = _make_agent(min_notional=500.0)
        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "RSI_MeanReversion",
            "direction": "long",
            "price": 500.0,
            "atr_at_signal": 25.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(result["approved_trades"], [])
        self.assertEqual(len(result["rejected_trades"]), 1)
        self.assertEqual(
            result["rejected_trades"][0]["reason"],
            "auto_adjust_risk_multiplier_exceeded",
        )
        events = audit.read_by_type("CAP_AUTO_ADJUSTED_REJECTED")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["auto_adjust_reason"], "exchange_min_notional_quantize")
        self.assertAlmostEqual(events[0]["risk_multiplier"], 5.25, delta=0.01)

    def test_low_exchange_min_notional_is_unaffected(self):
        """Sanity check: when the exchange's min-notional is at/below
        min_order_usd, the quantize block doesn't bump anything beyond
        what Step 1/2 already handled, and the trade is approved."""
        agent, audit, repo = _make_agent(min_notional=5.0)
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
        events = audit.read_by_type("CAP_AUTO_ADJUSTED_REJECTED")
        self.assertEqual(events, [])

    def test_raising_cap_allows_the_high_min_notional_trade(self):
        agent, audit, repo = _make_agent(min_notional=500.0, max_auto_adjust_risk_multiplier=10.0)
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
