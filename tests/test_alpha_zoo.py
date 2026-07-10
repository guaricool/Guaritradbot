"""
Sprint 21 tests — Alpha Zoo (50+ indicators via the `ta` library).

Run: python -m unittest tests.test_alpha_zoo -v
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.features.alpha_zoo import compute_alpha_features, list_alpha_features, count_alpha_features


def _make_sample_df(n=300, seed=42):
    """Build a synthetic OHLCV dataframe for testing."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=n, freq="1h")
    close = 100 + np.cumsum(rng.standard_normal(n) * 0.5)
    df = pd.DataFrame({
        "Open": close + rng.standard_normal(n) * 0.1,
        "High": close + np.abs(rng.standard_normal(n)) * 0.5,
        "Low": close - np.abs(rng.standard_normal(n)) * 0.5,
        "Close": close,
        "Volume": rng.integers(1000, 10000, n).astype(float),
    }, index=dates)
    return df


class AlphaZooFeatureCountTest(unittest.TestCase):
    def test_at_least_50_alpha_features_added(self):
        df = _make_sample_df(n=300)
        out = compute_alpha_features(df)
        features = list_alpha_features(out)
        self.assertGreaterEqual(
            len(features), 50,
            f"Sprint 21 requires 50+ alpha features; got {len(features)}",
        )

    def test_ohlcv_columns_preserved(self):
        df = _make_sample_df()
        out = compute_alpha_features(df)
        for col in ("Open", "High", "Low", "Close", "Volume"):
            self.assertIn(col, out.columns)

    def test_features_naming_convention(self):
        df = _make_sample_df()
        out = compute_alpha_features(df)
        for f in list_alpha_features(out):
            self.assertTrue(f.startswith("alpha_"), f"Feature {f} missing alpha_ prefix")

    def test_catalog_matches_computed(self):
        """The documented catalog should match what compute_alpha_features actually produces."""
        catalog = count_alpha_features()
        documented = sum(len(v) for v in catalog.values())
        df = _make_sample_df()
        out = compute_alpha_features(df)
        computed = len(list_alpha_features(out))
        # computed can be >= documented if we added bonus indicators (PPO, KAMA)
        self.assertGreaterEqual(computed, documented)


class AlphaZooQualityTest(unittest.TestCase):
    def test_no_nans_in_final_row(self):
        """The most recent bar should have values for all indicators (NaN from warmup is at the start)."""
        df = _make_sample_df(n=400)
        out = compute_alpha_features(df)
        last_row = out[list_alpha_features(out)].iloc[-1]
        nan_count = int(last_row.isna().sum())
        self.assertEqual(nan_count, 0, f"Final row has {nan_count} NaN values")

    def test_known_indicators_match_reference(self):
        """Sanity check: RSI, MACD, BB should produce expected ranges."""
        df = _make_sample_df(n=300, seed=42)
        out = compute_alpha_features(df)
        # RSI is bounded 0..100 (dropna to skip warmup)
        rsi = out["alpha_rsi_14"].dropna()
        self.assertGreaterEqual(rsi.min(), 0)
        self.assertLessEqual(rsi.max(), 100)
        # Bollinger bands should approximately bracket price (after warmup).
        # For 2-sigma BB, ~95% of bars should be within. We use a more
        # lenient threshold (>=80%) because synthetic data can have
        # unusual volatility regimes.
        bb_low_diff = (out["Close"] - out["alpha_bb_low"]).dropna()
        bb_high_diff = (out["alpha_bb_high"] - out["Close"]).dropna()
        pct_within = ((bb_low_diff >= 0) & (bb_high_diff >= 0)).mean()
        self.assertGreaterEqual(
            pct_within, 0.80,
            f"Only {pct_within:.1%} of bars within BB (expected >=80%)",
        )

    def test_handles_missing_volume(self):
        """Alpha zoo should work even if Volume column is missing or all-NaN."""
        df = _make_sample_df()
        df_no_vol = df.drop(columns=["Volume"])
        out = compute_alpha_features(df_no_vol)
        # Should still produce alpha features (volume ones gracefully skipped)
        features = list_alpha_features(out)
        self.assertGreater(len(features), 30)
        # No NaN explosion
        last_nan = int(out[features].iloc[-1].isna().sum())
        self.assertLess(last_nan, 5)


class AlphaZooSelectiveTest(unittest.TestCase):
    def test_skip_momentum_only(self):
        df = _make_sample_df()
        out = compute_alpha_features(
            df,
            include_momentum=False,
            include_trend=True,
            include_volatility=True,
            include_volume=True,
        )
        features = list_alpha_features(out)
        # No momentum features
        for f in features:
            self.assertFalse(
                f in ("alpha_rsi_14", "alpha_stoch", "alpha_williams_r"),
                f"Momentum feature {f} should have been skipped",
            )

    def test_skip_volume(self):
        df = _make_sample_df()
        out = compute_alpha_features(
            df,
            include_momentum=True,
            include_trend=True,
            include_volatility=True,
            include_volume=False,
        )
        features = list_alpha_features(out)
        # No volume features
        for f in features:
            self.assertFalse(
                f in ("alpha_obv", "alpha_cmf", "alpha_eom"),
                f"Volume feature {f} should have been skipped",
            )


if __name__ == "__main__":
    unittest.main()