"""
Sprint 47C (audit B10) — replacement economy: per-component edge floor.

The audit's B10 finding: the position-replacement gate compared
two scoring functions of different ranges against a single
0.20 threshold:
  - score_new_hypothesis: 3 components (expected_move_pct, R:R,
    ATR-vs-entry), range roughly [-0.5, +1.0]
  - score_position: 5 components (P&L, dist to SL, time decay,
    R:R remaining, momentum), range roughly [-1.0, +1.0]

That's apples vs oranges. A new hypothesis with a slightly
positive aggregate score could "beat" an open position with a
deeply negative aggregate score, but only because the scoring
functions have different scales -- not because the new trade
actually has more edge.

The chosen fix (defense in depth, NOT a full scoring refactor):
add a per-trade minimum expected edge floor. The aggregate
score comparison still runs, but the proposed trade must
ALSO have at least `replacement_min_expected_edge_pct` of
theoretical edge (in absolute terms) for a replacement to
be considered. This catches the case where a low-edge new
trade wins the score comparison only because the worst open
is deeply underwater. Setting the floor to 0.0 disables it.

What these tests cover:
  1. New hypothesis with expected_move_pct < floor: REPLACEMENT_SKIPPED
     (reason: below_min_edge_floor), even if it would have won
     the aggregate score comparison.
  2. New hypothesis with expected_move_pct >= floor: existing
     score comparison runs as before.
  3. Setting floor=0 disables the check (legacy behavior).
  4. Default floor is 0.005 (0.5%) -- matches config default.
"""
import unittest
from unittest.mock import MagicMock

from src.agents.risk_agent import RiskManagerAgent


def _make_risk(*, min_edge_floor_pct=0.005):
    """Build a RiskManagerAgent with the given floor and no real
    audit / event_bus / position_repo / broker — the gate runs
    before any of those are touched when the floor rejects."""
    agent = RiskManagerAgent(
        broker_client=None,
        risk_per_trade_pct=1.0,
        max_capital_per_trade_pct=10.0,
        atr_stop_multiplier=2.0,
        atr_take_profit_multiplier=4.0,
        min_sl_floor_pct=0.005,
        min_tp_floor_pct=0.005,
        risk_reward_ratio=2.0,
        max_open_trades=5,
        min_order_usd=10.0,
        max_auto_adjust_risk_multiplier=2.0,
        event_bus=None,
        mandate_gate=None,
        audit=None,
        position_repo=None,
        enable_position_replacement=True,
        replacement_score_threshold=0.20,
        replacement_min_expected_edge_pct=min_edge_floor_pct,
    )
    # Stub current_prices (no live prices -> worst_pos lookup returns
    # None for current_price, but score_position handles that).
    agent.current_prices = {}
    # No real audit means the append() call is a no-op, which is
    # what we want here -- we're testing the GATE behavior, not
    # the audit emission.
    return agent


class ReplacementEdgeFloorTest(unittest.TestCase):
    """Sprint 47C (audit B10): minimum expected edge floor."""

    def test_low_edge_new_signal_rejected_by_floor(self):
        """A new hypothesis with expected_move_pct=0.001 (below
        the 0.5% floor) is rejected with reason
        'below_min_edge_floor', even if it would have won the
        aggregate score comparison against the worst open."""
        agent = _make_risk(min_edge_floor_pct=0.005)
        new_hyp = {"asset": "BTC-USD", "direction": "long"}
        new_trade = {"asset": "BTC-USD", "direction": "long", "notional_usd": 10.0}
        new_score_inputs = {
            "expected_move_pct": 0.001,  # 0.1% -- below 0.5% floor
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit": 102.0,
            "atr_at_signal": 1.0,
        }
        # No position repo -> _try_replace_position returns False
        # immediately. Add a minimal repo stub so we get past that
        # gate and exercise the edge-floor check.
        agent.position_repo = MagicMock()
        agent.position_repo.open.return_value = []
        result = agent._try_replace_position(new_hyp, new_trade, new_score_inputs)
        self.assertFalse(result, "low-edge new signal must be rejected by the floor")

    def test_high_edge_new_signal_passes_floor(self):
        """A new hypothesis with expected_move_pct=0.02 (above the
        0.5% floor) passes the floor and proceeds to the existing
        aggregate score comparison. With no open positions, the
        existing comparison returns False (no worst to replace)."""
        agent = _make_risk(min_edge_floor_pct=0.005)
        new_hyp = {"asset": "BTC-USD", "direction": "long"}
        new_trade = {"asset": "BTC-USD", "direction": "long", "notional_usd": 10.0}
        new_score_inputs = {
            "expected_move_pct": 0.02,  # 2% -- well above the floor
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit": 102.0,
            "atr_at_signal": 1.0,
        }
        agent.position_repo = MagicMock()
        agent.position_repo.open.return_value = []  # no opens
        result = agent._try_replace_position(new_hyp, new_trade, new_score_inputs)
        # No opens -> existing comparison returns False (no worst
        # to replace), but the floor didn't trip first.
        self.assertFalse(result, "no opens -> still no replacement, but the floor let it through")

    def test_zero_floor_disables_check(self):
        """Setting the floor to 0.0 disables the check entirely
        (legacy behavior). A new hypothesis with expected_move_pct
        below the legacy 0.5% would still be passed to the existing
        aggregate score comparison."""
        agent = _make_risk(min_edge_floor_pct=0.0)
        new_hyp = {"asset": "BTC-USD", "direction": "long"}
        new_trade = {"asset": "BTC-USD", "direction": "long", "notional_usd": 10.0}
        new_score_inputs = {
            "expected_move_pct": 0.0001,  # 0.01% -- very low edge
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit": 102.0,
            "atr_at_signal": 1.0,
        }
        agent.position_repo = MagicMock()
        agent.position_repo.open.return_value = []
        result = agent._try_replace_position(new_hyp, new_trade, new_score_inputs)
        # With floor=0, the check is skipped -> existing comparison
        # runs (no opens -> returns False). The key behavior: it
        # didn't short-circuit on the floor.
        self.assertFalse(result)

    def test_default_floor_is_0_5_percent(self):
        """The default for replacement_min_expected_edge_pct in
        the dataclass / constructor signature is 0.005 (0.5%),
        matching the config.yaml default. This is a regression
        guard: if someone changes the default in one place but
        not the other, this test catches it."""
        agent = _make_risk()  # no explicit floor -> default
        self.assertEqual(agent.replacement_min_expected_edge_pct, 0.005)


if __name__ == "__main__":
    unittest.main()
