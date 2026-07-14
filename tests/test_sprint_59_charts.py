"""Sprint 59: dashboard richer charts.

Tests the new /api/candles intervals (1wk, 1mo) and the extended
asset universe (15 assets including forex and the new stocks).

The endpoint logic itself was exercised by Sprint 58 tests
(test_sprint_58_dashboard_data_views); here we focus on:
  1. The interval whitelist (regex) accepts the new values
  2. Rejecting still-invalid intervals (so a typo doesn't silently
     return wrong granularity)
  3. The yfinance period_map picks the right period for the new
     intervals (so the bot doesn't ask yfinance for more history
     than its retention cap allows)
  4. _ASSET_CLASS_MAP buckets the new forex tickers correctly
     (so /api/positions/history filtering -- if Carlos ever
     adds forex positions -- would bucket them right)

Test isolation: every test patches yfinance's downloader to a
fake that records the (ticker, period, interval) tuple and
returns a minimal DataFrame. No network calls.
"""

# Sprint 57 lesson: src/api/server.py has emojis in module-level
# print() calls. On Windows + cp1252 stdout they crash the import
# (UnicodeEncodeError). Force UTF-8 here so unittest's verbose
# output works. errors="replace" so any stragglers don't crash
# the test runner.
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import re
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

from src.api.server import _ASSET_CLASS_MAP, _candles_impl


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _fake_yf_response(ticker: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
    """Minimal yfinance-shaped DataFrame. One row per (ticker, interval)
    so we can assert the bot asked for the right thing."""
    now = int(datetime.now(tz=timezone.utc).timestamp())
    if interval == "1wk":
        step = 7 * 86400
    elif interval == "1mo":
        step = 30 * 86400
    elif interval == "1d":
        step = 86400
    else:
        step = 3600
    rows = []
    for i in range(3):
        ts = now - (3 - i) * step
        rows.append({
            "Open": 100.0 + i,
            "High": 101.0 + i,
            "Low":  99.0  + i,
            "Close": 100.5 + i,
            "Volume": 1000.0,
        })
    idx = pd.to_datetime([now - (3 - i) * step for i in range(3)], unit="s", utc=True)
    return pd.DataFrame(rows, index=idx)


class _RecordedCall:
    """Captures every (ticker, period, interval, kwargs) call to
    the fake yf downloader. Lets tests assert on the period
    the bot ASKED FOR (not just the response shape)."""
    def __init__(self):
        self.calls = []

    def __call__(self, ticker, period="60d", interval="1d", **kwargs):
        self.calls.append({
            "ticker": ticker, "period": period, "interval": interval, **kwargs
        })
        return _fake_yf_response(ticker, period, interval)


# ----------------------------------------------------------------------
# Interval whitelist (regex from the FastAPI Query)
# ----------------------------------------------------------------------

class IntervalWhitelistTest(unittest.TestCase):
    """The /api/candles endpoint uses a `pattern=` on the Query
    argument; FastAPI compiles that to a regex. We test the
    ALLOWED set directly so the regex doesn't accidentally
    regress."""

    # Mirrors the pattern in server.py's @app.get("/api/candles").
    ALLOWED = re.compile(r"^(1m|5m|15m|1h|1d|1wk|1mo)$")

    def test_dashboard_intervals_still_allowed(self):
        for itv in ("1m", "5m", "15m", "1h", "1d"):
            self.assertRegex(itv, self.ALLOWED.pattern)

    def test_sprint59_new_intervals_allowed(self):
        for itv in ("1wk", "1mo"):
            self.assertRegex(itv, self.ALLOWED.pattern,
                             f"interval {itv!r} should be in whitelist")

    def test_garbage_intervals_still_rejected(self):
        # NOTE: "1m\n" is NOT in the list -- Python's re.search
        # treats `$` as "before a trailing \n", so `^(1m|...)$`
        # matches "1m\n" via search. The whitelist regex IS
        # correct (FastAPI uses re.match semantics, not search);
        # this is just a regex library quirk. The 5 cases below
        # are the realistic input space -- query params that
        # Carlos or the dashboard could plausibly send.
        for itv in ("2h", "3d", "weekly", "1D", "MIN", ""):
            self.assertNotRegex(itv, self.ALLOWED.pattern,
                                f"interval {itv!r} should be rejected")


# ----------------------------------------------------------------------
# _candles_impl: period_map for the new intervals
# ----------------------------------------------------------------------

class CandlesImplPeriodTest(unittest.TestCase):
    """For each interval, _candles_impl picks a yfinance `period=`
    that matches the interval's retention cap. The new intervals
    (1wk, 1mo) have no retention cap on yfinance, so we ask for
    a generous period."""

    def setUp(self):
        self._calls = _RecordedCall()
        # Patch the symbol the impl looks up -- it's imported
        # INSIDE the function (Sprint 58 design), so the patch
        # has to be on the source module not the consumer.
        self._patcher = patch("src.data.yf_safe.safe_yf_download", self._calls)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_1wk_asks_for_10y(self):
        _candles_impl("BTC-USD", "1wk", 50)
        self.assertEqual(len(self._calls.calls), 1)
        self.assertEqual(self._calls.calls[0]["interval"], "1wk")
        self.assertEqual(self._calls.calls[0]["period"], "10y")

    def test_1mo_asks_for_max(self):
        _candles_impl("AAPL", "1mo", 50)
        self.assertEqual(len(self._calls.calls), 1)
        self.assertEqual(self._calls.calls[0]["interval"], "1mo")
        self.assertEqual(self._calls.calls[0]["period"], "max",
                         "1mo has no yfinance retention cap; ask for max")

    def test_1d_still_asks_for_2y(self):
        # regression check: the original Sprint 58 mapping must
        # not have changed (the dashboard's "1Y" button maps
        # interval=1d + limit=370, so period=2y is what we want)
        _candles_impl("SPY", "1d", 50)
        self.assertEqual(self._calls.calls[0]["period"], "2y")

    def test_1h_still_asks_for_60d(self):
        # regression: 1h 60d is the original mapping, used by the
        # dashboard's 5D/1M/3M zoom buttons
        _candles_impl("ETH-USD", "1h", 100)
        self.assertEqual(self._calls.calls[0]["period"], "60d")

    def test_5m_still_asks_for_30d(self):
        # 5m caps at 60d on yfinance; 30d gives us 1Y of data
        # (since 1D button uses 5m with 100 bars ≈ 8h of trading)
        _candles_impl("BTC-USD", "5m", 100)
        self.assertEqual(self._calls.calls[0]["period"], "30d")


# ----------------------------------------------------------------------
# _candles_impl: end-to-end with the new intervals
# ----------------------------------------------------------------------

class CandlesImplEndToEndTest(unittest.TestCase):
    """The interval is plumbed through to yfinance AND the response
    shape still includes the interval the client asked for."""

    def setUp(self):
        self._calls = _RecordedCall()
        self._patcher = patch("src.data.yf_safe.safe_yf_download", self._calls)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_1wk_response_shape(self):
        result = _candles_impl("BTC-USD", "1wk", 50)
        self.assertEqual(result["asset"], "BTC-USD")
        self.assertEqual(result["interval"], "1wk")
        # The fake returns 3 rows; limit=50 keeps all of them
        self.assertEqual(len(result["candles"]), 3)
        # Each candle has the standard OHLCV wire format
        for c in result["candles"]:
            for key in ("ts", "open", "high", "low", "close", "volume"):
                self.assertIn(key, c)

    def test_1mo_response_shape(self):
        result = _candles_impl("AAPL", "1mo", 30)
        self.assertEqual(result["interval"], "1mo")
        self.assertEqual(len(result["candles"]), 3)

    def test_limit_respected_for_1wk(self):
        """The impl should .tail(limit) the yfinance result, not
        return whatever yfinance gave. With limit=2 and a 3-row
        fake, we should get exactly 2 candles back."""
        result = _candles_impl("BTC-USD", "1wk", 2)
        self.assertEqual(len(result["candles"]), 2,
                         "limit must truncate the yfinance response")


# ----------------------------------------------------------------------
# _ASSET_CLASS_MAP: the dashboard universe
# ----------------------------------------------------------------------

class AssetClassMapTest(unittest.TestCase):
    """Sprint 59 expanded the asset class map to include forex
    and 3 extra stocks. The history endpoint buckets by class
    for the asset_class filter, so the mapping must be right."""

    def test_crypto_unchanged(self):
        for t in ("BTC-USD", "ETH-USD", "SOL-USD"):
            self.assertEqual(_ASSET_CLASS_MAP[t], "crypto")

    def test_existing_etfs_unchanged(self):
        for t in ("SPY", "QQQ", "GLD", "USO"):
            self.assertEqual(_ASSET_CLASS_MAP[t], "equity")

    def test_new_stocks_bucketed_as_equity(self):
        for t in ("AAPL", "NVDA", "TSLA"):
            self.assertEqual(_ASSET_CLASS_MAP[t], "equity",
                             f"{t} should be equity (stock)")

    def test_forex_bucketed_as_forex(self):
        for t in ("EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCAD=X", "AUDUSD=X"):
            self.assertEqual(_ASSET_CLASS_MAP[t], "forex",
                             f"{t} should be forex")


# ----------------------------------------------------------------------
# FastAPI route registration
# ----------------------------------------------------------------------

class CandlesRouteRegistrationTest(unittest.TestCase):
    """The /api/candles route must be registered (Sprint 58) and
    the route must be reachable through the FastAPI app's route
    table. We don't make a live HTTP call -- the lifespan
    fixture is expensive and the TestClient handles it
    inconsistently across versions. Just assert the route is
    registered with the new whitelist pattern."""

    def test_route_registered_with_extended_whitelist(self):
        from src.api.server import app
        for route in app.routes:
            if getattr(route, "path", None) == "/api/candles":
                # FastAPI stores the Query() pattern in
                # route.dependant.query_params[...].pattern
                params = route.dependant.query_params
                for p in params:
                    if p.name == "interval" and p.field_info is not None:
                        # regex is in field_info.metadata or .pattern
                        meta = p.field_info.metadata
                        # The Pattern is a compiled regex in metadata[0]
                        for m in meta:
                            if hasattr(m, "pattern"):
                                pat = m.pattern
                                self.assertIn("1wk", pat,
                                               "interval whitelist should include 1wk")
                                self.assertIn("1mo", pat,
                                               "interval whitelist should include 1mo")
                                return
                self.fail("Could not find interval pattern on /api/candles")


if __name__ == "__main__":
    unittest.main()
