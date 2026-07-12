"""
Sprint 46R (audit B4): regression tests for
EquityTracker.reconcile_external_balance.

The audit's exact wording:
  "EquityTracker no re-sincroniza depositos/retiros; su
   drawdown/delta deriva de la realidad con el tiempo."

Pre-46R: the tracker's `starting_balance` was set once at
construction (or load from disk) and never adjusted. If Carlos
deposited $20 from his bank to the broker, the tracker's
"expected" balance stayed at the old number. The next snapshot
showed a $20 "delta" that was really a deposit, not a P&L
gain. After a few deposits the drawdown calculation drifted
too (peak equity never reflected the deposits).

The fix: a `reconcile_external_balance(broker_balance,
current_open_position_notional)` method. The caller passes
the broker's current free balance; the tracker computes
expected_free = starting + realized - open_notional and
treats any discrepancy as a deposit (positive) or
withdrawal (negative), adjusting starting_balance
accordingly and emitting an audit event.

Tests cover:
  - happy path deposit: detected, starting_balance bumped,
    audit event emitted
  - happy path withdrawal: detected, starting_balance dropped,
    audit event emitted
  - within tolerance (rounding): no event
  - negative broker_balance: skipped silently (bad data)
  - open position notional is subtracted from expected
  - peak equity is bumped on deposit if new high
  - peak equity is dropped on withdrawal if the new peak
    would be over the remaining capital
  - audit emit failure is graceful (no crash)
"""
from __future__ import annotations

import unittest
from collections import deque
from unittest.mock import MagicMock

from src.safety.equity_tracker import EquityTracker


def _make_tracker(starting_balance: float = 100.0, audit=None):
    """Build a minimal EquityTracker for testing reconcile.
    The tracker needs to have at least one snapshot so its
    `history` is non-empty (the reconcile method reads the
    latest snapshot's realized_pnl).

    Note: the constructor emits one EQUITY_UPDATE audit event
    for the initial baseline snapshot. Tests that care about
    audit.append call counts should reset the mock AFTER
    construction so the baseline call doesn't pollute the
    reconciliation assertions.
    """
    t = EquityTracker(
        starting_balance=starting_balance,
        position_repo=None,
        history_size=10,
        audit=audit,
    )
    return t


