"""
Sprint 45 (N4 + N5) — Portfolio-risk gates.

Wires the Sprint 44 analytics modules (previously computed but never
consulted by the trading pipeline, per the second audit's N4 finding)
into real pre-trade gates on RiskManagerAgent:

  - src/analysis/stress_test.py     -> _check_portfolio_stress
  - src/analysis/asset_correlation.py -> _check_portfolio_correlation
  - src/analysis/tail_risk.py       -> _check_portfolio_tail_risk

Also covers N5: asset_correlation.AssetCorrelationResult.well_diversified
becoming Optional[bool], with None meaning "no data / unknown" instead
of the old fail-open default of True.

Design principle under test throughout: missing data or an internal
exception must ALWAYS allow the trade (never halt trading because a
data provider is flaky); only a *confirmed* breach rejects it.

Run: python -m unittest tests.test_sprint_45_portfolio_gates -v
"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _make_pos(asset: str, notional_usd: float, direction: str = "long"):
    """Build a Position-like object for testing. `notional_usd` is a
    computed property (entry_price * qty), so we set entry_price to
    the desired notional and qty=1.0 to get exactly that notional."""
    from src.data_store.positions import Position
    return Position(
        asset=asset,
        direction=direction,
        entry_price=notional_usd,
        stop_loss=0.0,
        take_profit=0.0,
        qty=1.0,
        risk_usd=1.0,
        entry_ts=time.time(),
        strategy="test",
    )


def _make_risk(opens=None, **kwargs):
    from src.agents.risk_agent import RiskManagerAgent
    from src.data_store.positions import PositionRepository
    tmpdir = tempfile.mkdtemp()
    repo = PositionRepository(path=os.path.join(tmpdir, "positions.json"))
    for p in (opens or []):
        repo.positions.append(p)
    return RiskManagerAgent(position_repo=repo, **kwargs)


# ============================================================
# N5: asset_correlation.py well_diversified now Optional[bool]
# ============================================================

class N5WellDiversifiedOptionalTest(unittest.TestCase):
    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_no_data_returns_none_not_true(self, mock_yf):
        """Sprint 45 fix: no data must be reported as UNKNOWN (None),
        not as a false-positive 'confirmed diversified' (True)."""
        from src.analysis.asset_correlation import analyze_assets
        mock_yf.return_value = None
        result = analyze_assets(["BTC-USD", "ETH-USD"])
        self.assertIsNone(result.well_diversified)


# ============================================================
# N4: _check_portfolio_stress (network-free, static shock tables)
# ============================================================

class PortfolioStressGateTest(unittest.TestCase):
    def test_disabled_allows(self):
        risk = _make_risk(opens=[_make_pos("BTC-USD", 50.0)], portfolio_stress_check=False)
        ok, reason = risk._check_portfolio_stress("ETH-USD", 50.0)
        self.assertTrue(ok)
        self.assertIn("disabled", reason)

    def test_zero_notional_allows(self):
        risk = _make_risk(opens=[_make_pos("BTC-USD", 50.0)])
        ok, reason = risk._check_portfolio_stress("ETH-USD", 0.0)
        self.assertTrue(ok)

    def test_mild_book_passes_default_cap(self):
        """A small, mixed book shouldn't breach the default 50% cap."""
        risk = _make_risk(
            opens=[_make_pos("SPY", 20.0), _make_pos("GLD", 20.0)],
            max_stress_drawdown_pct=50.0,
        )
        ok, reason = risk._check_portfolio_stress("SPY", 10.0)
        self.assertTrue(ok, reason)

    def test_all_crypto_book_breaches_tight_cap(self):
        """100% crypto book under 2022_RATE_HIKES (-64%) breaches a tight
        (e.g. 30%) cap -- this is a real, deterministic computation since
        stress_test.py needs no network."""
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0), _make_pos("ETH-USD", 50.0)],
            max_stress_drawdown_pct=30.0,
        )
        ok, reason = risk._check_portfolio_stress("SOL-USD", 50.0)
        self.assertFalse(ok)
        self.assertIn("exceeds", reason)

    def test_error_in_stress_calc_allows(self):
        risk = _make_risk(opens=[_make_pos("BTC-USD", 50.0)])
        with patch(
            "src.agents.risk_agent.stress_portfolio_all_scenarios",
            side_effect=RuntimeError("boom"),
        ):
            ok, reason = risk._check_portfolio_stress("ETH-USD", 10.0)
        self.assertTrue(ok)
        self.assertIn("stress_check_error", reason)


# ============================================================
# N4: _check_portfolio_correlation (network-dependent via analyze_assets)
# ============================================================

