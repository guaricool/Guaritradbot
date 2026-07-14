"""Sprint 62 — Paper-mode virtual starting balance.

Carlos's ask: when the bot is in paper mode, the dashboard should
show a meaningful "paper account" balance (e.g. $1,000) instead of
the real broker balance (which is $22.08 — too small for realistic
test trades because 1% risk × $22 = $0.22, fees alone wipe out any
profit).

This file tests:
  1. config.yaml has the new `paper.starting_balance_usd` knob.
  2. The `paper` config is parsed correctly with safe defaults.
  3. `build_state_snapshot` populates the new fields in paper mode.
  4. `build_state_snapshot` returns nulls for paper fields in live mode.
  5. `effective_balance_usd` = paper_starting + realized P&L.
  6. `effective_balance_source` is "paper_simulated" in paper mode
     and "broker_live" in live mode.
  7. The legacy `balance_usd` field still works (broker balance
     mirror) for back-compat with anything consuming the old API.
  8. Malformed/missing `paper.starting_balance_usd` falls back to
     the default $1,000.

All tests are pure unit tests on the StateSnapshot helper — no bot
runtime, no broker, no FS writes. Pattern matches Sprint 59's
test_sprint_59_charts.py isolation style.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Force UTF-8 stdout before any module prints emojis (Windows
# safety — same pattern as Sprint 57's test file).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Make the bot's src/ importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.api.state import (  # noqa: E402
    StateSnapshot,
    build_state_snapshot,
    read_mode,
    write_mode,
)


def _write_config_with_paper(paper_value, audit_path, positions_path):
    """Helper: write a minimal config.yaml in a tempdir that has the
    `paper:` section set to the requested value (or omitted)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="sprint62_cfg_"))
    cfg_path = tmpdir / "config.yaml"
    if paper_value is None:
        cfg_path.write_text(
            "mandate:\n"
            "  enabled: false\n"
            "trading:\n"
            "  risk_per_trade_pct: 1.0\n",
            encoding="utf-8",
        )
    else:
        cfg_path.write_text(
            "mandate:\n"
            "  enabled: false\n"
            "paper:\n"
            f"  starting_balance_usd: {paper_value}\n"
            "trading:\n"
            "  risk_per_trade_pct: 1.0\n",
            encoding="utf-8",
        )
    return cfg_path


def _empty_positions_file():
    """Return a path to a valid (empty) positions.json file."""
    f = Path(tempfile.mktemp(prefix="sprint62_pos_", suffix=".json"))
    f.write_text("[]", encoding="utf-8")
    return f


def _empty_audit_file():
    """Return a path to a valid (empty) audit.jsonl file."""
    f = Path(tempfile.mktemp(prefix="sprint62_audit_", suffix=".jsonl"))
    f.write_text("", encoding="utf-8")
    return f


def _override_mode(audit_path, mandate_enabled: bool):
    """Write the mode_override.json file in the audit_path's parent dir."""
    parent = Path(audit_path).parent
    parent.mkdir(parents=True, exist_ok=True)
    override = {
        "mandate_enabled": mandate_enabled,
        "switched_at": 1700000000.0,
        "switched_by": "test",
    }
    (parent / "mode_override.json").write_text(
        json.dumps(override), encoding="utf-8"
    )


class TestSprint62PaperStartingBalanceConfig(unittest.TestCase):
    """Sprint 62 config.yaml knob — paper.starting_balance_usd."""

    def test_paper_starting_balance_usd_is_1000_by_default(self):
        """The shipped config.yaml must have paper.starting_balance_usd
        set to 1000.0 (Carlos's chosen default for paper simulation)."""
        import yaml

        cfg_path = _REPO_ROOT / "config.yaml"
        self.assertTrue(
            cfg_path.exists(),
            f"config.yaml not found at {cfg_path}",
        )
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        paper_cfg = cfg.get("paper") or {}
        self.assertEqual(
            paper_cfg.get("starting_balance_usd"),
            1000.0,
            "config.yaml must have paper.starting_balance_usd: 1000.0",
        )


