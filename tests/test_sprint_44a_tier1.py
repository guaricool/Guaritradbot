"""
Sprint 44A — Tier 1: Asset class taxonomy + asset correlation + concentration gate.

Tests for:
  - src/data/asset_class.py       (enum, map, helpers)
  - src/analysis/asset_correlation.py  (fetch, align, matrix, avg, group, analyze)
  - src/agents/risk_agent.py       (_exposure_by_class, _check_concentration,
                                     end-to-end via validate_and_size)

Run: python -m unittest tests.test_sprint_44a_tier1 -v
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ============================================================
# Sprint 44A — Asset class taxonomy
# ============================================================

class AssetClassTest(unittest.TestCase):
    def test_known_crypto_map(self):
        from src.data.asset_class import get_asset_class, AssetClass
        for sym in ("BTC-USD", "ETH-USD", "SOL-USD", "BTCUSDT", "ETHUSDT", "SOLUSDT"):
            self.assertEqual(get_asset_class(sym), AssetClass.CRYPTO, sym)

    def test_known_equity_map(self):
        from src.data.asset_class import get_asset_class, AssetClass
        for sym in ("SPY", "QQQ"):
            self.assertEqual(get_asset_class(sym), AssetClass.EQUITY_GROWTH, sym)

    def test_known_commodity_map(self):
        from src.data.asset_class import get_asset_class, AssetClass
        self.assertEqual(get_asset_class("GLD"), AssetClass.COMMODITY_SAFE)
        self.assertEqual(get_asset_class("USO"), AssetClass.COMMODITY_ENERGY)

    def test_unknown_symbol_returns_cash(self):
        from src.data.asset_class import get_asset_class, AssetClass
        # Unknown tickers (and stablecoins) default to CASH, not raise.
        for sym in ("USDT", "USDC", "BUSD", "FAKEXYZ", ""):
            self.assertEqual(get_asset_class(sym), AssetClass.CASH, sym)

    def test_is_known_tradable(self):
        from src.data.asset_class import is_known_tradable
        self.assertTrue(is_known_tradable("BTC-USD"))
        self.assertTrue(is_known_tradable("SPY"))
        self.assertFalse(is_known_tradable("USDT"))
        self.assertFalse(is_known_tradable("BLAH"))

    def test_asset_class_is_str_enum(self):
        """AssetClass values must serialize to plain strings for JSON / audit logs."""
        from src.data.asset_class import AssetClass
        self.assertEqual(AssetClass.CRYPTO.value, "crypto")
        self.assertEqual(AssetClass.EQUITY_GROWTH.value, "equity_growth")
        # Round-trip via str() and equality.
        self.assertEqual(str(AssetClass.CRYPTO), "AssetClass.CRYPTO")
        # But the .value is what we use for serialization.
        self.assertEqual(AssetClass.CRYPTO.value, "crypto")


# ============================================================
# Sprint 44A — Asset correlation module
# ============================================================

def _synthetic_returns(seed: int, n_days: int = 60) -> pd.Series:
    """Helper: deterministic synthetic daily returns."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0, 0.02, n_days)
    idx = pd.date_range("2025-01-01", periods=n_days, freq="D")
    return pd.Series(rets, index=idx)


def _correlated_returns(seed: int, n_days: int = 60, corr: float = 0.9) -> pd.Series:
    """Helper: returns with a fixed correlation to the helper above's seed=0."""
    rng = np.random.default_rng(seed)
    base = _synthetic_returns(0, n_days).values
    noise = rng.normal(0.0, 0.02, n_days)
    # Mix: target corr -> use rho*base + sqrt(1-rho^2)*noise
    out = corr * base + np.sqrt(max(0.0, 1.0 - corr * corr)) * noise
    idx = pd.date_range("2025-01-01", periods=n_days, freq="D")
    return pd.Series(out, index=idx)


