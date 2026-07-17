"""
Bug fix: MarketAnalystAgent.fetch_one() previously had no data-quality
gate at all (unlike fetch_and_analyze(), which calls
_validate_or_fault() before computing indicators) and never trimmed a
still-forming daily candle (its "1d" interval was missing from
_YF_INTERVAL_SECONDS, so _trim_in_progress_bar was never invoked for
the only interval fetch_one is actually called with in production).

fetch_one's only real caller is EpochScheduler's periodic walk-forward
RSI re-optimization (src/execution/scheduler.py) -- these tests pin
that a corrupt/NaN bar is now rejected (returns None) and that a
still-forming "today" daily candle is dropped before indicators are
computed on it.
"""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd


class FetchOneRejectsCorruptDataTest(unittest.TestCase):
    def test_nan_close_returns_none(self):
        from src.agents.market_analyst import MarketAnalystAgent
        from src.agents import market_analyst as ma_mod

        ma = MarketAnalystAgent()
        n = 60
        df = pd.DataFrame({
            "Open": [100.0] * n,
            "High": [101.0] * n,
            "Low": [99.0] * n,
            "Close": [100.0] * (n - 1) + [np.nan],
            "Volume": [1000.0] * n,
        }, index=pd.date_range("2024-01-01", periods=n, freq="1D"))

        original = ma_mod.safe_yf_download
        ma_mod.safe_yf_download = lambda *a, **kw: df
        try:
            result = ma.fetch_one("BTC-USD", interval="1d", period="1y")
        finally:
            ma_mod.safe_yf_download = original

        self.assertIsNone(result)

    def test_negative_price_returns_none(self):
        from src.agents.market_analyst import MarketAnalystAgent
        from src.agents import market_analyst as ma_mod

        ma = MarketAnalystAgent()
        n = 60
        df = pd.DataFrame({
            "Open": [100.0] * n,
            "High": [101.0] * n,
            "Low": [99.0] * (n - 1) + [-5.0],
            "Close": [100.0] * n,
            "Volume": [1000.0] * n,
        }, index=pd.date_range("2024-01-01", periods=n, freq="1D"))

        original = ma_mod.safe_yf_download
        ma_mod.safe_yf_download = lambda *a, **kw: df
        try:
            result = ma.fetch_one("BTC-USD", interval="1d", period="1y")
        finally:
            ma_mod.safe_yf_download = original

        self.assertIsNone(result)

    def test_clean_daily_data_still_returns_indicators(self):
        """Regression guard: the new validation must not reject
        perfectly healthy data."""
        from src.agents.market_analyst import MarketAnalystAgent
        from src.agents import market_analyst as ma_mod

        ma = MarketAnalystAgent()
        n = 120
        # Well in the past so the in-progress-bar trim doesn't drop
        # the last row either.
        rng = np.random.default_rng(1)
        closes = 100 + np.cumsum(rng.normal(0, 1, n))
        df = pd.DataFrame({
            "Open": closes, "High": closes + 1, "Low": closes - 1,
            "Close": closes, "Volume": [1000.0] * n,
        }, index=pd.date_range("2020-01-01", periods=n, freq="1D"))

        original = ma_mod.safe_yf_download
        ma_mod.safe_yf_download = lambda *a, **kw: df
        try:
            result = ma.fetch_one("BTC-USD", interval="1d", period="1y")
        finally:
            ma_mod.safe_yf_download = original

        self.assertIsNotNone(result)
        self.assertIn("RSI", result.columns)
        self.assertIn("ATR_14", result.columns)


class FetchOneTrimsInProgressDailyBarTest(unittest.TestCase):
    def test_todays_still_forming_daily_bar_is_dropped(self):
        from src.agents.market_analyst import MarketAnalystAgent
        from src.agents import market_analyst as ma_mod

        ma = MarketAnalystAgent()
        n = 100
        # Bars end at "today" (still-forming, since the day hasn't
        # closed yet) -- the last row must be trimmed before indicators
        # are computed, same guarantee fetch_and_analyze already has
        # for intraday bars.
        closes = [100.0 + 0.1 * i for i in range(n)]
        idx = pd.date_range(end=pd.Timestamp.now().normalize(), periods=n, freq="1D")
        df = pd.DataFrame({
            "Open": closes, "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes], "Close": closes,
            "Volume": [1000.0] * n,
        }, index=idx)

        original = ma_mod.safe_yf_download
        ma_mod.safe_yf_download = lambda *a, **kw: df
        try:
            result = ma.fetch_one("BTC-USD", interval="1d", period="1y")
        finally:
            ma_mod.safe_yf_download = original

        self.assertIsNotNone(result)
        # The still-forming last bar (today) must have been dropped --
        # the result's last index must be strictly before today.
        self.assertLess(result.index[-1], pd.Timestamp.now().normalize())


if __name__ == "__main__":
    unittest.main()
