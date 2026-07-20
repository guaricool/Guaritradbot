"""
Bug: every Monday morning (or the morning after any weekend), the
equity staleness check falsely marked SPY/QQQ/GLD/USO's 4h feed as
"data integrity fail" and DEGRADED the whole MarketAnalystAgent,
producing zero hypotheses until enough of the new session accumulated.

Root cause: `_validate_or_fault`'s market-hours gate only skipped the
staleness check when the market was CLOSED at the exact instant the
check ran. Once it reopened, the RAW wall-clock age of the last
COMPLETE bucket (correctly Friday's close, since today's first 4h
bucket hasn't closed yet) blew past the flat 6x-multiplier threshold
(24h for 4h bars) -- Friday close to Monday morning is ~66 real hours,
even though almost none of that was actual trading time.

Fix: `_trading_seconds_since` measures only NYSE regular-session
seconds (09:30-16:00 ET, Mon-Fri) between the last bar and now, so a
Friday-close bar checked Monday morning reads as a few trading hours
old, not 66 wall-clock hours old.

Run: python -m unittest tests.test_monday_morning_equity_staleness -v
"""
import os
import sys
import unittest
from unittest.mock import patch

import pandas as pd
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.market_analyst import MarketAnalystAgent, _trading_seconds_since

NY = pytz.timezone("America/New_York")


def _make_ohlcv(last_ts: pd.Timestamp, n: int = 30) -> pd.DataFrame:
    idx = pd.date_range(end=last_ts, periods=n, freq="4h", tz=last_ts.tz)
    return pd.DataFrame({
        "Open": [100.0] * n, "High": [101.0] * n,
        "Low": [99.0] * n, "Close": [100.5] * n, "Volume": [1000] * n,
        "RSI": [50.0] * n, "EMA_9": [100.0] * n, "EMA_21": [100.0] * n,
        "EMA_50": [100.0] * n, "MACD": [0.0] * n, "MACD_Signal": [0.0] * n,
        "MACD_Hist": [0.0] * n, "ATR_14": [1.0] * n,
        "DI_Plus_14": [20.0] * n, "DI_Minus_14": [20.0] * n, "ADX_14": [20.0] * n,
        "Stoch_K": [50.0] * n, "Stoch_D": [50.0] * n,
        "BB_Upper": [101.0] * n, "BB_Middle": [100.0] * n, "BB_Lower": [99.0] * n,
        "Support_50": [99.0] * n, "Resistance_50": [101.0] * n,
    }, index=idx)


class MondayMorningStalenessTest(unittest.TestCase):
    def test_friday_close_bar_passes_monday_morning(self):
        """The exact bug scenario: last complete 4h bucket is Friday
        13:30 ET (2026-07-17), checked Monday 10:30 ET (2026-07-20).
        ~66 wall-clock hours old, but only ~3.9 trading hours old."""
        last_ts = NY.localize(pd.Timestamp("2026-07-17 13:30:00"))
        now = NY.localize(pd.Timestamp("2026-07-20 10:30:00"))
        df = _make_ohlcv(last_ts)
        agent = MarketAnalystAgent()
        with patch("pandas.Timestamp.now", return_value=now), \
             patch("src.agents.market_analyst._is_us_equity_market_open", return_value=True):
            result = agent._validate_or_fault(df, "SPY@4h", tf="4h", asset="SPY")
        self.assertTrue(result, "Friday-close 4h bar checked Monday morning must pass")

    def test_genuinely_stale_across_many_trading_days_still_fails(self):
        """A feed stuck for a full trading WEEK (not just a weekend)
        must still fail -- the fix must not disable staleness
        detection entirely for equities."""
        last_ts = NY.localize(pd.Timestamp("2026-07-13 13:30:00"))  # a Monday
        now = NY.localize(pd.Timestamp("2026-07-20 10:30:00"))       # the next Monday
        df = _make_ohlcv(last_ts)
        agent = MarketAnalystAgent()
        with patch("pandas.Timestamp.now", return_value=now), \
             patch("src.agents.market_analyst._is_us_equity_market_open", return_value=True):
            result = agent._validate_or_fault(df, "SPY@4h", tf="4h", asset="SPY")
        self.assertFalse(result, "A full trading week with no new bar must still fail staleness")

    def test_trading_seconds_since_excludes_weekend(self):
        last_ts = NY.localize(pd.Timestamp("2026-07-17 13:30:00"))  # Friday
        now = NY.localize(pd.Timestamp("2026-07-20 10:30:00"))       # Monday
        elapsed = _trading_seconds_since(last_ts, now)
        # Friday 13:30->16:00 (2.5h) + Monday 09:30->10:30 (1h) = 3.5h
        self.assertAlmostEqual(elapsed, 3.5 * 3600, delta=1.0)

    def test_trading_seconds_since_same_session(self):
        last_ts = NY.localize(pd.Timestamp("2026-07-20 09:30:00"))
        now = NY.localize(pd.Timestamp("2026-07-20 11:30:00"))
        elapsed = _trading_seconds_since(last_ts, now)
        self.assertAlmostEqual(elapsed, 2 * 3600, delta=1.0)


if __name__ == "__main__":
    unittest.main()
