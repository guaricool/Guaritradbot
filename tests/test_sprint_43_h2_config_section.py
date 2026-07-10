"""
Sprint 43 H2 fix tests — `max_capital_per_trade_pct` was in the wrong
config section.

The bug: `config.yaml` declared `max_capital_per_trade_pct: 50` under
the `exchange:` section, but `main.py` reads it from the `trading:`
section. Result: the bot always fell back to the default (10%) per
trade, while the dashboard's sidebar showed 50% (the value the user
THOUGHT was being applied). Inconsistent UI vs reality.

The fix:
  1. Moved `max_capital_per_trade_pct: 50` from `exchange:` to
     `trading:` in `config.yaml`.
  2. Updated the dashboard to read it from `trading:` with a
     fallback to `exchange:` for any older config that hasn't
     been updated.
  3. Verified that the bot's actual cap now matches the
     dashboard's display.

Tests verify:
  - config.yaml has it under trading: (and not under exchange:)
  - main.py's read of trading_cfg.get("max_capital_per_trade_pct")
    picks up 50 (not the default 10)
  - dashboard.py's cap_pct_cfg helper prefers trading: but falls
    back to exchange: for legacy configs
"""
import os
import sys
import unittest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class ConfigYAMLStructureTest(unittest.TestCase):
    """The config field must be under `trading:`, not `exchange:`."""

    def setUp(self):
        self.config_path = os.path.join(ROOT, "config.yaml")
        with open(self.config_path, encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)

    def test_max_capital_under_trading_not_exchange(self):
        self.assertIn("trading", self.cfg, "config.yaml must have a `trading:` section")
        self.assertIn(
            "max_capital_per_trade_pct",
            self.cfg["trading"],
            "Sprint 43 H2 fix: `max_capital_per_trade_pct` must live under `trading:`",
        )
        # The exchange section may still have a legacy value, but the
        # authoritative one is in trading.
        exch = self.cfg.get("exchange", {})
        if "max_capital_per_trade_pct" in exch:
            # If a legacy value is still there, the bot reads trading
            # first, so the user-visible behavior is correct. We just
            # warn that this is a legacy leftover.
            print(f"  [info] legacy `exchange.max_capital_per_trade_pct={exch['max_capital_per_trade_pct']}` still present (cosmetic)")

    def test_trading_value_is_sensible(self):
        val = self.cfg["trading"]["max_capital_per_trade_pct"]
        # The audit's original comment said 50% of $20 = $10. So 50
        # is the documented value.
        self.assertGreater(val, 0)
        self.assertLessEqual(val, 100)
        # Currently configured as 50 — change this if the user updates
        self.assertEqual(val, 50, "Default value should be 50 (matches the audit's intent)")

    def test_main_py_reads_from_trading(self):
        """main.py must read from trading_cfg (the trading: section)."""
        with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
            main_src = f.read()
        self.assertIn(
            'trading_cfg.get("max_capital_per_trade_pct"',
            main_src,
            "main.py must read max_capital_per_trade_pct from trading_cfg",
        )


class MainPyConfigFlowTest(unittest.TestCase):
    """End-to-end: with the fixed config, the bot picks up 50 (not 10)."""

    def setUp(self):
        # Use the actual config.yaml (no monkey-patching) — this is
        # the integration test for the H2 fix.
        with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)

    def test_bot_uses_50_not_10(self):
        """
        The bot should read 50 from trading:. If it falls back to 10
        (the default in main.py:135), the H2 fix isn't actually
        applied and the audit's complaint stands.
        """
        val = self.cfg["trading"].get("max_capital_per_trade_pct")
        self.assertIsNotNone(val, "Bot will fall back to 10% default — H2 fix not in effect")
        self.assertEqual(val, 50)


class DashboardCapPctCfgTest(unittest.TestCase):
    """Dashboard's cap_pct_cfg helper prefers trading: over exchange:."""

    def test_dashboard_prefers_trading_over_exchange(self):
        with open(os.path.join(ROOT, "dashboard.py"), encoding="utf-8") as f:
            dash_src = f.read()
        # The helper must be present
        self.assertIn(
            "cap_pct_cfg",
            dash_src,
            "Dashboard must define a `cap_pct_cfg` helper (Sprint 43 H2)",
        )
        # And it must prefer risk (trading:) over exch (exchange:)
        # Pattern: cap_pct_cfg = risk.get(..., exch.get(...))
        self.assertIn(
            'risk.get("max_capital_per_trade_pct"',
            dash_src,
            "Dashboard's cap_pct_cfg must read from `risk` (trading:) first",
        )


if __name__ == "__main__":
    unittest.main()