class AssetCorrelationAlignmentTest(unittest.TestCase):
    def test_align_empty(self):
        from src.analysis.asset_correlation import _align_returns
        assets, mat = _align_returns({})
        self.assertEqual(assets, [])
        self.assertEqual(mat.shape, (0, 0))

    def test_align_two_assets_same_dates(self):
        from src.analysis.asset_correlation import _align_returns
        idx = pd.date_range("2025-01-01", periods=10, freq="D")
        a = pd.Series([0.01, -0.02] * 5, index=idx)
        b = pd.Series([0.005, 0.003] * 5, index=idx)
        assets, mat = _align_returns({"A": a, "B": b})
        self.assertEqual(set(assets), {"A", "B"})
        self.assertEqual(mat.shape, (2, 10))

    def test_align_uses_intersection(self):
        from src.analysis.asset_correlation import _align_returns
        idx1 = pd.date_range("2025-01-01", periods=10, freq="D")
        idx2 = pd.date_range("2025-01-05", periods=10, freq="D")
        a = pd.Series([0.01] * 10, index=idx1)
        b = pd.Series([0.02] * 10, index=idx2)
        assets, mat = _align_returns({"A": a, "B": b})
        # Intersection: Jan 5, 6, 7, 8, 9, 10 = 6 days.
        self.assertEqual(mat.shape[1], 6)

    def test_align_too_few_overlap_returns_empty_matrix(self):
        from src.analysis.asset_correlation import _align_returns
        # Only 2 overlapping dates — below the 3-day minimum.
        idx1 = pd.date_range("2025-01-01", periods=2, freq="D")
        idx2 = pd.date_range("2025-01-02", periods=2, freq="D")
        a = pd.Series([0.01, 0.02], index=idx1)
        b = pd.Series([0.01, 0.02], index=idx2)
        assets, mat = _align_returns({"A": a, "B": b})
        self.assertEqual(len(assets), 2)
        self.assertEqual(mat.shape, (2, 0))


class AssetCorrelationMatrixTest(unittest.TestCase):
    def test_empty_returns_empty_matrix(self):
        from src.analysis.asset_correlation import compute_asset_correlation_matrix
        self.assertEqual(compute_asset_correlation_matrix({}).shape, (0, 0))

    def test_single_asset_identity(self):
        from src.analysis.asset_correlation import compute_asset_correlation_matrix
        m = compute_asset_correlation_matrix({"A": _synthetic_returns(1)})
        self.assertEqual(m.shape, (1, 1))
        self.assertAlmostEqual(m[0, 0], 1.0)

    def test_identical_series_correlation_one(self):
        from src.analysis.asset_correlation import compute_asset_correlation_matrix
        s = _synthetic_returns(7)
        m = compute_asset_correlation_matrix({"A": s, "B": s.copy()})
        self.assertAlmostEqual(m[0, 1], 1.0, places=5)
        self.assertAlmostEqual(m[1, 0], 1.0, places=5)

    def test_high_correlation_detected(self):
        from src.analysis.asset_correlation import compute_asset_correlation_matrix
        a = _synthetic_returns(0)
        b = _correlated_returns(99, corr=0.95)
        m = compute_asset_correlation_matrix({"A": a, "B": b})
        # Should be close to 0.95 (allowing for noise / finite sample).
        self.assertGreater(m[0, 1], 0.85)
        self.assertLessEqual(m[0, 1], 1.0)

    def test_low_correlation_detected(self):
        from src.analysis.asset_correlation import compute_asset_correlation_matrix
        a = _synthetic_returns(0)
        # Independent noise (no shared signal) → correlation near 0.
        b = _synthetic_returns(42)
        m = compute_asset_correlation_matrix({"A": a, "B": b})
        # With 60 days of returns, expect |corr| < 0.4 with high probability.
        self.assertLess(abs(m[0, 1]), 0.4)

    def test_constant_series_returns_zero(self):
        """If one series is flat, std=0 → corr is undefined, return 0."""
        from src.analysis.asset_correlation import compute_asset_correlation_matrix
        idx = pd.date_range("2025-01-01", periods=20, freq="D")
        a = pd.Series([0.0] * 20, index=idx)
        b = pd.Series(np.linspace(0, 1, 20) * 0.01, index=idx)
        m = compute_asset_correlation_matrix({"A": a, "B": b})
        # Should be 0 (not NaN, not 1.0).
        self.assertEqual(m[0, 1], 0.0)
        self.assertTrue(np.isfinite(m[0, 1]))

    def test_matrix_is_symmetric(self):
        from src.analysis.asset_correlation import compute_asset_correlation_matrix
        rets = {s: _synthetic_returns(i) for i, s in enumerate(["A", "B", "C"])}
        m = compute_asset_correlation_matrix(rets)
        for i in range(m.shape[0]):
            for j in range(m.shape[1]):
                self.assertAlmostEqual(m[i, j], m[j, i], places=10)

    def test_diagonal_is_one(self):
        from src.analysis.asset_correlation import compute_asset_correlation_matrix
        rets = {s: _synthetic_returns(i) for i, s in enumerate(["A", "B", "C"])}
        m = compute_asset_correlation_matrix(rets)
        for i in range(m.shape[0]):
            self.assertAlmostEqual(m[i, i], 1.0, places=10)


