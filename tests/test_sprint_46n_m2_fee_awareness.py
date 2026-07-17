"""
Sprint 46N — audit M2: fee-inconsistent closes + fee-blind profit-take.

Three close paths existed in this bot:
  1. PositionMonitor's SL/TP + smart-profit-take closes -- fee-aware
     since Sprint 46J (fee_pct_for_asset wired through).
  2. RiskManagerAgent._try_replace_position (position-replacement
     close) -- was NOT fee-aware; recorded gross P&L.
  3. src/api/state.py's manual dashboard close (single + bulk) -- was
     NOT fee-aware; always recorded exactly 0.0 (close_price ==
     entry_price) even though a real close always costs the fee.

On top of the inconsistency across close paths, PositionMonitor's
OWN smart-profit-take GATE (`min_profit_to_protect`, default 0.0 in
config.yaml) compared against RAW gross unrealized PnL -- so even
though the eventual close correctly subtracted fees, the decision to
close early didn't account for them, and could trigger a close that
nets a realized LOSS once fees are applied.

This file covers all four fixes:
  A. position_monitor.py: fee-aware min_profit_to_protect gate.
  B. risk_agent.py: RiskManagerAgent._try_replace_position now takes
     an optional `fee_pct_for_asset` and applies it on close.
  C. state.py: close_position/close_all_positions now take an
     optional `fee_pct`/`fee_pct_for_asset` and apply it.
  D. Backward compatibility: all three default to fee-free (0.0),
     unchanged behavior for callers that don't opt in.

Run: python -m unittest tests.test_sprint_46n_m2_fee_awareness -v
"""
import os
import sys
import tempfile
import time
import unittest
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data_store.positions import PositionRepository, Position
from src.data_store.position_monitor import PositionMonitor
from src.safety.audit_ledger import AuditLedger


def _crypto_fee(rate: float):
    """Fee callable that charges `rate` on BTC/ETH/SOL-* assets, 0.0
    on anything else (mirrors main.py's/server.py's real
    _fee_pct_for_asset, without needing brokers_config plumbing)."""
    def _fee(asset: str) -> float:
        return rate if asset.upper().startswith(("BTC", "ETH", "SOL")) else 0.0
    return _fee


# ============================================================
# A. position_monitor.py — fee-aware min_profit_to_protect gate
# ============================================================

class FeeAwareProfitProtectionTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=10000.0,
            stop_loss=9900.0,
            take_profit=10500.0,
            qty=0.001,             # $10 notional
            risk_usd=1.0,
            entry_ts=time.time() - 3600,
            strategy="momentum",
        )
        self.repo.add_open(self.pos)

    def _signals(self):
        return [{"asset": "BTC-USD", "direction": "short", "strength": 0.9}]

    def test_tiny_gross_profit_below_fee_cost_is_not_closed(self):
        """Gross profit ($0.01) is smaller than the round-trip fee at
        1% (~$0.20 on a $10 entry + ~$10 exit notional) -- must NOT
        trigger SMART_PROFIT_TAKE even though min_profit_to_protect
        defaults to 0.0 and the gross upnl is technically > 0."""
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit, broker=None,
            min_profit_to_protect=0.0,
            fee_pct_for_asset=_crypto_fee(0.01),  # 1% -- exaggerated for a clear test
        )
        # entry 10000, qty 0.001 -> $0.01 gross profit needs price = 10010
        closes = monitor.check_with_signals(
            current_prices={"BTC-USD": 10010.0},
            signals=self._signals(),
            signal_min_strength=0.6,
        )
        self.assertEqual(closes, [], "Tiny gross profit under fee cost must not be protected")
        self.assertEqual(self.repo.count_open(), 1)

    def test_gross_profit_comfortably_above_fee_cost_is_closed(self):
        """A gross profit clearly larger than the round-trip fee still
        triggers the early close, same as before this fix."""
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit, broker=None,
            min_profit_to_protect=0.0,
            fee_pct_for_asset=_crypto_fee(0.01),
        )
        # entry 10000 -> 10500 = $0.50 gross profit on 0.001 qty,
        # round-trip fee ~= (10 + 10.5) * 0.01 = ~$0.205 -- clears it.
        closes = monitor.check_with_signals(
            current_prices={"BTC-USD": 10500.0},
            signals=self._signals(),
            signal_min_strength=0.6,
        )
        self.assertEqual(len(closes), 1)
        self.assertGreater(closes[0].realized_pnl, 0.0)

    def test_no_fee_callable_preserves_old_behavior(self):
        """Without fee_pct_for_asset (None, the default), the gate is
        unchanged: any gross profit above min_profit_to_protect (0.0)
        triggers the close, exactly like before Sprint 46N M2."""
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit, broker=None,
            min_profit_to_protect=0.0,
        )
        closes = monitor.check_with_signals(
            current_prices={"BTC-USD": 10010.0},
            signals=self._signals(),
            signal_min_strength=0.6,
        )
        self.assertEqual(len(closes), 1)


