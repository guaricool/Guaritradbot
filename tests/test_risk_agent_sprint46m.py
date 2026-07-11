"""
Sprint 46M tests — RiskManagerAgent gates added after the live incident on
2026-07-10/11: the bot repeatedly opened a simultaneous BTC-USD long +
short pair every cycle. binance.us spot has no margin/borrow, so the
"short" leg was never a real exchange short — it produced repeated
CLOSE_FAILED "insufficient balance" errors and tangled, unclosable
positions.

Covers:
- allow_crypto_short=False (default): reject any "short" hypothesis on a
  CRYPTO-class asset.
- allow_crypto_short=True: short crypto hypotheses are NOT rejected by
  this gate (opt-in escape hatch, e.g. if real margin/futures trading is
  wired in later).
- block_conflicting_asset_positions=True (default): reject a new
  hypothesis for an asset that already has an OPEN position, regardless
  of direction (long+long or long+short).
- Equities are unaffected by the crypto-short gate (SPY/QQQ shorts still
  flow through to the mandate/broker layer as before).

Run: python -m unittest tests.test_risk_agent_sprint46m -v
"""
import os
import sys
import unittest
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.risk_agent import RiskManagerAgent
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository, Position


class _FakeBroker:
    def __init__(self):
        self.orders = []

    def get_usdt_balance(self):
        return 20.0

    @property
    def exchange(self):
        class _Ex:
            options = {"sandboxMode": True}
        return _Ex()

    def create_market_order(self, symbol, side, qty):
        self.orders.append({"symbol": symbol, "side": side, "qty": qty})
        return {"id": "fake", "symbol": symbol, "side": side, "qty": qty}


def _make_agent(tmpdir, **overrides):
    audit = AuditLedger(os.path.join(tmpdir, "audit.jsonl"))
    repo = PositionRepository(os.path.join(tmpdir, "positions.json"))
    broker = _FakeBroker()
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
        portfolio_stress_check=False,
        asset_concentration_check=False,
    )
    kwargs.update(overrides)
    return RiskManagerAgent(**kwargs), audit, repo


class CryptoShortBlockedTest(unittest.TestCase):
    """Default behavior: crypto shorts are rejected before ever reaching
    the mandate gate or the broker."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_btc_short_rejected_by_default(self):
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

        self.assertEqual(len(result["approved_trades"]), 0)
        self.assertEqual(len(result["rejected_trades"]), 1)
        self.assertEqual(
            result["rejected_trades"][0]["reason"], "crypto_short_not_supported"
        )
        # Never touched the broker.
        self.assertEqual(len(agent.broker.orders), 0)

    def test_btc_short_allowed_when_opted_in(self):
        agent, audit, repo = _make_agent(self.tmpdir, allow_crypto_short=True)
        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "Resistance_Fade",
            "direction": "short",
            "price": 64000.0,
            "atr_at_signal": 200.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(
            len(result["approved_trades"]), 1,
            f"Expected approval with allow_crypto_short=True, got {result['rejected_trades']}",
        )
        self.assertEqual(result["approved_trades"][0]["direction"], "short")

    def test_equity_short_unaffected_by_crypto_gate(self):
        """SPY/QQQ shorts must not be caught by the crypto-only gate."""
        agent, audit, repo = _make_agent(self.tmpdir)
        hypothesis = {
            "asset": "SPY",
            "strategy": "Resistance_Fade",
            "direction": "short",
            "price": 500.0,
            "atr_at_signal": 3.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)

        # Should NOT be rejected for "crypto_short_not_supported" — it may
        # still be approved or rejected for unrelated reasons, but never
        # because of the crypto-short gate.
        reasons = [r.get("reason") for r in result["rejected_trades"]]
        self.assertNotIn("crypto_short_not_supported", reasons)


class ConflictingAssetPositionBlockedTest(unittest.TestCase):
    """The actual root-cause bug: opening a long AND a short on the same
    asset in the same (or a later) cycle."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _open_position(self, repo, asset="BTC-USD", direction="long"):
        pos = Position(
            asset=asset,
            direction=direction,
            entry_price=64000.0,
            stop_loss=63000.0,
            take_profit=66000.0,
            qty=0.000156,
            risk_usd=0.20,
            entry_ts=__import__("time").time(),
            strategy="MACD_HistTurn_Bull",
        )
        repo.add_open(pos)
        return pos

    def test_new_hypothesis_rejected_when_asset_already_open(self):
        agent, audit, repo = _make_agent(self.tmpdir, allow_crypto_short=True)
        self._open_position(repo, asset="BTC-USD", direction="long")

        # A SHORT signal on the same asset (the exact live-incident shape)
        # must be rejected even with allow_crypto_short=True, because an
        # open position on this asset already exists.
        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "Resistance_Fade",
            "direction": "short",
            "price": 64100.0,
            "atr_at_signal": 200.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)

        self.assertEqual(len(result["approved_trades"]), 0)
        self.assertEqual(
            result["rejected_trades"][0]["reason"], "asset_already_has_open_position"
        )

    def test_different_asset_not_blocked(self):
        agent, audit, repo = _make_agent(self.tmpdir)
        self._open_position(repo, asset="BTC-USD", direction="long")

        hypothesis = {
            "asset": "ETH-USD",
            "strategy": "MACD_HistTurn_Bull",
            "direction": "long",
            "price": 3000.0,
            "atr_at_signal": 20.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)

        reasons = [r.get("reason") for r in result["rejected_trades"]]
        self.assertNotIn("asset_already_has_open_position", reasons)

    def test_gate_can_be_disabled(self):
        agent, audit, repo = _make_agent(
            self.tmpdir, allow_crypto_short=True, block_conflicting_asset_positions=False
        )
        self._open_position(repo, asset="BTC-USD", direction="long")

        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "Resistance_Fade",
            "direction": "short",
            "price": 64100.0,
            "atr_at_signal": 200.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)

        reasons = [r.get("reason") for r in result["rejected_trades"]]
        self.assertNotIn("asset_already_has_open_position", reasons)


if __name__ == "__main__":
    unittest.main()
