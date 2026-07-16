"""
Category (asset-class) historical performance scoring —
src/analysis/category_scoring.py.

Gap identified from the kalshi-ai-trading-bot reference project:
gate position sizing by each category's historical win rate, using
this bot's existing decision_log outcomes + asset_class taxonomy.
"""
import unittest

from src.analysis.category_scoring import (
    CategoryStats,
    asset_category_multiplier,
    category_risk_multiplier,
    compute_category_stats,
)


def _outcome(asset, pnl_usd, pnl_pct=None):
    return {
        "kind": "outcome",
        "asset": asset,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct if pnl_pct is not None else pnl_usd,
    }


class ComputeCategoryStatsTest(unittest.TestCase):
    def test_aggregates_by_asset_class_not_by_symbol(self):
        records = [
            _outcome("BTC-USD", 10.0),
            _outcome("ETH-USD", -5.0),
            _outcome("SOL-USD", 3.0),
            _outcome("SPY", 2.0),
        ]
        stats = compute_category_stats(records)
        self.assertIn("crypto", stats)
        self.assertEqual(stats["crypto"].trades, 3)
        self.assertEqual(stats["crypto"].wins, 2)
        self.assertEqual(stats["crypto"].losses, 1)
        self.assertAlmostEqual(stats["crypto"].win_rate, 2 / 3)
        self.assertIn("equity_growth", stats)
        self.assertEqual(stats["equity_growth"].trades, 1)

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(compute_category_stats([]), {})

    def test_malformed_records_are_skipped_not_raised(self):
        records = [
            {"kind": "outcome", "asset": "BTC-USD"},  # missing pnl fields -> default 0.0
            {"kind": "hypothesis", "asset": "BTC-USD", "pnl_usd": 5.0, "pnl_pct": 5.0},  # wrong kind
            {"asset": None, "pnl_usd": 1.0, "pnl_pct": 1.0},  # no asset
            "not a dict",
        ]
        stats = compute_category_stats(records)
        # Only the first record (defaults to 0.0 pnl) counts.
        self.assertEqual(stats["crypto"].trades, 1)
        self.assertEqual(stats["crypto"].wins, 0)


class CategoryRiskMultiplierTest(unittest.TestCase):
    def test_none_stats_is_neutral(self):
        self.assertEqual(category_risk_multiplier(None), 1.0)

    def test_too_few_trades_is_neutral_even_with_bad_win_rate(self):
        stats = CategoryStats("crypto", trades=3, wins=0, losses=3,
                               win_rate=0.0, total_pnl_usd=-10.0, avg_pnl_pct=-5.0)
        self.assertEqual(category_risk_multiplier(stats, min_trades=10), 1.0)

    def test_good_win_rate_stays_neutral_never_boosted_above_one(self):
        stats = CategoryStats("crypto", trades=20, wins=15, losses=5,
                               win_rate=0.75, total_pnl_usd=50.0, avg_pnl_pct=2.0)
        self.assertEqual(category_risk_multiplier(stats, min_trades=10), 1.0)

    def test_poor_win_rate_with_enough_trades_reduces(self):
        stats = CategoryStats("crypto", trades=20, wins=5, losses=15,
                               win_rate=0.25, total_pnl_usd=-30.0, avg_pnl_pct=-3.0)
        mult = category_risk_multiplier(
            stats, min_trades=10, poor_win_rate_threshold=0.35, reduction_factor=0.5,
        )
        self.assertEqual(mult, 0.5)

    def test_reduction_never_exceeds_configured_factor(self):
        stats = CategoryStats("crypto", trades=100, wins=1, losses=99,
                               win_rate=0.01, total_pnl_usd=-500.0, avg_pnl_pct=-10.0)
        mult = category_risk_multiplier(stats, min_trades=10, reduction_factor=0.5)
        self.assertEqual(mult, 0.5)  # not further reduced for a worse win rate


class AssetCategoryMultiplierTest(unittest.TestCase):
    def test_end_to_end_symbol_lookup(self):
        records = [_outcome("BTC-USD", -1.0)] * 5 + [_outcome("BTC-USD", -1.0)] * 10
        mult = asset_category_multiplier("ETH-USD", records, min_trades=10)
        # ETH-USD is also `crypto`, so BTC-USD's poor track record
        # (0% win rate, 15 trades) should affect it too.
        self.assertEqual(mult, 0.5)

    def test_unknown_symbol_defaults_to_cash_and_is_neutral(self):
        records = [_outcome("BTC-USD", -1.0)] * 20
        mult = asset_category_multiplier("UNKNOWN-TICKER", records, min_trades=10)
        self.assertEqual(mult, 1.0)


if __name__ == "__main__":
    unittest.main()