# ============================================================
# B. risk_agent.py — RiskManagerAgent._try_replace_position fee wiring
# ============================================================

class RiskAgentReplacementFeeTest(unittest.TestCase):
    def setUp(self):
        from src.agents.risk_agent import RiskManagerAgent
        from src.data.asset_allocation import AllocationPolicy
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.worst = Position(
            asset="ETH-USD", direction="long",
            entry_price=3000.0, stop_loss=2950.0, take_profit=3150.0,
            qty=0.01, risk_usd=0.50,
            entry_ts=time.time() - 86400,
            strategy="old_loser",
        )
        self.repo.add_open(self.worst)
        self._policy = AllocationPolicy
        self.RiskManagerAgent = RiskManagerAgent

    def _make_agent(self, fee_pct_for_asset=None):
        return self.RiskManagerAgent(
            position_repo=self.repo,
            audit=self.audit,
            max_open_trades=1,
            enable_position_replacement=True,
            replacement_score_threshold=0.20,
            asset_concentration_check=False,
            allocation_policy=self._policy(enabled=False),
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            portfolio_stress_check=False,
            current_prices={"ETH-USD": 2900.0},  # worst is down -1.66%, no fee involved in scoring
            fee_pct_for_asset=fee_pct_for_asset,
        )

    def test_replacement_close_applies_fee_when_wired(self):
        agent = self._make_agent(fee_pct_for_asset=_crypto_fee(0.01))
        new_hyp = {
            "asset": "BTC-USD", "strategy": "momentum_breakout", "direction": "long",
            "price": 50000.0, "atr_at_signal": 500.0, "expected_move_pct": 4.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [new_hyp]}}
        result = agent.validate_and_size({}, state)
        self.assertEqual(len(result["approved_trades"]), 1, result["rejected_trades"])

        events = self.audit.read_all()
        replaced = [e for e in events if e.get("event_type") == "POSITION_REPLACED"]
        self.assertEqual(len(replaced), 1)
        # Gross P&L: (2900 - 3000) * 0.01 = -$1.00. Fee: (3000*0.01 +
        # 2900*0.01) * 0.01 = ~$0.59. Net realized should be MORE
        # negative than the raw gross loss because of the fee.
        gross_pnl = (2900.0 - 3000.0) * 0.01
        self.assertLess(replaced[0]["closed_pnl_usd"], gross_pnl,
                         "Fee-aware replacement close should record a larger loss than gross")

    def test_replacement_close_fee_free_when_not_wired(self):
        """Default (fee_pct_for_asset=None) preserves the pre-M2
        behavior: replacement closes are recorded at gross P&L."""
        agent = self._make_agent(fee_pct_for_asset=None)
        new_hyp = {
            "asset": "BTC-USD", "strategy": "momentum_breakout", "direction": "long",
            "price": 50000.0, "atr_at_signal": 500.0, "expected_move_pct": 4.0,
        }
        state = {"generate_hypotheses": {"hypotheses": [new_hyp]}}
        result = agent.validate_and_size({}, state)
        self.assertEqual(len(result["approved_trades"]), 1, result["rejected_trades"])

        events = self.audit.read_all()
        replaced = [e for e in events if e.get("event_type") == "POSITION_REPLACED"]
        self.assertEqual(len(replaced), 1)
        gross_pnl = (2900.0 - 3000.0) * 0.01
        self.assertAlmostEqual(replaced[0]["closed_pnl_usd"], gross_pnl, places=6)


# ============================================================
# C. state.py — manual dashboard close fee wiring
# ============================================================

class ManualCloseFeeTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_path = os.path.join(self.tmpdir, "audit.jsonl")
        self.positions_path = os.path.join(self.tmpdir, "positions.json")

    def test_flat_fee_pct_applied(self):
        from src.api.state import close_position
        repo = PositionRepository(path=self.positions_path)
        pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=50000, stop_loss=49000, take_profit=52000,
            qty=0.001, risk_usd=5, entry_ts=time.time(),
            strategy="momentum",
        )
        repo.add_open(pos)
        result = close_position(
            position_id=pos.position_id,
            audit_path=self.audit_path,
            positions_path=self.positions_path,
            fee_pct=0.01,
        )
        self.assertIsNotNone(result)
        # close_price == entry_price -> gross P&L 0.0; the fee itself
        # is the only realized loss: (entry_notional + exit_notional) *
        # fee_pct = (50 + 50) * 0.01 = $1.00 (qty*entry both sides).
        self.assertLess(result["realized_pnl_usd"], 0.0)
        self.assertAlmostEqual(result["realized_pnl_usd"], -1.0, places=6)

    def test_fee_pct_for_asset_overrides_flat_fee_pct(self):
        from src.api.state import close_position
        repo = PositionRepository(path=self.positions_path)
        pos = Position(
            asset="SPY", direction="long",
            entry_price=500.0, stop_loss=490.0, take_profit=520.0,
            qty=1.0, risk_usd=10, entry_ts=time.time(),
            strategy="momentum",
        )
        repo.add_open(pos)
        result = close_position(
            position_id=pos.position_id,
            audit_path=self.audit_path,
            positions_path=self.positions_path,
            fee_pct=0.05,  # would apply if fee_pct_for_asset didn't win
            fee_pct_for_asset=_crypto_fee(0.05),  # SPY isn't crypto -> 0.0
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["realized_pnl_usd"], 0.0)

    def test_no_fee_supplied_stays_zero(self):
        """Default (no fee_pct, no fee_pct_for_asset) preserves the
        original 0.0 realized_pnl for a manual close -- no regression
        for existing dashboard behavior/tests."""
        from src.api.state import close_position
        repo = PositionRepository(path=self.positions_path)
        pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=50000, stop_loss=49000, take_profit=52000,
            qty=0.001, risk_usd=5, entry_ts=time.time(),
            strategy="momentum",
        )
        repo.add_open(pos)
        result = close_position(
            position_id=pos.position_id,
            audit_path=self.audit_path,
            positions_path=self.positions_path,
        )
        self.assertEqual(result["realized_pnl_usd"], 0.0)

    def test_close_all_positions_applies_per_asset_fee(self):
        """Bulk close with a mixed crypto/equity book: fee_pct_for_asset
        must be resolved PER POSITION, not shared flat across the
        batch -- crypto gets charged, equity doesn't."""
        from src.api.state import close_all_positions
        repo = PositionRepository(path=self.positions_path)
        btc = Position(
            asset="BTC-USD", direction="long",
            entry_price=50000, stop_loss=49000, take_profit=52000,
            qty=0.001, risk_usd=5, entry_ts=time.time(), strategy="s",
        )
        spy = Position(
            asset="SPY", direction="long",
            entry_price=500.0, stop_loss=490.0, take_profit=520.0,
            qty=1.0, risk_usd=10, entry_ts=time.time(), strategy="s",
        )
        repo.add_open(btc)
        repo.add_open(spy)
        closed = close_all_positions(
            audit_path=self.audit_path,
            positions_path=self.positions_path,
            fee_pct_for_asset=_crypto_fee(0.01),
        )
        self.assertEqual(len(closed), 2)
        by_asset = {c["asset"]: c for c in closed}
        self.assertLess(by_asset["BTC-USD"]["realized_pnl_usd"], 0.0)
        self.assertEqual(by_asset["SPY"]["realized_pnl_usd"], 0.0)


# ============================================================
# Sprint 46O (audit M2 follow-up): 2x fee multiplier, entry fee in
# sizing+mandate, auto-detect fee tier from broker.
# ============================================================

