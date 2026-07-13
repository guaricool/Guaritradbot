"""
Sprint 52.1 — Stress test small-account bypass tests.

Mirrors the Sprint 47A pattern (`check_trade_against_policy`):
when total_notional (current opens + proposed) is below
`AllocationPolicy.small_account_threshold_usd`, the portfolio
stress test is bypassed.

Why this exists: with the live $18 USDT account and the
$10 min order, the worst-case scenario (BTC-USD alone = -64%
in 2022 rate hikes) is structurally guaranteed to exceed the
30% cap in config.yaml. The bypass means a single
concentration cap (60%) is the only multi-position gate on a
small account, which is the appropriate backstop when one
position is already 50-100% of the book.

The bypass:
  - Reads the same threshold the AllocationPolicy exposes
    (`small_account_threshold_usd`, default 50.0).
  - Only fires when `portfolio_stress_check=True` (the
    existing feature flag) and `proposed_notional_usd > 0`.
  - On threshold check exception, falls through to the real
    stress test (defensive — never silently allow).
  - Returns ok=True with reason "small_account_stress_skipped"
    so the audit log clearly shows WHY the stress test
    didn't run.
"""
import unittest

from src.agents.risk_agent import RiskManagerAgent
from src.data.asset_allocation import (
    AllocationPolicy,
    AssetClass,
)


class _Pos:
    """Duck-typed stand-in: only needs what _check_portfolio_stress reads."""
    def __init__(self, asset, notional):
        self.asset = asset
        self.notional_usd = notional
        self.direction = "long"


def _agent_with_policy(policy):
    """Build a RiskManagerAgent with a specific AllocationPolicy and
    an in-memory position_repo stub."""
    agent = RiskManagerAgent(
        allocation_policy=policy,
        portfolio_stress_check=True,
        max_stress_drawdown_pct=30.0,  # tight cap that 2022 would breach
    )
    # Stub position_repo with .open() returning a list we control.
    agent.position_repo = unittest.mock.MagicMock() if False else type("R", (), {})()
    return agent


class _RepoStub:
    def __init__(self, opens):
        self._opens = opens
    def open(self):
        return list(self._opens)