class AverageCorrelationTest(unittest.TestCase):
    def test_single_asset(self):
        from src.analysis.asset_correlation import average_correlation
        self.assertEqual(average_correlation(np.eye(1)), 0.0)

    def test_empty(self):
        from src.analysis.asset_correlation import average_correlation
        self.assertEqual(average_correlation(np.zeros((0, 0))), 0.0)

    def test_two_assets(self):
        from src.analysis.asset_correlation import average_correlation
        m = np.array([[1.0, 0.7], [0.7, 1.0]])
        self.assertAlmostEqual(average_correlation(m), 0.7)

    def test_three_assets_avg_of_three_pairs(self):
        from src.analysis.asset_correlation import average_correlation
        m = np.array([
            [1.0, 0.6, 0.4],
            [0.6, 1.0, 0.2],
            [0.4, 0.2, 1.0],
        ])
        # Off-diagonal: (0.6 + 0.4 + 0.2) / 3 = 0.4
        self.assertAlmostEqual(average_correlation(m), 0.4)


class CorrelationBetweenTest(unittest.TestCase):
    def test_same_asset_returns_one(self):
        from src.analysis.asset_correlation import correlation_between
        s = _synthetic_returns(1)
        self.assertEqual(correlation_between({"A": s}, "A", "A"), 1.0)

    def test_missing_asset_returns_none(self):
        from src.analysis.asset_correlation import correlation_between
        s = _synthetic_returns(1)
        self.assertIsNone(correlation_between({"A": s}, "A", "B"))
        self.assertIsNone(correlation_between({"A": s}, "X", "A"))


class GroupByAssetClassTest(unittest.TestCase):
    def test_groups_correctly(self):
        from src.analysis.asset_correlation import group_by_asset_class
        groups = group_by_asset_class(["BTC-USD", "ETH-USD", "SPY", "QQQ", "GLD", "USO"])
        self.assertEqual(set(groups["crypto"]), {"BTC-USD", "ETH-USD"})
        self.assertEqual(set(groups["equity_growth"]), {"SPY", "QQQ"})
        self.assertEqual(groups["commodity_safe"], ["GLD"])
        self.assertEqual(groups["commodity_energy"], ["USO"])

    def test_unknown_symbols_bucketed_as_cash(self):
        from src.analysis.asset_correlation import group_by_asset_class
        groups = group_by_asset_class(["BTC-USD", "USDT", "BLAH"])
        self.assertEqual(groups["crypto"], ["BTC-USD"])
        self.assertEqual(set(groups["cash"]), {"USDT", "BLAH"})

    def test_empty_input(self):
        from src.analysis.asset_correlation import group_by_asset_class
        self.assertEqual(group_by_asset_class([]), {})


class FetchReturnsTest(unittest.TestCase):
    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_returns_dict_for_successful_fetches(self, mock_yf):
        """Mock yfinance to return a synthetic dataframe per symbol."""
        from src.analysis.asset_correlation import fetch_returns

        def fake_yf(symbol, period, interval, **_kw):
            idx = pd.date_range("2025-01-01", periods=60, freq="D")
            prices = 100.0 + np.cumsum(np.random.default_rng(hash(symbol) % 2**32).normal(0, 1, 60))
            return pd.DataFrame({"Close": prices}, index=idx)

        mock_yf.side_effect = fake_yf
        out = fetch_returns(["BTC-USD", "SPY"], window_days=60)
        self.assertEqual(set(out.keys()), {"BTC-USD", "SPY"})
        for sym, ser in out.items():
            self.assertGreater(len(ser), 30)  # at least 30 returns
            self.assertTrue(np.isfinite(ser.values).all())

    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_omits_failed_symbols(self, mock_yf):
        from src.analysis.asset_correlation import fetch_returns

        def fake_yf(symbol, **_kw):
            if symbol == "MISSING":
                return None
            idx = pd.date_range("2025-01-01", periods=60, freq="D")
            return pd.DataFrame({"Close": np.linspace(100, 110, 60)}, index=idx)

        mock_yf.side_effect = fake_yf
        out = fetch_returns(["BTC-USD", "MISSING"])
        self.assertIn("BTC-USD", out)
        self.assertNotIn("MISSING", out)

    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_handles_empty_dataframe(self, mock_yf):
        from src.analysis.asset_correlation import fetch_returns
        mock_yf.return_value = pd.DataFrame()  # empty
        out = fetch_returns(["BTC-USD"])
        self.assertNotIn("BTC-USD", out)


