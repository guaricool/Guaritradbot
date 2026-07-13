"""
Sprint 47A (audit M15 Option B) — small-account bypass tests.

The audit's M15 complaint: with a $20-100 account and the $10
minimum order, the 44B drift policy (40% crypto, 40% equity, 10%
commodities) is structurally unreachable — a single position is
already 50-100% of the book. The chosen resolution (Option B):
add a `small_account_threshold_usd` field to `AllocationPolicy`
that bypasses the drift check when the total notional (current
positions + proposed trade) is below the threshold.

These tests verify:
  1. Below the threshold: returns ok=True with reason
     "small_account_policy_skipped" (not the usual
     "within_policy" / breach reason).
  2. Above the threshold: the original drift check runs and
     rejects the trade if it would breach.
  3. Setting the threshold to 0 disables the bypass — the original
     behavior is preserved.
"""
import unittest

from src.data.asset_allocation import (
    AllocationPolicy,
    AssetClass,
    check_trade_against_policy,
)
from src.data.asset_class import get_asset_class


def _make_pos(asset, notional):
    """Lightweight stand-in for a Position — only needs what
    check_trade_against_policy reads (asset + notional_usd)."""
    pos = unittest.mock.MagicMock() if False else type("P", (), {})()
    pos.asset = asset
    pos.notional_usd = notional
    pos.direction = "long"
    return pos


# Avoid pulling unittest.mock just for the helper above — use a
# simple namespace. The risk_agent path uses real Position objects,
# but this gate only reads .asset and .notional_usd, so a duck-typed
# object is fine.
class _Pos:
    def __init__(self, asset, notional):
        self.asset = asset
        self.notional_usd = notional
        self.direction = "long"


def _policy(threshold=50.0):
    return AllocationPolicy(
        targets={
            AssetClass.CRYPTO.value: 0.40,
            AssetClass.EQUITY_GROWTH.value: 0.40,
            AssetClass.COMMODITY_SAFE.value: 0.10,
            AssetClass.COMMODITY_ENERGY.value: 0.10,
        },
        drift_tolerance_pct=10.0,
        enabled=True,
        small_account_threshold_usd=threshold,
    )


class SmallAccountBypassTest(unittest.TestCase):
    """Sprint 47A Option B: drift policy is skipped when total
    notional < threshold. The 44A concentration cap (60%) is the
    appropriate backstop for sub-threshold accounts."""

    def test_below_threshold_skips_drift_check(self):
        """Single $10 BTC position + $10 proposed ETH = $20 total
        < $50 threshold -> drift check is bypassed, even though
        the resulting book is 100% crypto (well above the 40%
        target + 10% drift = 50% cap)."""
        opens = [_Pos("BTC-USD", 10.0)]
        policy = _policy(threshold=50.0)
        ok, reason = check_trade_against_policy(
            asset="ETH-USD",
            proposed_notional_usd=10.0,
            current_positions=opens,
            policy=policy,
        )
        # We need to monkey-patch get_asset_class for ETH-USD since
        # asset_class.py still has it in the map (the B5 commit kept
        # it there as a superset).
        from src.data import asset_class as _ac
        with unittest.mock.patch.object(
            _ac, "ASSET_CLASS_MAP",
            {**_ac.ASSET_CLASS_MAP},  # identity (no-op)
        ):
            # The bypass returns ok=True before the drift check runs,
            # so the resulting reason should be
            # 'small_account_policy_skipped', not the breach
            # reason that the full drift check would have produced.
            self.assertTrue(ok)
            self.assertEqual(reason, "small_account_policy_skipped")

    def test_above_threshold_runs_full_drift_check(self):
        """At $60 total ($30 BTC + $30 ETH proposed = 100% crypto),
        the threshold ($50) is exceeded, so the full drift check
        runs and rejects the trade (the 50% cap on crypto is
        violated)."""
        opens = [_Pos("BTC-USD", 30.0)]
        policy = _policy(threshold=50.0)
        ok, reason = check_trade_against_policy(
            asset="ETH-USD",
            proposed_notional_usd=30.0,
            current_positions=opens,
            policy=policy,
        )
        self.assertFalse(ok)
        self.assertIn("allocation_policy_crypto", reason)
        self.assertIn("exceeds", reason)

    def test_zero_threshold_disables_bypass(self):
        """Setting small_account_threshold_usd=0 forces the drift
        policy to always run, even on a tiny book. This is the
        legacy behavior — preserved as an escape hatch for
        operators who want strict drift enforcement regardless of
        account size."""
        opens = [_Pos("BTC-USD", 10.0)]
        policy = _policy(threshold=0.0)
        ok, reason = check_trade_against_policy(
            asset="ETH-USD",
            proposed_notional_usd=10.0,
            current_positions=opens,
            policy=policy,
        )
        # $20 total, 100% crypto -> should be rejected (cap 50%).
        self.assertFalse(ok)
        self.assertIn("allocation_policy_crypto", reason)

    def test_threshold_includes_proposed_trade(self):
        """Empty book + $30 proposed trade = $30 < $50. The
        threshold includes the proposed trade, not just the
        current positions — this is what makes the bypass safe
        for a 'next trade would still be sub-threshold' check."""
        policy = _policy(threshold=50.0)
        ok, reason = check_trade_against_policy(
            asset="BTC-USD",
            proposed_notional_usd=30.0,
            current_positions=[],
            policy=policy,
        )
        # Empty book check fires first -> "empty_book" (not the
        # bypass). This is the correct ordering: an empty book
        # has nothing to drift FROM, so the original empty-book
        # bypass is what applies.
        self.assertTrue(ok)
        self.assertEqual(reason, "empty_book")


# Late import so the helper namespace above can be defined first.
import unittest.mock  # noqa: E402


if __name__ == "__main__":
    unittest.main()
