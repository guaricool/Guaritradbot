"""
Carlos: "no se ve el descuento en el balance... si eso pasa en live,
puede causar confusion". The "Paper balance (simulated)" hero card
used `paper_starting_usd + total_pnl` (realized P&L only), which never
changes the instant a position OPENS — only once it closes. So the
card kept showing the full starting balance as "available" while part
of it was actually locked in an open paper position.

Fix in src/api/state.py::build_state_snapshot: effective_balance_usd
in paper mode now subtracts the notional of currently OPEN positions
(the same total_exposure_usd figure the "Open positions" card uses),
so opening a position visibly debits it and closing credits it back
via realized_pnl. Also fixes a second, related gap: in LIVE mode
effective_balance_usd was always None (hero card showed "—" forever)
despite its own docstring claiming "In live mode = real broker
balance" — it's now the sum of whichever live broker balances are
configured.

Run: python -m unittest tests.test_effective_balance_reflects_open_positions -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.api.state import build_state_snapshot


def _write_mode_override(audit_path, mandate_enabled: bool):
    parent = Path(audit_path).parent
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "mode_override.json").write_text(
        json.dumps({"mandate_enabled": mandate_enabled, "switched_at": 1700000000.0, "switched_by": "test"}),
        encoding="utf-8",
    )


class EffectiveBalanceOpenPositionDiscountTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_path = os.path.join(self.tmpdir, "audit.jsonl")
        self.positions_path = os.path.join(self.tmpdir, "positions.json")
        Path(self.audit_path).write_text("", encoding="utf-8")

    def _write_open_position(self, notional_usd: float):
        qty = 1.0
        entry_price = notional_usd  # qty=1 keeps notional == entry_price for simplicity
        positions = [{
            "position_id": "p_open",
            "asset": "GLD",
            "direction": "long",
            "entry_price": entry_price,
            "qty": qty,
            "risk_usd": 5.0,
            "entry_ts": time.time(),
            "stop_loss": entry_price * 0.98,
            "take_profit": entry_price * 1.02,
            "protection_mode": "polling",
            "strategy": "test",
        }]
        Path(self.positions_path).write_text(json.dumps({"positions": positions}), encoding="utf-8")

    def test_open_position_debits_paper_effective_balance(self):
        """$1,000 paper balance, open a $200 position -> available must
        drop to $800, not stay at $1,000."""
        _write_mode_override(self.audit_path, mandate_enabled=False)
        self._write_open_position(notional_usd=200.0)
        cfg = {"mandate": {"enabled": False}, "paper": {"starting_balance_usd": 1000.0}}

        with patch("src.api.state.read_current_prices", return_value={"GLD": 200.0}):
            snap = build_state_snapshot(config=cfg, audit_path=self.audit_path, positions_path=self.positions_path)

        self.assertEqual(snap.effective_balance_source, "paper_simulated")
        self.assertEqual(snap.total_exposure_usd, 200.0)
        self.assertAlmostEqual(snap.effective_balance_usd, 800.0, places=2)

    def test_closing_position_restores_balance_via_realized_pnl(self):
        """No open positions, but one closed at +$10 realized -> back
        to starting + 10, nothing left debited."""
        _write_mode_override(self.audit_path, mandate_enabled=False)
        positions = [{
            "position_id": "p_closed",
            "asset": "GLD",
            "direction": "long",
            "entry_price": 200.0,
            "qty": 1.0,
            "risk_usd": 5.0,
            "entry_ts": time.time() - 3600,
            "stop_loss": 196.0,
            "take_profit": 204.0,
            "protection_mode": "polling",
            "strategy": "test",
            "closed_ts": time.time() - 60,
            "closed_price": 210.0,
            "close_reason": "TP_HIT",
            "realized_pnl": 10.0,
        }]
        Path(self.positions_path).write_text(json.dumps({"positions": positions}), encoding="utf-8")
        cfg = {"mandate": {"enabled": False}, "paper": {"starting_balance_usd": 1000.0}}

        snap = build_state_snapshot(config=cfg, audit_path=self.audit_path, positions_path=self.positions_path)
        self.assertEqual(snap.total_exposure_usd, 0.0)
        self.assertAlmostEqual(snap.effective_balance_usd, 1010.0, places=2)

    def test_no_open_positions_behaves_like_before(self):
        _write_mode_override(self.audit_path, mandate_enabled=False)
        Path(self.positions_path).write_text(json.dumps({"positions": []}), encoding="utf-8")
        cfg = {"mandate": {"enabled": False}, "paper": {"starting_balance_usd": 1000.0}}
        snap = build_state_snapshot(config=cfg, audit_path=self.audit_path, positions_path=self.positions_path)
        self.assertEqual(snap.effective_balance_usd, 1000.0)


class EffectiveBalanceLiveModeGapTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_path = os.path.join(self.tmpdir, "audit.jsonl")
        self.positions_path = os.path.join(self.tmpdir, "positions.json")
        Path(self.audit_path).write_text("", encoding="utf-8")
        Path(self.positions_path).write_text(json.dumps({"positions": []}), encoding="utf-8")

    def test_live_mode_effective_balance_no_longer_always_none(self):
        _write_mode_override(self.audit_path, mandate_enabled=True)
        cfg = {"mandate": {"enabled": True}}
        with patch("src.api.state._get_binance_balance", return_value=(500.0, "live")), \
             patch("src.api.state._get_alpaca_balance", return_value=(1500.0, "live")):
            snap = build_state_snapshot(config=cfg, audit_path=self.audit_path, positions_path=self.positions_path)

        self.assertEqual(snap.effective_balance_source, "broker_live")
        self.assertAlmostEqual(snap.effective_balance_usd, 2000.0, places=2)

    def test_live_mode_no_brokers_configured_is_zero_not_crash(self):
        _write_mode_override(self.audit_path, mandate_enabled=True)
        cfg = {"mandate": {"enabled": True}}
        with patch("src.api.state._get_binance_balance", return_value=(None, "not_configured")), \
             patch("src.api.state._get_alpaca_balance", return_value=(None, "not_configured")):
            snap = build_state_snapshot(config=cfg, audit_path=self.audit_path, positions_path=self.positions_path)

        self.assertEqual(snap.effective_balance_usd, 0.0)


if __name__ == "__main__":
    unittest.main()