class PortfolioCorrelationGateTest(unittest.TestCase):
    def test_disabled_allows(self):
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0), _make_pos("ETH-USD", 50.0)],
            correlation_check_enabled=False,
        )
        ok, reason = risk._check_portfolio_correlation("SOL-USD", 10.0)
        self.assertTrue(ok)
        self.assertIn("disabled", reason)

    def test_too_few_assets_allows(self):
        """Fewer than 3 total projected assets isn't enough signal."""
        risk = _make_risk(opens=[_make_pos("BTC-USD", 50.0)])
        ok, reason = risk._check_portfolio_correlation("ETH-USD", 10.0)
        self.assertTrue(ok)
        self.assertIn("too_few_assets", reason)

    def test_confirmed_bad_correlation_rejects(self):
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0), _make_pos("ETH-USD", 50.0)],
            max_avg_correlation_pct=50.0,
        )
        fake_result = MagicMock(well_diversified=False, avg_correlation=0.90)
        with patch("src.agents.risk_agent.analyze_assets", return_value=fake_result):
            ok, reason = risk._check_portfolio_correlation("SOL-USD", 10.0)
        self.assertFalse(ok)
        self.assertIn("correlation_avg_90", reason.replace(".", ""))

    def test_well_diversified_true_allows(self):
        risk = _make_risk(opens=[_make_pos("BTC-USD", 50.0), _make_pos("SPY", 50.0)])
        fake_result = MagicMock(well_diversified=True, avg_correlation=0.10)
        with patch("src.agents.risk_agent.analyze_assets", return_value=fake_result):
            ok, reason = risk._check_portfolio_correlation("GLD", 10.0)
        self.assertTrue(ok)

    def test_unknown_none_allows_not_rejects(self):
        """N5's fix: well_diversified=None (no data) must ALLOW, never
        be treated as a confirmed bad signal."""
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0), _make_pos("ETH-USD", 50.0)],
            max_avg_correlation_pct=1.0,  # would reject almost anything if it were checked
        )
        fake_result = MagicMock(well_diversified=None, avg_correlation=0.0)
        with patch("src.agents.risk_agent.analyze_assets", return_value=fake_result):
            ok, reason = risk._check_portfolio_correlation("SOL-USD", 10.0)
        self.assertTrue(ok)
        self.assertIn("no_data", reason)

    def test_error_allows(self):
        risk = _make_risk(opens=[_make_pos("BTC-USD", 50.0), _make_pos("ETH-USD", 50.0)])
        with patch("src.agents.risk_agent.analyze_assets", side_effect=RuntimeError("network down")):
            ok, reason = risk._check_portfolio_correlation("SOL-USD", 10.0)
        self.assertTrue(ok)
        self.assertIn("correlation_check_error", reason)


# ============================================================
# N4: _check_portfolio_tail_risk (network-dependent via compute_portfolio_tail_risk)
# ============================================================

class PortfolioTailRiskGateTest(unittest.TestCase):
    def test_disabled_allows(self):
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0)],
            tail_risk_check_enabled=False,
        )
        ok, reason = risk._check_portfolio_tail_risk("ETH-USD", 10.0)
        self.assertTrue(ok)
        self.assertIn("disabled", reason)

    def test_confirmed_cvar_breach_rejects(self):
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0)],
            max_cvar_95_pct=10.0,
        )
        fake_result = MagicMock(n_observations=120, cvar_95=-0.25)
        with patch("src.agents.risk_agent.compute_portfolio_tail_risk", return_value=fake_result):
            ok, reason = risk._check_portfolio_tail_risk("ETH-USD", 10.0)
        self.assertFalse(ok)
        self.assertIn("cvar95_25", reason.replace(".", ""))

    def test_within_cap_allows(self):
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0)],
            max_cvar_95_pct=30.0,
        )
        fake_result = MagicMock(n_observations=120, cvar_95=-0.05)
        with patch("src.agents.risk_agent.compute_portfolio_tail_risk", return_value=fake_result):
            ok, reason = risk._check_portfolio_tail_risk("ETH-USD", 10.0)
        self.assertTrue(ok)

    def test_no_data_allows(self):
        risk = _make_risk(opens=[_make_pos("BTC-USD", 50.0)])
        fake_result = MagicMock(n_observations=0, cvar_95=0.0)
        with patch("src.agents.risk_agent.compute_portfolio_tail_risk", return_value=fake_result):
            ok, reason = risk._check_portfolio_tail_risk("ETH-USD", 10.0)
        self.assertTrue(ok)
        self.assertIn("no_data", reason)

    def test_error_allows(self):
        risk = _make_risk(opens=[_make_pos("BTC-USD", 50.0)])
        with patch(
            "src.agents.risk_agent.compute_portfolio_tail_risk",
            side_effect=RuntimeError("boom"),
        ):
            ok, reason = risk._check_portfolio_tail_risk("ETH-USD", 10.0)
        self.assertTrue(ok)
        self.assertIn("tail_risk_check_error", reason)


# ============================================================
# Integration: gates fire from validate_and_size() in the right order
# ============================================================

class PortfolioGatesIntegrationTest(unittest.TestCase):
    def _hyp(self, asset="SOL-USD"):
        return {
            "asset": asset, "strategy": "momentum", "direction": "long",
            "price": 150.0, "atr_at_signal": 3.0, "expected_move_pct": 5.0,
        }

    def test_stress_block_surfaces_in_rejected_trades(self):
        from src.data.asset_allocation import AllocationPolicy
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0), _make_pos("ETH-USD", 50.0)],
            max_stress_drawdown_pct=1.0,  # essentially guaranteed to breach
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            asset_concentration_check=False,
            allocation_policy=AllocationPolicy(enabled=False),
            current_prices={"BTC-USD": 50000, "ETH-USD": 3000},
        )
        result = risk.validate_and_size({}, {"generate_hypotheses": {"hypotheses": [self._hyp()]}})
        self.assertEqual(len(result["approved_trades"]), 0)
        reasons = [str(r.get("reason", "")) for r in result["rejected_trades"]]
        self.assertTrue(any("stress_test" in r for r in reasons), reasons)

    def test_all_gates_disabled_does_not_block(self):
        from src.data.asset_allocation import AllocationPolicy
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0), _make_pos("ETH-USD", 50.0)],
            portfolio_stress_check=False,
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            asset_concentration_check=False,
            allocation_policy=AllocationPolicy(enabled=False),
            current_prices={"BTC-USD": 50000, "ETH-USD": 3000},
        )
        result = risk.validate_and_size({}, {"generate_hypotheses": {"hypotheses": [self._hyp()]}})
        self.assertEqual(len(result["approved_trades"]), 1)


if __name__ == "__main__":
    unittest.main()