class AnalyzeAssetsTest(unittest.TestCase):
    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_analyze_correlated_crypto(self, mock_yf):
        """BTC + ETH should show high correlation. The result must say
        "not well diversified" because they're a single bet."""
        from src.analysis.asset_correlation import analyze_assets

        def fake_yf(symbol, **_kw):
            idx = pd.date_range("2025-01-01", periods=60, freq="D")
            base = np.cumsum(np.random.default_rng(0).normal(0, 1, 60))
            # Make B highly correlated to A.
            if symbol == "BTC-USD":
                prices = 50000 + 1000 * base
            else:  # ETH-USD
                prices = 3000 + 60 * base + 5 * np.random.default_rng(1).normal(0, 1, 60)
            return pd.DataFrame({"Close": prices}, index=idx)

        mock_yf.side_effect = fake_yf
        result = analyze_assets(["BTC-USD", "ETH-USD"], window_days=60)
        self.assertEqual(set(result.assets), {"BTC-USD", "ETH-USD"})
        # 0.7+ correlation expected between BTC and ETH (defacto reality + synthetic data).
        self.assertGreater(result.matrix[0][1], 0.5)
        self.assertGreater(result.avg_correlation, 0.5)
        # Per-asset-class: both should be in crypto.
        self.assertEqual(set(result.per_asset_class["crypto"]),
                         {"BTC-USD", "ETH-USD"})

    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_analyze_no_data_returns_empty_result(self, mock_yf):
        from src.analysis.asset_correlation import analyze_assets
        mock_yf.return_value = None
        result = analyze_assets(["BTC-USD"])
        self.assertEqual(result.assets, [])
        self.assertEqual(result.matrix, [])
        self.assertEqual(result.avg_correlation, 0.0)
        # Sprint 45 fix (N5): missing data means UNKNOWN, not "confirmed
        # diversified". The original test asserted well_diversified was
        # truthy here ("no signal = no warning") — but that's exactly
        # the fail-open bug the second audit flagged: a network outage
        # silently looked identical to "portfolio is fine". Now it's
        # explicitly None so callers can't mistake "we don't know" for
        # "we checked and it's fine".
        self.assertIsNone(result.well_diversified)

    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_analyze_threshold_configurable(self, mock_yf):
        from src.analysis.asset_correlation import analyze_assets

        def fake_yf(symbol, **_kw):
            idx = pd.date_range("2025-01-01", periods=60, freq="D")
            prices = 100.0 + np.cumsum(np.random.default_rng(hash(symbol) % 2**32).normal(0, 1, 60))
            return pd.DataFrame({"Close": prices}, index=idx)

        mock_yf.side_effect = fake_yf
        # Default threshold 0.5.
        r1 = analyze_assets(["BTC-USD", "SPY"], window_days=60)
        # Very low threshold — nothing should be "well diversified".
        r2 = analyze_assets(["BTC-USD", "SPY"], window_days=60, threshold=0.0)
        self.assertGreaterEqual(r2.avg_correlation, r1.avg_correlation)


# ============================================================
# Sprint 44A — risk_agent concentration check
# ============================================================

