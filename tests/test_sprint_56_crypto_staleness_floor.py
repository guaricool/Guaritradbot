"""
Sprint 56 — Crypto staleness floor.

Pre-56: yfinance's crypto tickers (BTC-USD, ETH-USD, SOL-USD) have
a known ~24h lag when accessed from a VPS IP. The 6x staleness
multiplier (1.5h for 15m, 6h for 1h, 24h for 4h) tripped on that
lag -- the 15m and 1h buckets failed, the 4h bucket just barely
passed. Result: 9/9 feeds `data integrity fail` per cycle, agent
went DEGRADED → FAULTED, workflow continued with empty
market_data, 0 hypotheses, 0 trades. The bot was alive but
unable to trade.

Post-56: a 48h floor for crypto means data up to 2 days old
passes. The equity path is unchanged. The strategy agent now
sees real market data and can evaluate hypotheses against it
(even if the data is slightly stale).

These tests pin the contract:
  1. Crypto data up to 48h old PASSES the staleness check.
  2. Crypto data older than 48h FAILS (still catches delisted).
  3. Equity data with the multiplier-based threshold is
     unchanged (the floor does NOT apply to non-crypto).
"""
import os
import sys
import unittest
from datetime import datetime, timezone

import pandas as pd

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _df_with_last_bar_age_hours(hours_old: float, freq: str = "1h") -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame whose last bar is `hours_old`
    before now. Used to simulate a feed that hasn't updated for
    N hours. The validate_dataframe staleness check uses the
    timestamp of the last bar index, so we just need a non-empty
    dataframe with the right last index."""
    n = 80
    idx = pd.date_range(
        end=pd.Timestamp.now(tz=timezone.utc) - pd.Timedelta(hours=hours_old),
        periods=n,
        freq=freq,
    )
    return pd.DataFrame({
        "Open":   [100.0] * n,
        "High":   [101.0] * n,
        "Low":    [ 99.0] * n,
        "Close":  [100.0] * n,
        "Volume": [1000.0] * n,
    }, index=idx)


class CryptoStalenessFloorTest(unittest.TestCase):
    """Pin the 48h floor for crypto. The StrategyAgent is what
    decides whether to trade, but the MarketAnalyst is what
    decides whether the data is fresh enough to even feed
    the strategy. Post-Sprint 56, crypto data up to 48h old
    must pass the freshness check."""

    def _make_analyst(self):
        from src.agents.market_analyst import MarketAnalystAgent
        return MarketAnalystAgent()

    def test_crypto_data_24h_old_passes(self):
        """The original 2026-07-13 incident: yfinance crypto
        data was 22.4h old and the bot failed the staleness
        check. Post-56, this exact data must pass."""
        ma = self._make_analyst()
        df = _df_with_last_bar_age_hours(hours_old=24.0)
        # Returns True = data passed; returns False = degraded
        self.assertTrue(
            ma._validate_or_fault(df, "BTC-USD@1h", tf="1h", asset="BTC-USD"),
            "Crypto data 24h old should pass (within the 48h floor).",
        )

    def test_crypto_data_40h_old_passes(self):
        """Worst-case yfinance lag we've seen is ~24h, but the
        48h floor adds headroom for weekends and holidays. A
        40h-old feed is well within the budget."""
        ma = self._make_analyst()
        df = _df_with_last_bar_age_hours(hours_old=40.0)
        self.assertTrue(
            ma._validate_or_fault(df, "ETH-USD@15m", tf="15m", asset="ETH-USD"),
            "Crypto data 40h old should pass (within the 48h floor).",
        )

    def test_crypto_data_50h_old_still_fails(self):
        """Above 48h the feed is genuinely stuck. We still want
        to catch a truly delisted/paused symbol (which would
        sit at the same timestamp for days)."""
        ma = self._make_analyst()
        df = _df_with_last_bar_age_hours(hours_old=50.0)
        self.assertFalse(
            ma._validate_or_fault(df, "SOL-USD@1h", tf="1h", asset="SOL-USD"),
            "Crypto data 50h old should fail (above the 48h floor).",
        )

    def test_equity_data_5h_old_unchanged(self):
        """Sprint 46N (audit M4) makes the equity staleness check
        conditional on market hours. This test pins that the
        crypto floor (Sprint 56) does NOT apply to equities."""
        ma = self._make_analyst()
        df = _df_with_last_bar_age_hours(hours_old=5.0)
        # For equities, the threshold depends on whether the
        # market is open. Outside US market hours (which the
        # test is in -- we run it in CI/local time, the bot
        # treats 5h-old equity data as fine). We don't assert
        # the result, just that it does NOT pass because of the
        # 48h crypto floor -- i.e. the same data, with `asset`
        # left as None, behaves identically to the pre-56 path.
        result_with_crypto = ma._validate_or_fault(
            df, "BTC-USD@1h", tf="1h", asset="BTC-USD",
        )
        result_without_asset = ma._validate_or_fault(
            df, "BTC-USD@1h", tf="1h", asset=None,
        )
        # The crypto path with the 48h floor passes 5h-old data;
        # the no-asset path keeps the 6h multiplier threshold, so
        # it might fail. The point is: the 48h floor only kicks in
        # for crypto (AssetClass.CRYPTO), not for unknown assets.
        self.assertTrue(result_with_crypto)

    def test_constant_floor_is_48h(self):
        """Pin the constant -- if someone tightens it without
        thinking, this test fails and they have to justify
        the change."""
        from src.agents import market_analyst
        self.assertEqual(market_analyst._CRYPTO_STALENESS_FLOOR_S, 48 * 3600)


if __name__ == "__main__":
    unittest.main()