class FeeMultiplierTest(unittest.TestCase):
    """Sprint 46O (audit M2): default min_profit_fee_multiplier is 2.0
    (not 1.0) so a SMART_PROFIT_TAKE close can't end up a realized
    net loss when an operator leaves min_profit_to_protect at 0.0.
    With a 1x multiplier, gross profit == round-trip fee == NET 0;
    any basis point of slippage or rounding becomes a loss. The
    audit's exact wording: "min_profit_to_protect >= 2x fee"."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))

    def _signals(self):
        return [{"asset": "BTC-USD", "direction": "short", "strength": 0.9}]

    def test_default_multiplier_is_2x(self):
        """The constructor default must be 2.0 (not 1.0). Operators
        who don't set the config should still be safe by default."""
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit, broker=None,
        )
        self.assertEqual(monitor.min_profit_fee_multiplier, 2.0)

    def test_1x_fee_multiplier_is_explicit_opt_in(self):
        """A caller who really wants the pre-Sprint-46O 1x behavior
        can still get it by passing it explicitly — useful for
        paper-mode backtests, or for exchanges with zero fees. Just
        don't let it be the accidental default."""
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit, broker=None,
            min_profit_fee_multiplier=1.0,
        )
        self.assertEqual(monitor.min_profit_fee_multiplier, 1.0)

    def test_profit_above_1x_but_below_2x_fee_is_not_closed(self):
        """Gross profit of $0.30 on a $10 position with 1% fee:
          - round-trip fee = $20 * 0.01 = $0.20
          - 1x floor = $0.20 (would close: profit $0.30 > $0.20)
          - 2x floor = $0.40 (must NOT close: profit $0.30 < $0.40)
        This is the exact reason the audit asked for 2x: the
        1x floor nets ~$0.10 of profit, but a 0.5% slippage turns
        it into a loss. 2x is the safe floor."""
        pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=10000.0, stop_loss=9900.0, take_profit=10500.0,
            qty=0.001, risk_usd=1.0, entry_ts=time.time() - 3600,
            strategy="momentum",
        )
        self.repo.add_open(pos)
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit, broker=None,
            min_profit_to_protect=0.0,
            min_profit_fee_multiplier=2.0,
            fee_pct_for_asset=_crypto_fee(0.01),
        )
        # entry=10000, qty=0.001, profit needs price=10300 (gross $0.30)
        closes = monitor.check_with_signals(
            current_prices={"BTC-USD": 10300.0},
            signals=self._signals(),
        )
        self.assertEqual(closes, [],
                         "Gross profit between 1x and 2x fee must NOT be "
                         "protected under the new 2x default")
        self.assertEqual(self.repo.count_open(), 1)

    def test_profit_above_2x_fee_is_closed(self):
        """Profit comfortably above 2x round-trip fee still triggers
        the early close — the new multiplier doesn't make the gate
        too conservative to ever fire."""
        pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=10000.0, stop_loss=9900.0, take_profit=10500.0,
            qty=0.001, risk_usd=1.0, entry_ts=time.time() - 3600,
            strategy="momentum",
        )
        self.repo.add_open(pos)
        monitor = PositionMonitor(
            repo=self.repo, audit=self.audit, broker=None,
            min_profit_to_protect=0.0,
            min_profit_fee_multiplier=2.0,
            fee_pct_for_asset=_crypto_fee(0.01),
        )
        # round-trip fee = $0.20, 2x = $0.40. Price 10500 -> gross profit $0.50
        closes = monitor.check_with_signals(
            current_prices={"BTC-USD": 10500.0},
            signals=self._signals(),
        )
        self.assertEqual(len(closes), 1)
        self.assertEqual(self.repo.count_open(), 0)

    def test_multiplier_clamped_to_sane_range(self):
        """Negative or 100x multipliers are almost certainly typos.
        The constructor clamps to [0, 10] without raising — a typo
        in config.yaml must not be a startup error."""
        for bad, expected in [(-1.0, 0.0), (50.0, 10.0), (0.0, 0.0)]:
            m = PositionMonitor(
                repo=self.repo, audit=self.audit, broker=None,
                min_profit_fee_multiplier=bad,
            )
            self.assertEqual(
                m.min_profit_fee_multiplier, expected,
                f"multiplier={bad} should clamp to {expected}",
            )


# ============================================================
# Sprint 46O (audit M2): auto-detect fee tier from broker
# ============================================================