class SmallAccountStressBypassTest(unittest.TestCase):
    def _agent(self, opens, threshold=50.0):
        policy = AllocationPolicy(
            targets={AssetClass.CRYPTO: 1.0},
            small_account_threshold_usd=threshold,
        )
        agent = RiskManagerAgent(
            allocation_policy=policy,
            portfolio_stress_check=True,
            max_stress_drawdown_pct=30.0,
        )
        agent.position_repo = _RepoStub(opens)
        return agent

    def test_below_threshold_skips_stress(self):
        """Account < $50 (Carlos's $18 live case) -> stress skipped."""
        agent = self._agent(opens=[_Pos("BTC-USD", 8.0)], threshold=50.0)
        # Proposed: $10 trade -> total $18, well under $50 threshold.
        ok, reason = agent._check_portfolio_stress(
            asset="BTC-USD",
            proposed_notional_usd=10.0,
        )
        self.assertTrue(ok, f"Expected bypass, got reject: {reason}")
        self.assertEqual(reason, "small_account_stress_skipped")

    def test_exactly_at_threshold_runs_stress(self):
        """Account == $50 (boundary) -> stress runs (uses < not <=)."""
        # Use a 2022-style scenario where 30% cap would block.
        # Easier: ensure the bypass doesn't fire at exactly $50.
        agent = self._agent(opens=[_Pos("BTC-USD", 40.0)], threshold=50.0)
        # Proposed $10 -> total $50 exactly, NOT < 50, so bypass should NOT fire.
        ok, reason = agent._check_portfolio_stress(
            asset="BTC-USD",
            proposed_notional_usd=10.0,
        )
        # Either ok=True (stress passed) or ok=False (stress blocked),
        # but reason MUST NOT be "small_account_stress_skipped"
        self.assertNotEqual(reason, "small_account_stress_skipped")

    def test_above_threshold_runs_stress(self):
        """Account > $50 -> stress runs (the bypass doesn't fire)."""
        agent = self._agent(opens=[_Pos("BTC-USD", 60.0)], threshold=50.0)
        # total = 60 + 10 = 70, well above 50, bypass should not fire.
        ok, reason = agent._check_portfolio_stress(
            asset="BTC-USD",
            proposed_notional_usd=10.0,
        )
        # 2022 BTC alone projected > 30% drawdown; expect a stress block.
        # The exact reason string is deterministic but the prefix is what matters.
        if not ok:
            self.assertTrue(
                reason.startswith("stress_test_") and "drawdown_exceeds" in reason,
                f"Expected stress_test block reason, got: {reason}",
            )
        # Either way, NOT the bypass.
        self.assertNotEqual(reason, "small_account_stress_skipped")

    def test_zero_threshold_disables_bypass(self):
        """Setting threshold=0 -> bypass never fires."""
        agent = self._agent(opens=[_Pos("BTC-USD", 8.0)], threshold=0.0)
        ok, reason = agent._check_portfolio_stress(
            asset="BTC-USD",
            proposed_notional_usd=10.0,
        )
        # total = 18, would normally be < 50, but threshold=0 disables.
        self.assertNotEqual(reason, "small_account_stress_skipped")

    def test_empty_book_with_small_trade_skips(self):
        """No open positions + $10 trade = $10 total < $50 -> skip."""
        agent = self._agent(opens=[], threshold=50.0)
        ok, reason = agent._check_portfolio_stress(
            asset="BTC-USD",
            proposed_notional_usd=10.0,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "small_account_stress_skipped")

    def test_proposed_zero_still_returns_zero_reason(self):
        """proposed_notional_usd=0 -> original behavior (not the bypass)."""
        agent = self._agent(opens=[_Pos("BTC-USD", 8.0)], threshold=50.0)
        ok, reason = agent._check_portfolio_stress(
            asset="BTC-USD",
            proposed_notional_usd=0.0,
        )
        # The first early-return in the method catches this.
        self.assertEqual(reason, "stress_check_disabled_or_zero_notional")

    def test_stress_check_disabled_returns_disabled_reason(self):
        """portfolio_stress_check=False -> original 'disabled' reason."""
        policy = AllocationPolicy(
            targets={AssetClass.CRYPTO: 1.0},
            small_account_threshold_usd=50.0,
        )
        agent = RiskManagerAgent(
            allocation_policy=policy,
            portfolio_stress_check=False,
            max_stress_drawdown_pct=30.0,
        )
        agent.position_repo = _RepoStub([_Pos("BTC-USD", 8.0)])
        ok, reason = agent._check_portfolio_stress(
            asset="BTC-USD",
            proposed_notional_usd=10.0,
        )
        self.assertEqual(reason, "stress_check_disabled_or_zero_notional")


class ThresholdCheckResilienceTest(unittest.TestCase):
    """The bypass is a defensive add-on. If anything in the
    threshold math throws, the method falls through to the real
    stress test (never silently allow)."""

    def test_repo_open_raises_falls_through_to_real_check(self):
        policy = AllocationPolicy(
            targets={AssetClass.CRYPTO: 1.0},
            small_account_threshold_usd=50.0,
        )
        agent = RiskManagerAgent(
            allocation_policy=policy,
            portfolio_stress_check=True,
            max_stress_drawdown_pct=30.0,
        )
        # Repo whose .open() throws
        class _BrokenRepo:
            def open(self):
                raise RuntimeError("simulated repo failure")
        agent.position_repo = _BrokenRepo()
        # Should NOT silently allow; should run real stress test.
        ok, reason = agent._check_portfolio_stress(
            asset="BTC-USD",
            proposed_notional_usd=10.0,
        )
        self.assertNotEqual(reason, "small_account_stress_skipped")
        # Real stress test will block (2022 64% > 30%) or pass — either
        # way, the bypass didn't fire.


if __name__ == "__main__":
    unittest.main()