class TestSprint62BuildStateSnapshotPaperFields(unittest.TestCase):
    """Sprint 62 — paper fields in StateSnapshot."""

    def setUp(self):
        self.audit_path = _empty_audit_file()
        self.positions_path = _empty_positions_file()

    def test_paper_mode_populates_paper_fields(self):
        """In paper mode, effective_balance_usd is computed from
        paper.starting_balance_usd and the new fields are populated."""
        _override_mode(self.audit_path, mandate_enabled=False)
        cfg = {
            "mandate": {"enabled": False},
            "paper": {"starting_balance_usd": 1000.0},
        }
        snap = build_state_snapshot(
            config=cfg,
            audit_path=str(self.audit_path),
            positions_path=str(self.positions_path),
        )
        self.assertEqual(snap.mode.mode, "paper")
        self.assertEqual(snap.effective_balance_source, "paper_simulated")
        self.assertEqual(snap.paper_starting_balance_usd, 1000.0)
        # No closed trades yet → effective balance == paper starting
        self.assertEqual(snap.effective_balance_usd, 1000.0)

    def test_live_mode_paper_fields_are_null(self):
        """In live mode, the new paper fields are null and the
        effective balance source is `broker_live`."""
        _override_mode(self.audit_path, mandate_enabled=True)
        cfg = {
            "mandate": {"enabled": True},
            "paper": {"starting_balance_usd": 1000.0},  # ignored in live
        }
        snap = build_state_snapshot(
            config=cfg,
            audit_path=str(self.audit_path),
            positions_path=str(self.positions_path),
        )
        self.assertEqual(snap.mode.mode, "live")
        self.assertEqual(snap.effective_balance_source, "broker_live")
        self.assertIsNone(snap.paper_starting_balance_usd)
        # effective_balance_usd in live mode comes from the broker,
        # not from the config; it may be None if no broker is wired.
        # We just check it's not the paper value.
        if snap.effective_balance_usd is not None:
            self.assertNotEqual(snap.effective_balance_usd, 1000.0)

    def test_effective_balance_includes_realized_pnl(self):
        """Effective balance = paper_starting + sum of realized P&L
        across all closed positions."""
        # Write a positions.json with one closed winning trade and
        # one closed losing trade.
        import time as _time

        now = _time.time()
        positions = [
            {
                "position_id": "p_win",
                "asset": "BTC-USD",
                "direction": "long",
                "entry_price": 60000.0,
                "qty": 0.001,
                "risk_usd": 0.5,
                "entry_ts": now - 7200,
                "stop_loss": 59500.0,
                "take_profit": 61000.0,
                "protection_mode": "polling",
                "strategy": "TestLong",
                "closed_ts": now - 3600,
                "closed_price": 60500.0,
                "close_reason": "TP_HIT",
                "realized_pnl": 0.5,  # +$0.50
            },
            {
                "position_id": "p_loss",
                "asset": "ETH-USD",
                "direction": "long",
                "entry_price": 1800.0,
                "qty": 0.01,
                "risk_usd": 0.2,
                "entry_ts": now - 1800,
                "stop_loss": 1780.0,
                "take_profit": 1840.0,
                "protection_mode": "polling",
                "strategy": "TestLong",
                "closed_ts": now - 600,
                "closed_price": 1790.0,
                "close_reason": "STOP_HIT",
                "realized_pnl": -0.1,  # -$0.10
            },
        ]
        self.positions_path.write_text(
            json.dumps({"positions": positions}), encoding="utf-8"
        )
        _override_mode(self.audit_path, mandate_enabled=False)
        cfg = {
            "mandate": {"enabled": False},
            "paper": {"starting_balance_usd": 1000.0},
        }
        snap = build_state_snapshot(
            config=cfg,
            audit_path=str(self.audit_path),
            positions_path=str(self.positions_path),
        )
        # Net realized P&L = +0.5 - 0.1 = +0.4
        # Effective balance = 1000 + 0.4 = 1000.4
        self.assertAlmostEqual(
            snap.effective_balance_usd, 1000.4, places=2,
            msg=f"expected 1000 + 0.4 = 1000.4, got {snap.effective_balance_usd}",
        )
        self.assertEqual(snap.total_realized_pnl_usd, 0.4)
        # Daily P&L: both closed within 24h
        self.assertAlmostEqual(snap.daily_realized_pnl_usd, 0.4, places=2)

    def test_missing_paper_config_falls_back_to_1000(self):
        """If `paper:` is missing from config entirely, fall back to
        the default $1,000 (same as if `starting_balance_usd` is 0
        or negative)."""
        _override_mode(self.audit_path, mandate_enabled=False)
        cfg = {"mandate": {"enabled": False}}  # no `paper` key
        snap = build_state_snapshot(
            config=cfg,
            audit_path=str(self.audit_path),
            positions_path=str(self.positions_path),
        )
        self.assertEqual(snap.paper_starting_balance_usd, 1000.0)
        self.assertEqual(snap.effective_balance_usd, 1000.0)

    def test_zero_paper_starting_balance_falls_back_to_1000(self):
        """`paper.starting_balance_usd: 0` or negative is invalid and
        must fall back to the $1,000 default — the math breaks at
        0 (can't compute 1% of 0)."""
        _override_mode(self.audit_path, mandate_enabled=False)
        for bad_value in (0, -100, -1.5):
            cfg = {
                "mandate": {"enabled": False},
                "paper": {"starting_balance_usd": bad_value},
            }
            snap = build_state_snapshot(
                config=cfg,
                audit_path=str(self.audit_path),
                positions_path=str(self.positions_path),
            )
            self.assertEqual(
                snap.paper_starting_balance_usd, 1000.0,
                f"bad value {bad_value!r} should fall back to 1000.0",
            )

    def test_malformed_paper_starting_balance_falls_back_to_1000(self):
        """A non-numeric `paper.starting_balance_usd` (e.g. a string
        like 'lots') must not crash the snapshot — fall back to
        the default."""
        _override_mode(self.audit_path, mandate_enabled=False)
        cfg = {
            "mandate": {"enabled": False},
            "paper": {"starting_balance_usd": "lots"},
        }
        snap = build_state_snapshot(
            config=cfg,
            audit_path=str(self.audit_path),
            positions_path=str(self.positions_path),
        )
        self.assertEqual(snap.paper_starting_balance_usd, 1000.0)

    def test_legacy_balance_field_still_present(self):
        """Sprint 62 added new fields but must NOT remove the legacy
        `balance_usd` and `balance_source` (consumed by older API
        clients — see config61.yaml's note about back-compat)."""
        _override_mode(self.audit_path, mandate_enabled=False)
        cfg = {
            "mandate": {"enabled": False},
            "paper": {"starting_balance_usd": 1000.0},
        }
        snap = build_state_snapshot(
            config=cfg,
            audit_path=str(self.audit_path),
            positions_path=str(self.positions_path),
        )
        # The legacy field should still be a float (default 0.0 if
        # no broker is wired in this test).
        self.assertIsInstance(snap.balance_usd, float)
        self.assertIsInstance(snap.balance_source, str)
        # The new effective_balance_usd should also be a float in paper mode.
        self.assertIsInstance(snap.effective_balance_usd, float)


class TestSprint62ReadModeIntegration(unittest.TestCase):
    """Sprint 62 — read_mode + write_mode still work the same."""

    def test_read_mode_returns_paper_when_override_disables_mandate(self):
        """The mode toggle (write_mode) and the read path must still
        work: override with mandate_enabled=False → mode == 'paper'."""
        audit_path = _empty_audit_file()
        try:
            write_mode(
                mandate_enabled=False,
                switched_by="test",
                audit_path=str(audit_path),
            )
            mode = read_mode(audit_path=str(audit_path))
            self.assertEqual(mode.mode, "paper")
            self.assertFalse(mode.mandate_enabled)
        finally:
            pass


if __name__ == "__main__":
    unittest.main()
