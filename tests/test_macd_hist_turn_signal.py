"""
Bug fix: StrategyAgent's "MACD histogram turning" block (comment:
"turning bullish: was negative and getting more negative, now rising")
used a condition (`h_prev2 < h_prev < 0`) that actually requires the
histogram to already be RISING for two bars before the check — i.e.
it fired on momentum CONTINUATION, never on the V-shaped REVERSAL the
name/comment describe. Fixed to `h_prev2 > h_prev < 0` (was declining
while negative, now turns up). Symmetric fix for the bearish branch.

These tests build a market_data df where MACD_Signal is held at 0 (so
hist == macd exactly, and neither the strict-cross (A) nor the
recent-cross (B) branches fire — the histogram never crosses zero in
either scenario below) and drive only branch C.
"""
import unittest

import numpy as np
import pandas as pd

from src.agents.strategy_agent import StrategyAgent


def _make_df(last_three_macd, padding=-10.0, n=40):
    # Padding must be clearly same-signed and far from the MACD_Signal
    # (held at 0) so the strict-cross (A) and recent-cross (B) branches
    # — which run BEFORE this elif chain reaches branch C — don't fire
    # first and mask what branch C actually does. A flat 0 padding would
    # spuriously look like "just crossed zero" the moment the last-3
    # values move off of it.
    macd = [padding] * (n - 3) + list(last_three_macd)
    sig = [0.0] * n
    close = [100.0] * n
    return pd.DataFrame({
        "Open": close, "High": close, "Low": close, "Close": close,
        "MACD": macd, "MACD_Signal": sig, "ATR_14": [1.0] * n,
    }, index=pd.date_range("2024-01-01", periods=n, freq="1h"))


def _hist_turn_strategies(hypotheses):
    return {h["strategy"] for h in hypotheses if h.get("asset") == "BTC-USD"}


class MACDHistTurnBullishReversalTest(unittest.TestCase):
    def test_decline_then_bounce_fires_bull_turn(self):
        # -1 -> -3 (declining, getting MORE negative) -> -2 (turning up)
        agent = StrategyAgent()
        df = _make_df([-1.0, -3.0, -2.0])
        state = {"analyze_market": {"market_data": {"BTC-USD": {"1h": df}}}}
        result = agent.evaluate_strategies({}, state)
        hyps = result["hypotheses"] if isinstance(result, dict) else result
        strategies = _hist_turn_strategies(hyps)
        self.assertIn("MACD_HistTurn_Bull", strategies)
        btc_long_hyps = [h for h in hyps if h.get("asset") == "BTC-USD"
                         and h.get("strategy") == "MACD_HistTurn_Bull"]
        self.assertEqual(btc_long_hyps[0]["direction"], "long")

    def test_pure_continuation_does_not_fire_bull_turn(self):
        # -5 -> -3 -> -1: continuously RISING the whole time, never
        # declined. Under the old buggy condition this incorrectly
        # fired "HistTurn_Bull" (a continuation, not a turn).
        agent = StrategyAgent()
        df = _make_df([-5.0, -3.0, -1.0])
        state = {"analyze_market": {"market_data": {"BTC-USD": {"1h": df}}}}
        result = agent.evaluate_strategies({}, state)
        hyps = result["hypotheses"] if isinstance(result, dict) else result
        strategies = _hist_turn_strategies(hyps)
        self.assertNotIn("MACD_HistTurn_Bull", strategies)


class MACDHistTurnBearishReversalTest(unittest.TestCase):
    def test_rise_then_drop_fires_bear_turn(self):
        # +1 -> +3 (rising, getting MORE positive) -> +2 (turning down)
        # allow_crypto_short=True: BTC-USD shorts are filtered out by
        # default (binance.us spot has no margin) -- opt in so this
        # test can see the raw signal-generation branch under test.
        agent = StrategyAgent(allow_crypto_short=True)
        df = _make_df([1.0, 3.0, 2.0], padding=10.0)
        state = {"analyze_market": {"market_data": {"BTC-USD": {"1h": df}}}}
        result = agent.evaluate_strategies({}, state)
        hyps = result["hypotheses"] if isinstance(result, dict) else result
        strategies = _hist_turn_strategies(hyps)
        self.assertIn("MACD_HistTurn_Bear", strategies)
        btc_short_hyps = [h for h in hyps if h.get("asset") == "BTC-USD"
                          and h.get("strategy") == "MACD_HistTurn_Bear"]
        self.assertEqual(btc_short_hyps[0]["direction"], "short")

    def test_pure_continuation_does_not_fire_bear_turn(self):
        # +5 -> +3 -> +1: continuously FALLING the whole time, never
        # rose. Under the old buggy condition this incorrectly fired
        # "HistTurn_Bear" (a continuation, not a turn).
        agent = StrategyAgent(allow_crypto_short=True)
        df = _make_df([5.0, 3.0, 1.0], padding=10.0)
        state = {"analyze_market": {"market_data": {"BTC-USD": {"1h": df}}}}
        result = agent.evaluate_strategies({}, state)
        hyps = result["hypotheses"] if isinstance(result, dict) else result
        strategies = _hist_turn_strategies(hyps)
        self.assertNotIn("MACD_HistTurn_Bear", strategies)


if __name__ == "__main__":
    unittest.main()
