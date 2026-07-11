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


if __name__ == "__main__":
    unittest.main()