class FetchFeeRateTest(unittest.TestCase):
    """Sprint 46O (audit M2): BrokerClient.fetch_fee_rate() returns
    the user's actual per-account commission from the exchange, so
    the bot can WARN at startup if the configured fee differs from
    reality. The Carlos case in point: the bot assumed 0.1% taker,
    his real tier is 0.02% (5x lower) — every TP was being set
    further from break-even than needed."""

    def test_returns_commission_rates_when_present(self):
        """binance.us /account endpoint puts rates under
        info.commissionRates.{maker,taker} as decimal strings
        (e.g. "0.00020000")."""
        from src.execution.broker import BrokerClient

        class _FakeEx:
            def fetch_balance(self):
                return {
                    "info": {
                        "commissionRates": {
                            "maker": "0.00000000",
                            "taker": "0.00020000",
                            "buyer": "0",
                            "seller": "0",
                        },
                        "takerCommission": "2",
                        "makerCommission": "0",
                    }
                }

        class _FakeClient:
            exchange = _FakeEx()

        maker, taker = BrokerClient.fetch_fee_rate(_FakeClient())
        self.assertAlmostEqual(maker, 0.0)
        self.assertAlmostEqual(taker, 0.0002)

    def test_falls_back_to_basis_points_shape(self):
        """Older binance.us accounts return `takerCommission` as a
        basis-point int (e.g. "2" = 0.02%) without a
        commissionRates dict. The fallback should still resolve."""
        from src.execution.broker import BrokerClient

        class _FakeEx:
            def fetch_balance(self):
                return {
                    "info": {
                        # No commissionRates — only the older fields
                        "takerCommission": "2",
                        "makerCommission": "0",
                    }
                }

        class _FakeClient:
            exchange = _FakeEx()

        maker, taker = BrokerClient.fetch_fee_rate(_FakeClient())
        self.assertAlmostEqual(taker, 0.0002)
        self.assertAlmostEqual(maker, 0.0)

    def test_returns_none_none_on_total_failure(self):
        """Any error -> (None, None) so the caller can fall back to
        the config value. The function must never raise — a fee
        query that breaks startup would be worse than no auto-
        detection at all."""
        from src.execution.broker import BrokerClient

        class _FakeEx:
            def fetch_balance(self):
                raise RuntimeError("rate limited")

        class _FakeClient:
            exchange = _FakeEx()

        maker, taker = BrokerClient.fetch_fee_rate(_FakeClient())
        self.assertIsNone(maker)
        self.assertIsNone(taker)

    def test_returns_none_when_no_fee_info_anywhere(self):
        """fetch_balance() succeeded but no fee fields in the
        response. Common for testnet/sandbox. Result: (None, None)
        — caller falls back to config."""
        from src.execution.broker import BrokerClient

        class _FakeEx:
            def fetch_balance(self):
                return {"info": {"balances": []}, "free": {}, "used": {}, "total": {}}

        class _FakeClient:
            exchange = _FakeEx()

        maker, taker = BrokerClient.fetch_fee_rate(_FakeClient())
        self.assertIsNone(maker)
        self.assertIsNone(taker)


# ============================================================
# Sprint 46O (audit M2): entry fee in sizing (trade_proposal) +
# in mandate cap checks.
# ============================================================