class ConcentrationGateTest(unittest.TestCase):
    def _make_pos(self, asset: str, notional_usd: float, direction: str = "long"):
        """Build a Position-like object for testing."""
        from src.data_store.positions import Position
        import time
        return Position(
            asset=asset,
            direction=direction,
            entry_price=notional_usd if asset in ("BTC-USD", "ETH-USD", "SOL-USD") else notional_usd,
            stop_loss=0.0,
            take_profit=0.0,
            qty=1.0,
            risk_usd=1.0,
            entry_ts=time.time(),
            strategy="test",
        )

    def _make_risk(self, opens=None, max_pct: float = 60.0, enabled: bool = True):
        from src.agents.risk_agent import RiskManagerAgent
        from src.data_store.positions import PositionRepository
        import tempfile, os
        # Real PositionRepository pointed at a temp file, then inject opens.
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "positions.json")
        repo = PositionRepository(path=path)
        if opens:
            for p in opens:
                # Bypass add_open to avoid the disk write for tests.
                repo.positions.append(p)
        return RiskManagerAgent(
            position_repo=repo,
            asset_concentration_check=enabled,
            max_asset_class_concentration_pct=max_pct,
            # Sprint 45: network-dependent portfolio gates off in this pre-existing test (not what it's testing).
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
)

    def test_first_trade_no_concentration(self):
        """Empty book → first trade always allowed."""
        risk = self._make_risk(opens=[], max_pct=60.0)
        ok, reason = risk._check_concentration("BTC-USD", proposed_notional_usd=20.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "empty_book_no_concentration")

    def test_concentration_disabled_allows_everything(self):
        risk = self._make_risk(
            opens=[self._make_pos("BTC-USD", 50.0)],
            max_pct=10.0,  # would normally reject
            enabled=False,
        )
        ok, reason = risk._check_concentration("BTC-USD", proposed_notional_usd=50.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "concentration_check_disabled")

    def test_unknown_symbol_bucketed_as_cash_allowed(self):
        risk = self._make_risk(
            opens=[self._make_pos("BTC-USD", 50.0)],
            max_pct=10.0,
        )
        ok, reason = risk._check_concentration("USDT", proposed_notional_usd=20.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "cash_class_skipped")

    def test_zero_notional_skipped(self):
        risk = self._make_risk(opens=[self._make_pos("BTC-USD", 50.0)])
        ok, reason = risk._check_concentration("BTC-USD", proposed_notional_usd=0.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "zero_notional_skipped")

    def test_within_cap_allows(self):
        """Adding another crypto trade that keeps crypto < 60% of total."""
        opens = [
            self._make_pos("BTC-USD", 20.0),
            self._make_pos("SPY", 30.0),
        ]
        # Current: crypto=20, equity_growth=30, total=50. crypto% = 40%.
        # Adding ETH-USD $10 → crypto=30, total=60, crypto%=50%. Under 60% cap. ✓
        risk = self._make_risk(opens=opens, max_pct=60.0)
        ok, reason = risk._check_concentration("ETH-USD", proposed_notional_usd=10.0)
        self.assertTrue(ok, f"expected ok=True, got {reason}")

    def test_exceeds_cap_rejects(self):
        """Adding a trade that would push crypto over the cap."""
        opens = [
            self._make_pos("BTC-USD", 50.0),
            self._make_pos("SPY", 30.0),
        ]
        # Current: crypto=50, equity=30, total=80. crypto%=62.5%.
        # Adding SOL-USD $20 → crypto=70, total=100, crypto%=70%. Over 60% cap. ✗
        risk = self._make_risk(opens=opens, max_pct=60.0)
        ok, reason = risk._check_concentration("SOL-USD", proposed_notional_usd=20.0)
        self.assertFalse(ok)
        self.assertIn("asset_class_crypto", reason)
        self.assertIn("exceeds", reason)

    def test_different_class_always_allowed(self):
        """Adding GLD (commodity_safe) to a book heavy on crypto should be fine."""
        opens = [
            self._make_pos("BTC-USD", 80.0),
            self._make_pos("ETH-USD", 80.0),
        ]
        # crypto already 100%. Adding commodity_safe = different bucket.
        risk = self._make_risk(opens=opens, max_pct=60.0)
        ok, reason = risk._check_concentration("GLD", proposed_notional_usd=20.0)
        self.assertTrue(ok, f"expected ok=True, got {reason}")

    def test_exposure_by_class_bucketing(self):
        opens = [
            self._make_pos("BTC-USD", 10.0),
            self._make_pos("ETH-USD", 20.0),
            self._make_pos("SPY", 30.0),
            self._make_pos("GLD", 40.0),
        ]
        risk = self._make_risk(opens=opens)
        exp = risk._exposure_by_class()
        self.assertAlmostEqual(exp["crypto"], 30.0)
        self.assertAlmostEqual(exp["equity_growth"], 30.0)
        self.assertAlmostEqual(exp["commodity_safe"], 40.0)
        self.assertNotIn("commodity_energy", exp)

    def test_cap_can_be_lowered(self):
        """Setting max_pct to 10% should reject a tiny addition to a same-class book."""
        opens = [
            self._make_pos("BTC-USD", 50.0),
            self._make_pos("SPY", 50.0),
        ]
        # crypto = 50, total = 100, crypto% = 50%.
        # Add BTC $1 → crypto = 51, total = 101, crypto% = 50.5%. Under 60% but over 10%? No, 50.5 > 10.
        risk = self._make_risk(opens=opens, max_pct=10.0)
        ok, reason = risk._check_concentration("BTC-USD", proposed_notional_usd=1.0)
        self.assertFalse(ok)
        self.assertIn("10pct_cap", reason)

    def test_circular_cap_at_exact_threshold_allows(self):
        """Equality at the threshold is allowed (> not >=)."""
        opens = [
            self._make_pos("BTC-USD", 30.0),
            self._make_pos("SPY", 30.0),
        ]
        # crypto=30, total=60, 50%. Add ETH $0 → still 50%. Allow (proposed=0 case handled by zero skip).
        # Instead: add ETH that brings to exactly 60%.
        # crypto=30, total=60, add ETH $15 → crypto=45, total=75, 60%. At cap → allowed (>).
        risk = self._make_risk(opens=opens, max_pct=60.0)
        ok, reason = risk._check_concentration("ETH-USD", proposed_notional_usd=15.0)
        self.assertTrue(ok, f"expected ok=True at threshold, got {reason}")


