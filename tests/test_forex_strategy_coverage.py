"""
Bug fix: forex (OANDA) was added as a new asset class/broker, but every
strategy block in StrategyAgent.evaluate_strategies() iterates a
HARDCODED per-strategy tuple of assets instead of `market_data.keys()`
-- none of them included the forex symbols. Confirmed on a real
deployment: CAPITAL_ROUTING_APPLIED included "forex" with valid
indicator data fetched every cycle, but zero HYPOTHESIS_GENERATED
events ever fired for EURUSD=X/GBPUSD=X/USDJPY=X/USDCAD=X/AUDUSD=X
across 6+ real cycles, while equities fired 4-5 hypotheses every time.

Run: python -m unittest tests.test_forex_strategy_coverage -v
"""
import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.strategy_agent import StrategyAgent, FOREX_MAJORS


def _make_rsi_oversold_df(n=30):
    """RSI crosses from >=30 to <30 on the last bar -- triggers the
    strict RSI mean-reversion cross (path A)."""
    close = [1.08] * n
    rsi = [50.0] * (n - 1) + [25.0]
    return pd.DataFrame({
        "Open": close, "High": close, "Low": close, "Close": close,
        "RSI": rsi, "ATR_14": [0.001] * n, "BB_Middle": [1.09] * n,
    }, index=pd.date_range("2024-01-01", periods=n, freq="1h"))


class ForexMajorsConstantTest(unittest.TestCase):
    def test_all_five_pairs_present(self):
        self.assertEqual(
            set(FOREX_MAJORS),
            {"EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCAD=X", "AUDUSD=X"},
        )


class ForexRsiStrategyCoverageTest(unittest.TestCase):
    """The RSI mean-reversion block (SPY/QQQ + forex majors) must
    evaluate forex symbols exactly like equities."""

    def test_forex_pair_generates_rsi_hypothesis(self):
        agent = StrategyAgent()
        df = _make_rsi_oversold_df()
        state = {"analyze_market": {"market_data": {"EURUSD=X": {"1h": df}}}}
        result = agent.evaluate_strategies({}, state)
        hyps = result["hypotheses"] if isinstance(result, dict) else result
        forex_hyps = [h for h in hyps if h.get("asset") == "EURUSD=X"]
        self.assertTrue(
            forex_hyps,
            f"Expected at least one hypothesis for EURUSD=X, got none. All hyps: {hyps}",
        )
        self.assertTrue(any("RSI" in h["strategy"] for h in forex_hyps))

    def test_all_forex_majors_are_evaluated(self):
        """Every one of the 5 configured forex pairs must be looked at
        by the RSI strategy, not just EUR/USD."""
        agent = StrategyAgent()
        market_data = {pair: {"1h": _make_rsi_oversold_df()} for pair in FOREX_MAJORS}
        state = {"analyze_market": {"market_data": market_data}}
        result = agent.evaluate_strategies({}, state)
        hyps = result["hypotheses"] if isinstance(result, dict) else result
        assets_with_hyps = {h["asset"] for h in hyps}
        for pair in FOREX_MAJORS:
            self.assertIn(pair, assets_with_hyps, f"{pair} never generated a hypothesis")

    def test_equity_still_works_unaffected(self):
        """Back-compat: adding forex must not break SPY/QQQ."""
        agent = StrategyAgent()
        df = _make_rsi_oversold_df()
        state = {"analyze_market": {"market_data": {"SPY": {"1h": df}}}}
        result = agent.evaluate_strategies({}, state)
        hyps = result["hypotheses"] if isinstance(result, dict) else result
        self.assertTrue(any(h.get("asset") == "SPY" for h in hyps))


if __name__ == "__main__":
    unittest.main()