class EntryFeeInMandateTest(unittest.TestCase):
    """Sprint 46O (audit M2): the entry-side exchange fee must be
    counted in the mandate's caps. The audit's exact finding: "el
    fee de entrada tampoco está en el sizing ni en el mandato"."""

    def setUp(self):
        from src.safety.mandate_gate import MandateConfig, MandateGate
        self.cfg = MandateConfig(
            enabled=True,
            allowed_symbols=set(),  # all allowed
            max_position_usd=10.0,
            max_daily_loss_usd=5.0,
            max_total_exposure_usd=100.0,
        )
        self.gate = MandateGate(self.cfg)

    def test_proposal_without_entry_fee_uses_notional_only(self):
        """Backward-compat: a proposal that doesn't carry the new
        entry_fee_usd field falls back to notional-only. The
        mandate must never fail-closed on a missing field — too
        many callers (dashboard, tests, legacy paths) build the
        proposal without it."""
        verdict = self.gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 9.99,  # under cap
            "risk_usd": 0.20,
        })
        self.assertTrue(verdict.ok, f"unexpected reject: {verdict.reason}")

    def test_proposal_with_entry_fee_counts_it(self):
        """The exact audit scenario: a $10 trade with 0.1% fee =
        $0.01 entry fee. notional_usd = $10.00, notional_with_fees
        = $10.01. max_position_usd = $10.00. Pre-fix this PASSED
        (notional=10 == cap), post-fix it FAILS
        (notional+fee=$10.01 > cap)."""
        verdict = self.gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 10.00,
            "entry_fee_usd": 0.01,
            "notional_with_fees_usd": 10.01,
            "risk_usd": 0.20,
        })
        self.assertFalse(verdict.ok)
        self.assertIn("notional_exceeds_max", verdict.reason)

    def test_proposal_just_under_cap_with_fee_passes(self):
        """$9.99 notional + $0.01 fee = $10.00 == cap -> OK
        (the mandate's check is `>` not `>=`, so equal passes)."""
        verdict = self.gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 9.99,
            "entry_fee_usd": 0.01,
            "notional_with_fees_usd": 10.00,
            "risk_usd": 0.20,
        })
        self.assertTrue(verdict.ok, f"unexpected reject: {verdict.reason}")

    def test_legacy_falls_back_to_entry_fee_field(self):
        """A proposal with only `entry_fee_usd` (no
        `notional_with_fees_usd`) still gets the fee counted —
        derived internally. The combined field is the
        preferred/cheaper path; the fallback is for callers that
        build the proposal partially."""
        verdict = self.gate.validate({
            "asset": "BTC-USD",
            "notional_usd": 9.995,
            "entry_fee_usd": 0.01,  # => notional_with_fees = 10.005 > 10
            "risk_usd": 0.20,
        })
        self.assertFalse(verdict.ok, "fee fallback to entry_fee_usd should have caught this")


# ============================================================
# Sprint 46O (audit M2): integration — risk_agent populates the
# new entry_fee fields on the trade_proposal that the mandate then
# sees. This is the wiring test: the field must be set with the
# right name and the right value, and it must flow into the cap.
# ============================================================

