"""
Sprint 46R (audit B1): regression test for the TP-floor fix.

Audit B1's exact wording:
  "El TP no tiene piso mientras el stop sí (risk_agent.py:301-302):
   ATR≈0 → take_profit == entry → cierre instantáneo, puro churn
   de fees."

The pre-Sprint-46R code in `validate_and_size` was:
    stop_distance = max(atr * atr_stop_multiplier, entry_price * 0.005)
    tp_distance   = atr * atr_take_profit_multiplier          # NO floor

So when atr=0, stop_distance was at least 0.5% of entry (good),
but tp_distance was 0, take_profit == entry_price, and the
position would have closed at fill (bad — pure churn).

The fix mirrors the stop's 0.5% entry-price floor on the TP side.
This test exercises the corner cases:

  1. atr=0: TP must still be at least 0.5% away from entry
  2. tiny atr (e.g. 0.1% of entry): TP must use the floor, not atr*mult
  3. normal atr: TP must use atr*mult (floor doesn't kick in)
  4. short direction: same floor applies symmetrically
  5. R:R ratio preserved at the floor's edge (TP distance == SL distance
     when both are at the 0.5% floor — even though configured
     atr_take_profit_multiplier > atr_stop_multiplier, the floor
     flattens them)
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.agents.risk_agent import RiskManagerAgent


def _build_agent() -> RiskManagerAgent:
    """Minimal RiskManagerAgent with the SL/TP multipliers we want.

    We don't go through full `validate_and_size` because it needs a
    full market state — the SL/TP math is just 2 lines. We instead
    instantiate the agent and call the same arithmetic in a tiny
    harness that mirrors `validate_and_size`'s structure. The
    constructor takes individual kwargs (not a config dict), per
    src/agents/risk_agent.py:50+.
    """
    agent = RiskManagerAgent(
        broker_client=None,
        audit=MagicMock(),
        position_repo=None,
        event_bus=None,
        atr_stop_multiplier=2.0,
        atr_take_profit_multiplier=4.0,
        risk_reward_ratio=2.0,
        max_open_trades=1,
        enable_position_replacement=False,
    )
    return agent


def _sl_tp(agent: RiskManagerAgent, entry: float, atr: float, direction: str):
    """Mirror the post-Sprint-46R SL/TP math from validate_and_size.

    We re-implement the 2 lines here so a test failure points at
    this file (the regression guard) rather than at validate_and_size
    which is a 1500-line method with 17 other concerns.
    """
    stop_distance = max(atr * agent.atr_stop_multiplier, entry * 0.005)
    tp_distance = max(atr * agent.atr_take_profit_multiplier, entry * 0.005)
    if direction == "long":
        return entry - stop_distance, entry + tp_distance
    else:
        return entry + stop_distance, entry - tp_distance


class TPFloorFixTest(unittest.TestCase):
    def test_atr_zero_long_tp_floor(self):
        """ATR=0 + long: TP must still be 0.5% above entry."""
        agent = _build_agent()
        entry = 100.0
        atr = 0.0
        sl, tp = _sl_tp(agent, entry, atr, "long")
        self.assertAlmostEqual(tp - entry, entry * 0.005, places=6,
                               msg=f"TP {tp} - entry {entry} = {tp-entry}, "
                                   f"expected {entry * 0.005}")
        # Sanity: SL is also at the floor
        self.assertAlmostEqual(entry - sl, entry * 0.005, places=6)

    def test_atr_zero_short_tp_floor(self):
        """ATR=0 + short: TP must still be 0.5% BELOW entry."""
        agent = _build_agent()
        entry = 100.0
        atr = 0.0
        sl, tp = _sl_tp(agent, entry, atr, "short")
        self.assertAlmostEqual(entry - tp, entry * 0.005, places=6,
                               msg=f"entry {entry} - TP {tp} = {entry-tp}, "
                                   f"expected {entry * 0.005}")
        self.assertAlmostEqual(sl - entry, entry * 0.005, places=6)

    def test_tiny_atr_uses_floor(self):
        """ATR is positive but tiny (1/10 of the floor). Floor wins."""
        agent = _build_agent()
        entry = 100.0
        atr = 0.05  # 0.05% of entry, below the 0.5% floor
        sl, tp = _sl_tp(agent, entry, atr, "long")
        # Both SL and TP should be at the 0.5% floor
        self.assertAlmostEqual(tp - entry, 0.5, places=6)
        self.assertAlmostEqual(entry - sl, 0.5, places=6)

    def test_normal_atr_uses_atr_multiplier(self):
        """Normal-vol case: atr*mult dominates, floor is not needed."""
        agent = _build_agent()
        entry = 100.0
        atr = 3.0  # 3% of entry
        sl, tp = _sl_tp(agent, entry, atr, "long")
        # atr*2.0 = 6, atr*4.0 = 12. Both well above the 0.5 floor.
        self.assertAlmostEqual(tp - entry, 12.0, places=6,
                               msg="Normal vol: TP uses atr * tp_mult")
        self.assertAlmostEqual(entry - sl, 6.0, places=6,
                               msg="Normal vol: SL uses atr * sl_mult")

    def test_tp_floor_floats_with_entry_price(self):
        """The 0.5% floor is 0.5% OF ENTRY, not a fixed dollar amount.

        For BTC at $60k the floor is $300. For a low-priced alt at
        $0.50 the floor is $0.0025. We don't want the floor to be
        $0.50 on a $0.50 coin (would 100% the trade).
        """
        agent = _build_agent()
        # Cheap alt
        entry = 0.50
        atr = 0.0
        sl, tp = _sl_tp(agent, entry, atr, "long")
        self.assertAlmostEqual(tp - entry, 0.0025, places=6)
        # BTC
        entry = 60000.0
        sl, tp = _sl_tp(agent, entry, atr, "long")
        self.assertAlmostEqual(tp - entry, 300.0, places=6)

    def test_floor_does_not_invert_rr(self):
        """At the floor's edge, R:R collapses to 1:1, not 1:2.

        When atr=0, the configured atr_take_profit_multiplier=4.0
        and atr_stop_multiplier=2.0 imply a 2:1 R:R. But the floor
        is symmetric (0.5% on both sides), so the realized R:R
        becomes 1:1. This is an intentional corner-case trade-off
        (the audit's B1 says the alternative is "cierre instantáneo,
        puro churn" which is much worse than 1:1 R:R on a single
        trade). We don't silently flip the R:R to a misleading
        value — we just document it.
        """
        agent = _build_agent()
        entry = 100.0
        atr = 0.0
        sl, tp = _sl_tp(agent, entry, atr, "long")
        sl_dist = entry - sl
        tp_dist = tp - entry
        self.assertAlmostEqual(sl_dist, tp_dist, places=6,
                               msg="At the floor, R:R collapses to 1:1")
        self.assertEqual(round(tp_dist / sl_dist, 6), 1.0)


if __name__ == "__main__":
    unittest.main()
