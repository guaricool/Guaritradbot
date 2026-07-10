"""
Sprint 18 tests — RiskManagerAgent fixes & portfolio management.

Covers:
- Bug A: Auto-adjust when notional < min_order_usd (not just max_notional)
- Feature 1: Position replacement when max_open_trades is full

These tests don't need pytest — use stdlib `unittest`.
Run: python -m unittest tests.test_risk_agent_sprint18 -v
"""
import os
import sys
import unittest
import tempfile

# Make project root importable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.risk_agent import RiskManagerAgent
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository, Position
from src.safety.mandate_gate import MandateGate, MandateConfig


class _FakeBroker:
    """Captures market orders without hitting a real exchange."""
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


class RiskAgentBugAFixTest(unittest.TestCase):
    """
    Bug A: Micro-Account Death Loop.

    Setup: $20 balance, 1% risk, 4% ATR stop → notional = $5 < min_order $10.
    Expected: Auto-adjust triggers and bumps notional to $10 (not rejected).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.broker = _FakeBroker()

    def test_risk_below_min_order_triggers_auto_adjust(self):
        """$20 balance + 1% risk + 4% stop = $5 notional → must bump to $10."""
        agent = RiskManagerAgent(
            broker_client=self.broker,
            risk_per_trade_pct=1.0,
            max_capital_per_trade_pct=50.0,   # 50% of $20 = $10 (not less than min)
            atr_stop_multiplier=2.0,
            min_order_usd=10.0,
            audit=self.audit,
            position_repo=self.repo,
        )
        # ATR = 4% of entry → stop_distance = 8% of entry → 1% risk / 8% = 12.5%
        # → notional = $20 * 12.5% = $2.50 < $10
        # But that's below balance too. Use a slightly different setup:
        # risk=1% of $20 = $0.20, stop_distance = 4% of entry = $200 (entry $5000)
        # → quantity = $0.20 / $200 = 0.001 BTC → notional = $5
        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "RSI_MeanReversion",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 200.0,  # 0.4% of price; 2*ATR = $400 stop distance
        }
        # Wait: 2*ATR = 400, quantity = 0.20/400 = 0.0005 BTC, notional = $25 (above min)
        # We need notional < min. Set ATR higher: ATR = 2000, stop = 4000.
        hypothesis["atr_at_signal"] = 2000.0
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}

        result = agent.validate_and_size({}, state)

        self.assertEqual(len(result["approved_trades"]), 1,
                         f"Trade should be approved after auto-adjust, got {result['rejected_trades']}")
        trade = result["approved_trades"][0]
        self.assertGreaterEqual(
            trade["notional_usd"], 10.0,
            f"Auto-adjust should bring notional to >= min_order $10, got ${trade['notional_usd']}",
        )

    def test_max_cap_below_min_order_also_triggers(self):
        """Original Sprint 12 case still works: max_cap=$5, min=$10 → bump."""
        agent = RiskManagerAgent(
            broker_client=self.broker,
            risk_per_trade_pct=1.0,
            max_capital_per_trade_pct=25.0,   # 25% of $20 = $5 (less than min)
            atr_stop_multiplier=2.0,
            min_order_usd=10.0,
            audit=self.audit,
            position_repo=self.repo,
        )
        hypothesis = {
            "asset": "BTC-USD",
            "strategy": "RSI_MeanReversion",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 100.0,  # small ATR → big notional from risk/distance
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}

        result = agent.validate_and_size({}, state)
        self.assertEqual(len(result["approved_trades"]), 1)
        self.assertGreaterEqual(result["approved_trades"][0]["notional_usd"], 10.0)


class PositionReplacementTest(unittest.TestCase):
    """
    Feature 1: Position Replacement.

    Setup: max_open_trades = 2. Two positions open. New hypothesis scores
    MUCH higher than the worst open. Expected: worst is closed, new opens.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.broker = _FakeBroker()

        # Pre-fill repo with 2 open positions
        import time
        # Position A: a loser (entered high, price dropped)
        self.pos_a = Position(
            asset="ETH-USD",
            direction="long",
            entry_price=3000.0,
            stop_loss=2950.0,
            take_profit=3150.0,
            qty=0.01,
            risk_usd=0.50,
            entry_ts=time.time() - 86400,  # 24h old
            strategy="old_loser",
        )
        # Position B: a stale winner
        self.pos_b = Position(
            asset="GLD",
            direction="long",
            entry_price=180.0,
            stop_loss=178.0,
            take_profit=185.0,
            qty=0.1,
            risk_usd=0.20,
            entry_ts=time.time() - 86400 * 3,  # 3 days old
            strategy="old_winner",
        )
        self.repo.add_open(self.pos_a)
        self.repo.add_open(self.pos_b)

    def test_replace_worst_when_new_score_much_higher(self):
        agent = RiskManagerAgent(
            broker_client=self.broker,
            risk_per_trade_pct=1.0,
            max_capital_per_trade_pct=50.0,
            atr_stop_multiplier=2.0,
            min_order_usd=10.0,
            audit=self.audit,
            position_repo=self.repo,
            max_open_trades=2,  # we're already at the limit
            enable_position_replacement=True,
            replacement_score_threshold=0.20,
            current_prices={
                "ETH-USD": 2900.0,   # pos_a is -1.66% below entry (loser)
                "GLD": 182.0,        # pos_b is +1.1% above entry (winner but stale)
            },
        )

        # New hypothesis: BTC with high expected move + good R:R
        # Should score very high and replace pos_a (the loser)
        new_hyp = {
            "asset": "BTC-USD",
            "strategy": "momentum_breakout",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 500.0,  # 1% ATR (clean)
            "expected_move_pct": 4.0,  # strong expected move
        }
        state = {"generate_hypotheses": {"hypotheses": [new_hyp]}}
        result = agent.validate_and_size({}, state)

        # The new trade should have been approved
        self.assertEqual(len(result["approved_trades"]), 1,
                         f"New trade should be approved via replacement; rejected={result['rejected_trades']}")
        self.assertEqual(result["approved_trades"][0]["asset"], "BTC-USD")

        # One of the original positions should have been closed
        opens_after = self.repo.open()
        closed_assets = {self.pos_a.asset, self.pos_b.asset} - {p.asset for p in opens_after}
        self.assertEqual(
            len(closed_assets), 1,
            f"Exactly one open position should have been closed; closed={closed_assets}",
        )
        # The closed one should be the loser (ETH-USD)
        self.assertIn("ETH-USD", closed_assets, "The losing position should have been replaced")

        # Audit log should record POSITION_REPLACED
        events = self.audit.read_all()
        replaced = [e for e in events if e.get("event_type") == "POSITION_REPLACED"]
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0]["closed_asset"], "ETH-USD")
        self.assertEqual(replaced[0]["new_asset"], "BTC-USD")

    def test_no_replacement_when_new_score_not_better_enough(self):
        """If new signal is not better than worst + threshold, don't replace."""
        agent = RiskManagerAgent(
            broker_client=self.broker,
            risk_per_trade_pct=1.0,
            max_capital_per_trade_pct=50.0,
            atr_stop_multiplier=2.0,
            min_order_usd=10.0,
            audit=self.audit,
            position_repo=self.repo,
            max_open_trades=2,
            enable_position_replacement=True,
            replacement_score_threshold=0.20,
            current_prices={
                "ETH-USD": 2970.0,   # pos_a slightly down (small negative score)
                "GLD": 181.0,        # pos_b slightly up (small positive score)
            },
        )

        # New hypothesis: NEGATIVE expected move + noisy ATR → very low score
        new_hyp = {
            "asset": "BTC-USD",
            "strategy": "weak_signal",
            "direction": "long",
            "price": 50000.0,
            "atr_at_signal": 3000.0,  # 6% ATR (noisy → -0.2)
            "expected_move_pct": -1.0,  # NEGATIVE → score goes deeply negative
        }
        state = {"generate_hypotheses": {"hypotheses": [new_hyp]}}
        result = agent.validate_and_size({}, state)

        # Trade should have been REJECTED
        self.assertEqual(len(result["approved_trades"]), 0)
        self.assertTrue(
            any("max_open_trades" in str(r.get("reason", "")) for r in result["rejected_trades"]),
            f"Should have been rejected for max_open_trades; got {result['rejected_trades']}",
        )
        # All originals should still be open
        self.assertEqual(self.repo.count_open(), 2)
        # Audit log records the skip
        events = self.audit.read_all()
        skipped = [e for e in events if e.get("event_type") == "REPLACEMENT_SKIPPED"]
        self.assertEqual(len(skipped), 1, f"Should log REPLACEMENT_SKIPPED; events={[e.get('event_type') for e in events]}")


class PositionScoringTest(unittest.TestCase):
    """Verify scoring functions are sensible in isolation."""

    def test_losing_position_scores_lower_than_winning(self):
        import time
        repo = PositionRepository.__new__(PositionRepository)
        repo.positions = []

        loser = Position(
            asset="X", direction="long",
            entry_price=100, stop_loss=95, take_profit=110,
            qty=1, risk_usd=5,
            entry_ts=time.time(),
            strategy="test",
        )
        winner = Position(
            asset="Y", direction="long",
            entry_price=100, stop_loss=95, take_profit=110,
            qty=1, risk_usd=5,
            entry_ts=time.time(),
            strategy="test",
        )
        repo.positions = [loser, winner]
        agent = RiskManagerAgent(
            position_repo=repo,
            current_prices={"X": 96, "Y": 108},  # loser down, winner up
        )
        loser_score = agent.score_position(loser, current_price=96)
        winner_score = agent.score_position(winner, current_price=108)
        self.assertLess(loser_score, winner_score)


if __name__ == "__main__":
    unittest.main()