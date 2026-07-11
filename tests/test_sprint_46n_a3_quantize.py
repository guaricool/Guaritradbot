"""
Sprint 46N tests — audit finding A3 (AUDITORIA_COMPLETA_2026-07-11.md).

A3: `broker.py:88-100` sent a raw, unquantized quantity straight to
`exchange.create_market_order` -- and `risk_agent.py` only rounded the
sized quantity to 8 decimals, never checking the exchange's real lot
step-size or min-notional. Dimensioning exactly at $10.00 (the stable-
state config: 50% of a $20 balance, which equals binance.us's
MIN_NOTIONAL) means any truncation by the exchange's own step-size
rounding leaves the real notional under $10 -> the exchange rejects
the order at send time, well after all the sizing/gating work already
ran. The native-OCO protection path already used
`exchange.amount_to_precision` (broker.py's `create_oco_sell_order`);
the entry-order path never did.

Fix (matches the audit's suggested correction):
1. RiskManagerAgent.validate_and_size, for CRYPTO hypotheses only, now
   re-sizes to max(min_order_usd, exchange_min_notional) x 1.05 if the
   currently-sized notional is below that buffered floor, then
   quantizes the quantity via `exchange.amount_to_precision` and
   re-verifies the notional AFTER quantization -- rejecting
   (`below_exchange_min_notional_after_quantize`) if it's still short.
2. BrokerClient.create_market_order also quantizes via
   `amount_to_precision` as defense-in-depth for every other caller
   (position closes/replacements) that passes an already-sized qty
   through unchanged.

Both fixes fail open: any error while consulting the exchange (markets
not loaded, symbol not found, network hiccup) logs a warning
(QUANTIZE_SKIPPED) and falls back to the pre-existing 8-decimal
rounding -- a quantization problem must never silently block an
otherwise-valid trade.

Run: python -m unittest tests.test_sprint_46n_a3_quantize -v
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


class _FakeExchange:
    """Minimal ccxt-like exchange stub for testing the A3 quantization
    block in isolation, without a real ccxt/network dependency."""

    def __init__(self, min_notional=None, step_size=None, markets_loaded=True,
                 raise_on_market=False, raise_on_precision=False):
        self.markets = {"BTC/USD": {}} if markets_loaded else {}
        self._min_notional = min_notional
        self._step_size = step_size
        self._raise_on_market = raise_on_market
        self._raise_on_precision = raise_on_precision
        self.load_markets_called = False
        self.options = {"sandboxMode": True}

    def load_markets(self):
        self.load_markets_called = True
        self.markets = {"BTC/USD": {}}

    def market(self, symbol):
        if self._raise_on_market:
            raise KeyError(f"no such symbol: {symbol}")
        limits = {}
        if self._min_notional is not None:
            limits = {"cost": {"min": self._min_notional}}
        return {"limits": limits}

    def amount_to_precision(self, symbol, amount):
        if self._raise_on_precision:
            raise ValueError("precision error (simulated)")
        if self._step_size:
            steps = int(amount / self._step_size)
            return str(round(steps * self._step_size, 10))
        return str(amount)


class _FakeBroker:
    def __init__(self, balance=1000.0, exchange=None):
        self._balance = balance
        self.exchange = exchange or _FakeExchange()

    def get_usdt_balance(self):
        return self._balance


def _make_agent(exchange=None, balance=1000.0, **overrides):
    tmpdir = tempfile.mkdtemp()
    audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
    repo = PositionRepository(os.path.join(tmpdir, "positions.json"))
    broker = _FakeBroker(balance=balance, exchange=exchange)
    kwargs = dict(
        broker_client=broker,
        risk_per_trade_pct=1.0,
        max_capital_per_trade_pct=10.0,
        atr_stop_multiplier=2.0,
        min_order_usd=10.0,
        audit=audit,
        position_repo=repo,
        correlation_check_enabled=False,
        tail_risk_check_enabled=False,
    )
    kwargs.update(overrides)
    agent = RiskManagerAgent(**kwargs)
    return agent, audit, repo, broker


def _btc_hypothesis(price=50000.0, atr=100.0):
    return {
        "asset": "BTC-USD",
        "strategy": "test",
        "direction": "long",
        "price": price,
        "atr_at_signal": atr,
    }


class QuantizationAppliedTest(unittest.TestCase):
    """A crypto trade's sized quantity gets truncated to the exchange's
    real lot step-size, and the resulting (smaller) notional is used
    -- not the naive `round(quantity, 8)` from before this fix."""

    def test_quantity_truncated_to_step_size(self):
        exch = _FakeExchange(min_notional=10.0, step_size=0.0015)
        agent, audit, repo, broker = _make_agent(exchange=exch)
        # balance=$1000, risk=1% -> risk_amount_usd=$10; stop_distance=
        # max(2*100, 50000*0.005)=250 -> quantity=10/250=0.04 ->
        # notional=$2000, capped by max_capital_per_trade_pct=10% of
        # $1000=$100 -> quantity=100/50000=0.002 (before quantization).
        state = {"generate_hypotheses": {"hypotheses": [_btc_hypothesis()]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(len(result["approved_trades"]), 1, result["rejected_trades"])
        trade = result["approved_trades"][0]
        # 0.002 truncated to nearest multiple of 0.0015 -> 0.0015.
        self.assertAlmostEqual(trade["position_size"], 0.0015, places=8)
        self.assertAlmostEqual(trade["notional_usd"], 0.0015 * 50000.0, places=2)
        # Markets were already loaded on this fake exchange -- load_markets()
        # must NOT be called again (mirrors real ccxt: don't reload
        # unnecessarily on every single hypothesis).
        self.assertFalse(exch.load_markets_called)

    def test_load_markets_called_when_not_yet_loaded(self):
        exch = _FakeExchange(min_notional=10.0, step_size=0.0015, markets_loaded=False)
        agent, audit, repo, broker = _make_agent(exchange=exch)
        state = {"generate_hypotheses": {"hypotheses": [_btc_hypothesis()]}}
        result = agent.validate_and_size({}, state)
        self.assertEqual(len(result["approved_trades"]), 1, result["rejected_trades"])
        self.assertTrue(exch.load_markets_called)


class RejectedBelowExchangeMinNotionalTest(unittest.TestCase):
    """If, even after the buffered re-size, the exchange's own step-size
    truncation would push the notional back under its real minimum,
    the trade must be REJECTED -- sending it would just bounce."""

    def test_still_below_min_after_quantize_is_rejected(self):
        # balance=$1000, risk=1% -> risk_amount_usd=$10; atr=30000 ->
        # stop_distance=max(2*30000, 50000*0.005)=60000 -> quantity=
        # 10/60000=0.0001667 -> notional=$8.33 < min_order_usd -> Sprint
        # 18 auto-adjust bumps to notional=$10.00 exactly (quantity=
        # 0.0002).
        # buffered_floor = max(10, 10) * 1.05 = 10.5 -> since $10.00 <
        # $10.50, the A3 buffer bump re-sizes to quantity=10.5/50000=
        # 0.00021 -> truncated to nearest multiple of 0.00019 ->
        # 0.00019 -> notional = 0.00019*50000 = $9.50 < the (unbuffered)
        # $10 exchange floor -> reject.
        exch = _FakeExchange(min_notional=10.0, step_size=0.00019)
        agent, audit, repo, broker = _make_agent(exchange=exch)
        hyp = _btc_hypothesis(price=50000.0, atr=30000.0)  # very wide stop
        state = {"generate_hypotheses": {"hypotheses": [hyp]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(result["approved_trades"], [])
        reasons = [r.get("reason") for r in result["rejected_trades"]]
        self.assertIn("below_exchange_min_notional_after_quantize", reasons)

        events = audit.read_by_type("TRADE_REJECTED")
        matching = [e for e in events if e.get("reason") == "below_exchange_min_notional_after_quantize"]
        self.assertEqual(len(matching), 1)
        self.assertLess(matching[0]["notional_after_quantize"], 10.0)


class NoExchangeMinNotionalFallsBackToConfigTest(unittest.TestCase):
    """If the exchange doesn't publish a min-notional (limits missing),
    the buffered floor falls back to just min_order_usd x 1.05."""

    def test_missing_limits_uses_min_order_usd_only(self):
        exch = _FakeExchange(min_notional=None, step_size=0.0001)
        agent, audit, repo, broker = _make_agent(exchange=exch, min_order_usd=10.0)
        state = {"generate_hypotheses": {"hypotheses": [_btc_hypothesis()]}}
        result = agent.validate_and_size({}, state)
        self.assertEqual(len(result["approved_trades"]), 1, result["rejected_trades"])
        self.assertGreaterEqual(result["approved_trades"][0]["notional_usd"], 10.0)


class QuantizeFailureFallsBackGracefullyTest(unittest.TestCase):
    """Any exception while consulting the exchange (unknown symbol,
    markets not loaded and load_markets() fails, etc.) must NOT block
    an otherwise-valid trade -- fall back to unquantized sizing and
    audit a QUANTIZE_SKIPPED breadcrumb."""

    def test_market_lookup_failure_falls_back(self):
        exch = _FakeExchange(raise_on_market=True)
        agent, audit, repo, broker = _make_agent(exchange=exch)
        state = {"generate_hypotheses": {"hypotheses": [_btc_hypothesis()]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(len(result["approved_trades"]), 1, result["rejected_trades"])
        events = audit.read_by_type("QUANTIZE_SKIPPED")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["asset"], "BTC-USD")

    def test_amount_to_precision_failure_falls_back(self):
        exch = _FakeExchange(min_notional=10.0, raise_on_precision=True)
        agent, audit, repo, broker = _make_agent(exchange=exch)
        state = {"generate_hypotheses": {"hypotheses": [_btc_hypothesis()]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(len(result["approved_trades"]), 1, result["rejected_trades"])
        events = audit.read_by_type("QUANTIZE_SKIPPED")
        self.assertEqual(len(events), 1)


class EquityAssetsNeverQuantizedTest(unittest.TestCase):
    """Equities (Alpaca) trade fractional/notional shares -- no step-
    size/min-notional concept -- so this gate must never touch them,
    even when a crypto broker/exchange happens to be configured."""

    def test_equity_hypothesis_skips_quantization_entirely(self):
        exch = _FakeExchange(min_notional=10.0, step_size=0.0015)
        agent, audit, repo, broker = _make_agent(exchange=exch, balance=1000.0)
        hyp = {
            "asset": "SPY",
            "strategy": "test",
            "direction": "long",
            "price": 500.0,
            "atr_at_signal": 5.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hyp]}}
        result = agent.validate_and_size({}, state)

        # Whatever happens with balance resolution for an equity asset
        # (no alpaca_broker configured here -> falls back to no_broker
        # sim / crypto broker per resolve_broker_for_close's fallback),
        # the exchange's quantize path must never have been touched.
        self.assertFalse(exch.load_markets_called)
        events = audit.read_by_type("QUANTIZE_SKIPPED")
        self.assertEqual(len(events), 0)


class NoBrokerConfiguredSkipsQuantizationTest(unittest.TestCase):
    """No crypto broker at all (broker_client=None) -- e.g. paper/dev
    setup with no exchange credentials -- must not attempt to quantize
    (nothing to quantize against) and must not error."""

    def test_no_broker_skips_quantize_gate_entirely(self):
        tmpdir = tempfile.mkdtemp()
        audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
  