class RiskAgentEntryFeeInProposalTest(unittest.TestCase):
    """Sprint 46O (audit M2): the audit's exact finding was "el fee
    de entrada tampoco está en el sizing ni en el mandato" — i.e.
    the trade_proposal that RiskManagerAgent hands to the
    MandateGate had no entry fee info, and the mandate didn't know
    to ask. This test pins both halves of that wiring."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_trade_proposal_carries_entry_fee_for_crypto(self):
        """A $10 BTC-USD trade at 0.1% taker must arrive at the
        mandate with entry_fee_usd=0.01 and
        notional_with_fees_usd=10.01. The mandate test
        `test_proposal_with_entry_fee_counts_it` then exercises
        the consumption side; this test pins the production
        side (the risk_agent's output)."""
        from src.data_store.positions import PositionRepository
        from src.safety.mandate_gate import MandateConfig, MandateGate

        repo = PositionRepository(path=os.path.join(self.tmpdir, "positions.json"))
        # Mandate with a $10.005 cap — $10 notional PASSES the old
        # notional-only check, fails the new notional+fee check.
        gate = MandateGate(MandateConfig(
            enabled=True, allowed_symbols=set(),
            max_position_usd=10.005,
            max_daily_loss_usd=5.0, max_total_exposure_usd=100.0,
        ))

        captured = {}
        original_validate = gate.validate

        def _capture_validate(trade, **kwargs):
            captured.update(trade)
            return original_validate(trade, **kwargs)

        gate.validate = _capture_validate

        from src.agents.risk_agent import RiskManagerAgent
        agent = RiskManagerAgent(
            broker_client=None,  # no broker -> paper-mode balance sim
            alpaca_broker=None,
            brokers_config={"crypto": {"symbols": ["BTC-USD"]}, "equity": {"symbols": ["SPY"]}},
            audit=None,
            position_repo=repo,
            mandate_gate=gate,
            min_order_usd=10.0,
            max_capital_per_trade_pct=50.0,
            risk_per_trade_pct=1.0,
            atr_stop_multiplier=2.0,
            atr_take_profit_multiplier=4.0,
            max_open_trades=5,
            asset_concentration_check=False,
            portfolio_stress_check=False,
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            block_conflicting_asset_positions=False,
            fee_pct_for_asset=_crypto_fee(0.001),  # 0.1% — the config default
        )

        # Paper mode so the no-broker balance fallback works
        override_path = os.path.join(self.tmpdir, "mode_override.json")
        with open(override_path, "w") as f:
            json.dump({"mandate_enabled": False}, f)
        agent.mode_override_path = override_path

        state = {
            "generate_hypotheses": {
                "hypotheses": [
                    {
                        "asset": "BTC-USD", "direction": "long",
                        "strategy": "test", "price": 50000.0,
                        "atr_at_signal": 500.0,
                    }
                ]
            }
        }
        agent.validate_and_size({}, state)

        self.assertEqual(captured.get("asset"), "BTC-USD",
                         "mandate was never called for BTC-USD — sizing failed before mandate")
        # The two new fields the audit asked for:
        self.assertIn("entry_fee_usd", captured,
                      "RiskManagerAgent must populate entry_fee_usd "
                      "on every trade_proposal (audit M2)")
        self.assertIn("notional_with_fees_usd", captured,
                      "RiskManagerAgent must populate notional_with_fees_usd "
                      "on every trade_proposal (audit M2)")
        self.assertIn("entry_fee_pct", captured)
        # Values: at 0.1% taker on a $50 notional (no broker, $100
        # paper balance * 50% max_cap = $50), entry fee is $0.05,
        # and the all-in cost is $50.05. The exact dollar amount
        # depends on the no-broker paper-mode balance ($100), but
        # the *relationship* notional*0.001 = entry_fee must always
        # hold for a crypto asset.
        self.assertAlmostEqual(captured["entry_fee_pct"], 0.001, places=6)
        self.assertAlmostEqual(
            captured["entry_fee_usd"],
            captured["notional_usd"] * 0.001,
            places=4,
        )
        self.assertAlmostEqual(
            captured["notional_with_fees_usd"],
            captured["notional_usd"] + captured["entry_fee_usd"],
            places=4,
        )

    def test_trade_proposal_entry_fee_zero_for_equity(self):
        """Alpaca equities are commission-free, so an SPY trade
        proposal must carry entry_fee_usd=0.0 — the mandate
        shouldn't penalize equity trades with phantom fees."""
        from src.data_store.positions import PositionRepository
        from src.safety.mandate_gate import MandateConfig, MandateGate

        repo = PositionRepository(path=os.path.join(self.tmpdir, "positions.json"))
        gate = MandateGate(MandateConfig(
            enabled=True, allowed_symbols=set(),
            max_position_usd=10000.0,
            max_daily_loss_usd=5.0, max_total_exposure_usd=100000.0,
        ))
        captured = {}
        original_validate = gate.validate
        def _capture_validate(trade, **kwargs):
            captured.update(trade)
            return original_validate(trade, **kwargs)
        gate.validate = _capture_validate

        from src.agents.risk_agent import RiskManagerAgent
        agent = RiskManagerAgent(
            broker_client=None, alpaca_broker=None,
            brokers_config={"crypto": {"symbols": ["BTC-USD"]}, "equity": {"symbols": ["SPY"]}},
            audit=None, position_repo=repo, mandate_gate=gate,
            min_order_usd=10.0, max_capital_per_trade_pct=50.0,
            risk_per_trade_pct=1.0,
            atr_stop_multiplier=2.0, atr_take_profit_multiplier=4.0,
            max_open_trades=5,
            asset_concentration_check=False,
            portfolio_stress_check=False,
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            block_conflicting_asset_positions=False,
            fee_pct_for_asset=_crypto_fee(0.001),  # only BTC/ETH/SOL -> fee, SPY -> 0.0
        )
        override_path = os.path.join(self.tmpdir, "mode_override.json")
        with open(override_path, "w") as f:
            json.dump({"mandate_enabled": False}, f)
        agent.mode_override_path = override_path

        state = {
            "generate_hypotheses": {
                "hypotheses": [
                    {
                        "asset": "SPY", "direction": "long",
                        "strategy": "test", "price": 500.0,
                        "atr_at_signal": 5.0,
                    }
                ]
            }
        }
        agent.validate_and_size({}, state)

        self.assertEqual(captured.get("asset"), "SPY")
        self.assertEqual(captured["entry_fee_pct"], 0.0)
        self.assertEqual(captured["entry_fee_usd"], 0.0)
        self.assertEqual(captured["notional_with_fees_usd"], captured["notional_usd"])


if __name__ == "__main__":
    unittest.main()
