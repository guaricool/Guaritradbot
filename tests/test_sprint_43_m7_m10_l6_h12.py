"""
Sprint 43 — M7 (sidebar session_state), M10 (orphan data paths),
L6 (bfill removed), H12 (in-progress bar dropped).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# M10: the deprecated modules must raise NotImplementedError
# when their functions are called.
class M10OrphanDataPathsTest(unittest.TestCase):
    def test_download_data_raises(self):
        from src.data.download import download_data
        with self.assertRaises(NotImplementedError) as ctx:
            download_data("BTC-USD", "1h", period="60d")
        self.assertIn("deprecated", str(ctx.exception).lower())

    def test_historical_fetch_raises(self):
        from src.data.historical import fetch_historical_data
        with self.assertRaises(NotImplementedError) as ctx:
            fetch_historical_data("BTC/USDT", "1d", "2023-01-01T00:00:00Z")
        self.assertIn("deprecated", str(ctx.exception).lower())

    def test_deprecated_files_have_deprecation_warnings(self):
        """The deprecation should be visible in the module docstring."""
        for path in ["src/data/download.py", "src/data/historical.py"]:
            with open(os.path.join(ROOT, path), encoding="utf-8") as f:
                content = f.read()
            self.assertIn("deprecated", content.lower())


# L6: bfill() removed from _bollinger and _support_resistance.
class L6NoBfillInWarmupTest(unittest.TestCase):
    def test_bollinger_keeps_warmup_nans(self):
        """The first `period` bars must be NaN (warmup), not bfilled
        from later values. bfill was a look-ahead bias."""
        from src.agents.market_analyst import _bollinger
        # 30 bars, period=20 → bars 0-19 should be NaN, bar 20+ valid
        n = 30
        df = pd.DataFrame({
            "Close": [100.0 + 0.1 * i for i in range(n)],
            "High": [101.0 + 0.1 * i for i in range(n)],
            "Low":  [99.0 + 0.1 * i for i in range(n)],
            "Open": [100.0 + 0.1 * i for i in range(n)],
            "Volume": [1000.0] * n,
        })
        upper, middle, lower = _bollinger(df, period=20, std_dev=2.0)
        # First 20 rows are warmup — must be NaN (or all NaN in
        # the upper/lower; the middle also starts at row 19).
        self.assertTrue(pd.isna(upper.iloc[:19]).all(),
                        f"First 19 upper values must be NaN (warmup), got {upper.iloc[:19].tolist()}")
        # Row 20+ should have valid values
        self.assertTrue(pd.notna(upper.iloc[20:]).all())

    def test_support_resistance_keeps_warmup_nans(self):
        from src.agents.market_analyst import _support_resistance
        n = 60
        df = pd.DataFrame({
            "Close": [100.0 + 0.1 * i for i in range(n)],
            "High": [101.0 + 0.1 * i for i in range(n)],
            "Low":  [99.0 + 0.1 * i for i in range(n)],
            "Open": [100.0 + 0.1 * i for i in range(n)],
            "Volume": [1000.0] * n,
        })
        support, resistance = _support_resistance(df, window=50)
        # First 49 rows are warmup — must be NaN
        self.assertTrue(pd.isna(support.iloc[:49]).all(),
                        f"First 49 support values must be NaN (warmup)")


# H12: in-progress bar dropped from resample.
class H12ResampleDropsInProgressBarTest(unittest.TestCase):
    def test_resample_drops_partial_last_bucket(self):
        """If the last 60m bar is at 14:30, the 12:00-16:00 4h bucket
        is in-progress (only 2.5h old). It must be dropped. The
        earlier 3 complete buckets (00-04, 04-08, 08-12) are kept."""
        from src.agents.market_analyst import _resample_ohlcv
        # 15 hourly bars: 00:00 through 14:00. Buckets (4h):
        #   [00:00, 04:00): bars 00, 01, 02, 03 → 4 bars (complete)
        #   [04:00, 08:00): bars 04, 05, 06, 07 → 4 bars (complete)
        #   [08:00, 12:00): bars 08, 09, 10, 11 → 4 bars (complete)
        #   [12:00, 16:00): bars 12, 13, 14    → 3 bars (INCOMPLETE)
        # H12 fix: drop the incomplete one → 3 buckets remain.
        idx = pd.date_range("2026-07-10 00:00", periods=15, freq="1h")
        df = pd.DataFrame({
            "Open": [100.0 + i for i in range(len(idx))],
            "High": [101.0 + i for i in range(len(idx))],
            "Low":  [99.0 + i for i in range(len(idx))],
            "Close": [100.5 + i for i in range(len(idx))],
            "Volume": [100.0] * len(idx),
        }, index=idx)
        resampled = _resample_ohlcv(df, "4h")
        self.assertEqual(len(resampled), 3,
                         f"Expected 3 complete 4h buckets, got {len(resampled)}: {resampled}")

    def test_resample_keeps_complete_buckets(self):
        """If the last 60m bar ends a complete bucket, that bucket
        should be KEPT."""
        from src.agents.market_analyst import _resample_ohlcv
        # 8 hourly bars from 00:00 to 07:00 — bucket 04:00-08:00 is COMPLETE.
        idx = pd.date_range("2026-07-10 00:00", periods=8, freq="1h")
        df = pd.DataFrame({
            "Open": [100.0 + i for i in range(len(idx))],
            "High": [101.0 + i for i in range(len(idx))],
            "Low":  [99.0 + i for i in range(len(idx))],
            "Close": [100.5 + i for i in range(len(idx))],
            "Volume": [100.0] * len(idx),
        }, index=idx)
        resampled = _resample_ohlcv(df, "4h")
        # Bucket 00:00-04:00 is complete (4 hourly bars: 00, 01, 02, 03)
        # Bucket 04:00-08:00 is complete (4 hourly bars: 04, 05, 06, 07)
        # Both should be present.
        self.assertEqual(len(resampled), 2,
                         f"Expected 2 complete 4h buckets, got {len(resampled)}: {resampled}")


# M7: sidebar variables go through session_state. The dashboard
# is hard to unit-test without running Streamlit, so we just
# check that the pattern is used (no direct `var = st.checkbox`
# then `if var:` in the main area without session_state in between).
class M7SidebarSessionStateTest(unittest.TestCase):
    def test_dashboard_uses_session_state_for_sidebar_vars(self):
        with open(os.path.join(ROOT, "dashboard.py"), encoding="utf-8") as f:
            content = f.read()
        # Each sidebar var should be written to st.session_state
        for var in ["refresh_sec", "show_news_panel", "show_signals", "show_audit"]:
            self.assertIn(
                f'st.session_state["{var}"]',
                content,
                f"Dashboard should write {var} to st.session_state (M7 fix)",
            )

    def test_dashboard_reads_sidebar_vars_via_session_state_in_main(self):
        """The main area should READ via st.session_state.get, not
        rely on a variable that was declared inside `with st.sidebar:`."""
        with open(os.path.join(ROOT, "dashboard.py"), encoding="utf-8") as f:
            content = f.read()
        # Look for `st.session_state.get("refresh_sec", ...)` pattern
        self.assertIn('st.session_state.get("refresh_sec"', content)


if __name__ == "__main__":
    unittest.main()