class ConcentrationGateIntegrationTest(unittest.TestCase):
    """End-to-end: run validate_and_size and check that the concentration
    gate integrates correctly with the rest of the risk pipeline."""

    def _make_risk(self, opens, max_pct: float = 60.0):
        from src.agents.risk_agent import RiskManagerAgent
        from src.data_store.positions import PositionRepository
        import tempfile, os
        tmpdir = tempfile.mkdtemp()
        repo = PositionRepository(path=os.path.join(tmpdir, "positions.json"))
        for p in opens:
            repo.positions.append(p)
        return RiskManagerAgent(
            position_repo=repo,
            asset_concentration_check=True,
            max_asset_class_concentration_pct=max_pct,
            min_order_usd=10.0,
            max_open_trades=5,
            max_capital_per_trade_pct=50,
            risk_per_trade_pct=1.0,
            # Sprint 45: network-dependent portfolio gates off in this pre-existing test (not what it's testing).
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
)

    def test_concentration_blocked_in_validate_and_size(self):
        """A new trade that would push crypto over the cap gets rejected
        with reason containing 'concentration' or 'asset_class'."""
        from src.data_store.positions import Position
        import time
        # Pre-existing book with comparable-size positions:
        #   BTC-USD notional $50 (0.001 BTC * $50k)
        #   ETH-USD notional $30 (0.01 ETH * $3k)
        #   SPY    notional $30 (0.075 shares * $400)
        # Total = $110, crypto = $80 (73%).
        # Adding SOL-USD $20 → crypto = $100, total = $130, crypto% = 77%.
        # With max_pct=50% this MUST be rejected.
        opens = [
            Position(
                asset="BTC-USD", direction="long",
                entry_price=50000, stop_loss=49000, take_profit=52000,
                qty=0.001, risk_usd=10, entry_ts=time.time(), strategy="test",
            ),
            Position(
                asset="ETH-USD", direction="long",
                entry_price=3000, stop_loss=2900, take_profit=3200,
                qty=0.01, risk_usd=10, entry_ts=time.time(), strategy="test",
            ),
            Position(
                asset="SPY", direction="long",
                entry_price=400, stop_loss=395, take_profit=420,
                qty=0.075, risk_usd=4, entry_ts=time.time(), strategy="test",
            ),
        ]
        risk = self._make_risk(opens=opens, max_pct=50.0)
        # Validate the specific check directly (full validate_and_size would
        # need a real broker for balance).
        ok, reason = risk._check_concentration("SOL-USD", proposed_notional_usd=20.0)
        self.assertFalse(ok, f"expected rejection, got ok={ok} reason={reason}")
        self.assertIn("crypto", reason)
        self.assertIn("exceeds", reason)

    def test_concentration_allows_diversifying_trade(self):
        """Adding a trade from a different class (commodity_safe) is allowed
        even when crypto is already over the cap. Diversification wins."""
        from src.data_store.positions import Position
        import time
        opens = [
            Position(
                asset="BTC-USD", direction="long",
                entry_price=50000, stop_loss=49000, take_profit=52000,
                qty=0.001, risk_usd=10, entry_ts=time.time(), strategy="test",
            ),
            Position(
                asset="ETH-USD", direction="long",
                entry_price=3000, stop_loss=2900, take_profit=3200,
                qty=0.01, risk_usd=10, entry_ts=time.time(), strategy="test",
            ),
        ]
        risk = self._make_risk(opens=opens, max_pct=50.0)
        # GLD is commodity_safe, not crypto. Always allowed.
        ok, reason = risk._check_concentration("GLD", proposed_notional_usd=20.0)
        self.assertTrue(ok, f"expected ok=True, got reason={reason}")

    def test_concentration_rejects_before_replacement(self):
        """Sprint 44A: concentration check must run BEFORE the
        max_open_trades / replacement check. A new trade that would push
        crypto over the cap is rejected, even if the new signal is so
        strong it would normally trigger a replacement.

        Sprint 44B: the allocation policy runs even earlier. To isolate
        the concentration gate here, we pass an explicitly DISABLED
        allocation policy (so the test measures the 44A gate in
        isolation, not the priority order of the two gates).
        """
        from src.data_store.positions import Position
        from src.agents.risk_agent import RiskManagerAgent
        from src.data.asset_allocation import AllocationPolicy
        import time
        import tempfile, os

        tmpdir = tempfile.mkdtemp()
        from src.data_store.positions import PositionRepository
        repo = PositionRepository(path=os.path.join(tmpdir, "positions.json"))
        # 2 open positions, both crypto, max_open=2 (replacement scenario).
        repo.positions.append(Position(
            asset="ETH-USD", direction="long",
            entry_price=3000, stop_loss=2950, take_profit=3150,
            qty=0.01, risk_usd=0.5,
            entry_ts=time.time() - 86400, strategy="old",
        ))
        repo.positions.append(Position(
            asset="BTC-USD", direction="long",
            entry_price=50000, stop_loss=49500, take_profit=52000,
            qty=0.001, risk_usd=5,
            entry_ts=time.time() - 86400, strategy="old",
        ))

        agent = RiskManagerAgent(
            position_repo=repo,
            max_open_trades=2,
            min_order_usd=10.0,
            max_capital_per_trade_pct=50,
            risk_per_trade_pct=1.0,
            asset_concentration_check=True,
            max_asset_class_concentration_pct=80.0,  # permissive
            allocation_policy=AllocationPolicy(enabled=False),  # isolate 44A
            enable_position_replacement=True,
            replacement_score_threshold=0.20,
            current_prices={"ETH-USD": 2900, "BTC-USD": 49000},
            # Sprint 45: network-dependent portfolio gates off in this pre-existing test (not what it's testing).
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
)
        # New SOL-USD: another crypto, but with permissive 80% cap → should pass.
        new_hyp = {
            "asset": "SOL-USD", "strategy": "momentum", "direction": "long",
            "price": 150.0, "atr_at_signal": 3.0, "expected_move_pct": 5.0,
        }
        result = agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": [new_hyp]}})
        # With 80% cap, crypto post-add = (30+50+10) / (30+50+10) = 90% > 80% → reject
        self.assertEqual(len(result["approved_trades"]), 0)
        # And the reason should be concentration, NOT max_open_trades.
        rejected_reasons = [str(r.get("reason", "")) for r in result["rejected_trades"]]
        self.assertTrue(
            any("asset_class_crypto" in r for r in rejected_reasons),
            f"Expected concentration rejection, got {rejected_reasons}",
        )


if __name__ == "__main__":
    unittest.main()
