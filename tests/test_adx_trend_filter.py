"""
Bug fix: StrategyAgent computed `adx_now` and had an `adx_trend_min`
param (Sprint 10, module docstring advertises "ADX trend
confirmation") but never actually used either anywhere — every GLD/USO
EMA cross fired regardless of trend strength. Wired ADX_14 in as a
gate: below `adx_trend_min`, skip the cross checks for that asset this
cycle (weak/choppy market, not a real trend).
"""
import unittest

import pandas as pd

from src.agents.strategy_agent import StrategyAgent


def _make_gld_df(adx_value, n=70):
    # EMA_20 crosses above EMA_50 on the last bar (golden cross).
    ema50 = [100.0] * n
    ema20 = [99.0] * (n - 1) + [101.0]
    close = [100.0] * n
    adx = [adx_value] * n if adx_value is not None else None
    data = {
        "Open": close, "High": close, "Low": close, "Close": close,
        "EMA_20": ema20, "EMA_50": ema50, "ATR_14": [1.0] * n,
    }
    if adx is not None:
        data["ADX_14"] = adx
    return pd.DataFrame(data, index=pd.date_range("2024-01-01", periods=n, freq="4h"))


def _gld_strategies(hyps):
    return {h["strategy"] for h in hyps if h.get("asset") == "GLD"}


class ADXTrendFilterTest(unittest.TestCase):
    def test_strong_trend_allows_cross(self):
        agent = StrategyAgent()
        df = _make_gld_df(adx_value=30.0)  # above default adx_trend_min=20
        state = {"analyze_market": {"market_data": {"GLD": {"4h": df}}}}
        result = agent.evaluate_strategies({}, state)
        hyps = result["hypotheses"] if isinstance(result, dict) else result
        self.assertIn("EMA_GoldenCross", _gld_strategies(hyps))

    def test_weak_trend_blocks_cross(self):
        agent = StrategyAgent()
        df = _make_gld_df(adx_value=10.0)  # below default adx_trend_min=20
        state = {"analyze_market": {"market_data": {"GLD": {"4h": df}}}}
        result = agent.evaluate_strategies({}, state)
        hyps = result["hypotheses"] if isinstance(result, dict) else result
        self.assertNotIn("EMA_GoldenCross", _gld_strategies(hyps))

    def test_missing_adx_column_fails_open(self):
        """A missing ADX_14 column must never silently block the
        cross forever -- only a confirmed low reading should."""
        agent = StrategyAgent()
        df = _make_gld_df(adx_value=None)
        state = {"analyze_market": {"market_data": {"GLD": {"4h": df}}}}
        result = agent.evaluate_strategies({}, state)
        hyps = result["hypotheses"] if isinstance(result, dict) else result
        self.assertIn("EMA_GoldenCross", _gld_strategies(hyps))


if __name__ == "__main__":
    unittest.main()