class ReconcileExternalBalanceTest(unittest.TestCase):
    def test_deposit_detected(self):
        """Broker balance higher than expected = Carlos deposited."""
        audit = MagicMock()
        t = _make_tracker(starting_balance=100.0, audit=audit)
        # Constructor emitted 1 EQUITY_UPDATE; reset so we
        # only count the reconcile-triggered call.
        audit.reset_mock()

        # Simulate a $20 deposit: broker says 120, expected 100.
        result = t.reconcile_external_balance(broker_balance=120.0)

        self.assertAlmostEqual(result["deposit_usd"], 20.0, places=4)
        self.assertAlmostEqual(result["withdrawal_usd"], 0.0, places=4)
        self.assertAlmostEqual(result["new_starting_balance"], 120.0, places=4)
        # starting_balance mutated
        self.assertAlmostEqual(t.starting_balance, 120.0, places=4)
        # Audit emitted (exactly once, the EQUITY_DEPOSIT)
        audit.append.assert_called_once()
        evt_name = audit.append.call_args.args[0]
        self.assertEqual(evt_name, "EQUITY_DEPOSIT")

    def test_withdrawal_detected(self):
        """Broker balance lower than expected = Carlos withdrew."""
        audit = MagicMock()
        t = _make_tracker(starting_balance=100.0, audit=audit)
        audit.reset_mock()

        # Simulate a $30 withdrawal: broker says 70, expected 100.
        result = t.reconcile_external_balance(broker_balance=70.0)

        self.assertAlmostEqual(result["deposit_usd"], 0.0, places=4)
        self.assertAlmostEqual(result["withdrawal_usd"], 30.0, places=4)
        self.assertAlmostEqual(result["new_starting_balance"], 70.0, places=4)
        self.assertAlmostEqual(t.starting_balance, 70.0, places=4)
        audit.append.assert_called_once()
        evt_name = audit.append.call_args.args[0]
        self.assertEqual(evt_name, "EQUITY_WITHDRAWAL")

    def test_within_tolerance_no_event(self):
        """A sub-penny difference (e.g. rounding noise) must not
        trigger a phantom deposit/withdrawal event."""
        audit = MagicMock()
        t = _make_tracker(starting_balance=100.0, audit=audit)
        audit.reset_mock()

        # $0.005 difference - within the $0.01 tolerance.
        result = t.reconcile_external_balance(broker_balance=100.005)

        self.assertAlmostEqual(result["deposit_usd"], 0.0, places=4)
        self.assertEqual(result["new_starting_balance"], 100.0)
        # No audit event
        audit.append.assert_not_called()

    def test_negative_broker_balance_skipped(self):
        """If the broker reports a negative free balance, skip
        silently (broker API error, not a real negative deposit)."""
        audit = MagicMock()
        t = _make_tracker(starting_balance=100.0, audit=audit)
        audit.reset_mock()

        result = t.reconcile_external_balance(broker_balance=-50.0)

        self.assertEqual(result["deposit_usd"], 0.0)
        self.assertEqual(result["withdrawal_usd"], 0.0)
        self.assertEqual(result["new_starting_balance"], 100.0)
        self.assertFalse(result["audit_emitted"])
        # starting_balance NOT mutated
        self.assertEqual(t.starting_balance, 100.0)
        audit.append.assert_not_called()

    def test_open_position_notional_subtracted_from_expected(self):
        """A position that's open means the cash is locked
        in the position, not in the free balance. The
        reconcile method must subtract it from expected_free
        before computing the deposit/withdrawal delta."""
        audit = MagicMock()
        t = _make_tracker(starting_balance=100.0, audit=audit)
        audit.reset_mock()

        # starting_balance=100, no realized P&L yet, $40 in an
        # open position. Expected free balance = 100 + 0 - 40 = 60.
        # If broker says 60, no deposit/withdrawal.
        result = t.reconcile_external_balance(
            broker_balance=60.0,
            current_open_position_notional=40.0,
        )
        self.assertEqual(result["deposit_usd"], 0.0)
        self.assertEqual(result["withdrawal_usd"], 0.0)
        audit.append.assert_not_called()

    def test_realized_pnl_in_expected_free(self):
        """If there's a closed trade's realized P&L in the
        most recent snapshot, it's added to expected_free."""
        audit = MagicMock()
        t = _make_tracker(starting_balance=100.0, audit=audit)
        audit.reset_mock()
        # Simulate a closed trade with +$10 realized P&L by
        # patching the latest snapshot.
        latest = t.history[-1]
        # EquitySnapshot is a dataclass; use object.__setattr__
        # to mutate the frozen-equivalent field.
        object.__setattr__(latest, "realized_pnl", 10.0)

        # Expected free = 100 + 10 = 110. If broker says 110,
        # no deposit/withdrawal.
        result = t.reconcile_external_balance(broker_balance=110.0)
        self.assertEqual(result["deposit_usd"], 0.0)
        self.assertEqual(result["withdrawal_usd"], 0.0)
        audit.append.assert_not_called()

    def test_deposit_bumps_peak_equity(self):
        """A deposit that's larger than the previous peak
        should become the new peak (peak equity = max of
        starting_balance after deposit vs prior peak)."""
        audit = MagicMock()
        t = _make_tracker(starting_balance=100.0, audit=audit)
        # Set prior peak via a snapshot
        latest = t.history[-1]
        object.__setattr__(latest, "total_equity", 105.0)
        t._max_equity = 105.0

        # $50 deposit: starting goes 100 -> 150, new peak = 150
        t.reconcile_external_balance(broker_balance=150.0)
        self.assertAlmostEqual(t._max_equity, 150.0, places=4)

    def test_withdrawal_drops_peak_equity(self):
        """A withdrawal should drop the peak equity by the
        withdrawal amount (the peak was based on the pre-
        withdrawal starting balance)."""
        audit = MagicMock()
        t = _make_tracker(starting_balance=100.0, audit=audit)
        # Set peak to 150 (a previous high)
        t._max_equity = 150.0

        # $30 withdrawal: starting 100 -> 70, peak 150 -> 120
        t.reconcile_external_balance(broker_balance=70.0)
        self.assertAlmostEqual(t.starting_balance, 70.0, places=4)
        # Peak dropped by exactly the withdrawal amount
        self.assertAlmostEqual(t._max_equity, 120.0, places=4)

    def test_audit_failure_is_graceful(self):
        """If the audit.append call raises, the reconcile
        method should NOT crash the cycle - log and return."""
        audit = MagicMock()
        # The constructor's baseline EQUITY_UPDATE will hit
        # audit.append first. We want the RECONCILE call to
        # raise. Approach: let the constructor's call pass
        # through (no side_effect), then install a side_effect
        # AFTER construction that raises on every call.
        t = _make_tracker(starting_balance=100.0, audit=audit)
        # Now install the failure mode.
        audit.append.side_effect = RuntimeError("audit ledger is broken")

        # Must not raise.
        result = t.reconcile_external_balance(broker_balance=120.0)
        self.assertFalse(result["audit_emitted"])
        # But the balance adjustment still happened - that's
        # in-memory state and the bot's snapshot logic depends
        # on it being correct.
        self.assertAlmostEqual(t.starting_balance, 120.0, places=4)


if __name__ == "__main__":
    unittest.main()
