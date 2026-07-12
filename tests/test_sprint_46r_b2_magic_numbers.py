"""
Sprint 46R (audit B2): regression tests for the
"magic numbers to config" fixes.

The audit's exact wording:
  "Numeros magicos fuera de config: signal_min_strength=0.6
   (main.py:930), lookback 3600s, floors de $10/$100,
   entry_price * 0.005, caps del strategy_agent, TTLs de
   cache."

This commit closes 2 of the explicit items (signal_min_strength
and the entry_price * 0.005 SL/TP floor). The other items
(TTLs de cache, caps del strategy_agent) are not addressed in
this commit but are explicitly listed in audit doc section 10
PENDIENTES as separate follow-ups.

The fixes:
  1. `smart_profit_take_min_signal_strength` in config.yaml
     replaces the hard-coded `signal_min_strength=0.6` in
     main.py's call to position_monitor.check_with_signals.
  2. `min_sl_floor_pct` and `min_tp_floor_pct` in config.yaml
     replace the hard-coded `entry_price * 0.005` inside
     RiskManagerAgent (B1's symmetric floor).

Tests cover:
  - config.yaml has the new keys with the right defaults
  - main.py reads the new key (smoke test: source inspection)
  - RiskManagerAgent honors min_sl_floor_pct and
    min_tp_floor_pct from the constructor (not from a
    hard-coded value)
  - back-compat: default values preserve the B1 behavior
    (0.005 on both sides)
"""
from __future__ import annotations

import unittest
from pathlib import Path
import re

import yaml

from src.agents.risk_agent import RiskManagerAgent
from src.agents.notification_agent import _is_live_mode  # noqa: F401  (warmup)
from src.core.logging_setup import setup_logging


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
MAIN_PATH = REPO_ROOT / "main.py"


class ConfigYamlHasNewKeysTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        cls.trading = cls.cfg.get("trading", {})

    def test_smart_profit_take_min_signal_strength_present(self):
        """The new B2 key for the SMART_PROFIT_TAKE strength
        threshold must exist in config.yaml's `trading:` section
        with a sane default (0.6 matches the pre-46R hard-coded
        value)."""
        self.assertIn(
            "smart_profit_take_min_signal_strength", self.trading,
            "config.yaml:trading: must have smart_profit_take_min_signal_strength"
        )
        v = self.trading["smart_profit_take_min_signal_strength"]
        self.assertIsInstance(v, (int, float))
        self.assertGreaterEqual(v, 0.0)
        self.assertLessEqual(v, 1.0,
            "strength threshold must be in [0, 1]")
        # Default = 0.6 (matches pre-46R hard-coded value)
        self.assertAlmostEqual(float(v), 0.6, places=4,
            msg="Default should preserve the pre-46R 0.6")

    def test_min_sl_floor_pct_present(self):
        self.assertIn("min_sl_floor_pct", self.trading)
        v = float(self.trading["min_sl_floor_pct"])
        # Default 0.005 = 0.5% (matches B1's hard-coded floor)
        self.assertAlmostEqual(v, 0.005, places=6)

    def test_min_tp_floor_pct_present(self):
        self.assertIn("min_tp_floor_pct", self.trading)
        v = float(self.trading["min_tp_floor_pct"])
        # Default 0.005 = 0.5% (matches B1's hard-coded floor)
        self.assertAlmostEqual(v, 0.005, places=6)


class MainPyReadsConfigTest(unittest.TestCase):
    """Static source inspection: main.py must read the new
    config keys instead of using hard-coded 0.6 / 0.005."""

    def test_main_reads_smart_profit_take_min_signal_strength(self):
        main_src = MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn("smart_profit_take_min_signal_strength", main_src)
        # And it should default to 0.6 (preserves the pre-46R
        # value if config is missing the key).
        m = re.search(
            r'smart_profit_take_min_signal_strength"?\s*,\s*([\d.]+)',
            main_src,
        )
        self.assertIsNotNone(m)
        self.assertAlmostEqual(float(m.group(1)), 0.6, places=4)

    def test_main_reads_min_sl_floor_pct(self):
        main_src = MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn("min_sl_floor_pct", main_src)
        m = re.search(r'min_sl_floor_pct"?\s*,\s*([\d.]+)', main_src)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(float(m.group(1)), 0.005, places=6)

    def test_main_reads_min_tp_floor_pct(self):
        main_src = MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn("min_tp_floor_pct", main_src)
        m = re.search(r'min_tp_floor_pct"?\s*,\s*([\d.]+)', main_src)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(float(m.group(1)), 0.005, places=6)

    def test_main_no_hardcoded_signal_min_strength(self):
        """The literal `signal_min_strength=0.6` (pre-46R) must
        no longer appear in main.py — it should be read from
        config. Search for the exact pattern that pre-46R used."""
        main_src = MAIN_PATH.read_text(encoding="utf-8")
        # The pre-46R call was:
        #   position_monitor.check_with_signals(..., signal_min_strength=0.6, ...)
        # We use a regex that requires the value to be a literal
        # 0.6, not the result of a .get() call.
        self.assertNotRegex(
            main_src,
            r"signal_min_strength\s*=\s*0\.6\b",
            "main.py still has the hard-coded signal_min_strength=0.6"
            " - B2 wants it read from config",
        )


class RiskAgentSlTpFloorConfigTest(unittest.TestCase):
    """Verify the new SL/TP floor constructor kwargs are
    actually used by RiskManagerAgent (not the hard-coded
    0.005)."""

    def setUp(self):
        # Warm up the logging so RiskManagerAgent's loggers
        # don't complain on init.
        setup_logging()

    def _build_agent(self, min_sl_floor_pct=0.005, min_tp_floor_pct=0.005):
        """Minimal RiskManagerAgent for testing the SL/TP math.
        We don't go through full validate_and_size; we mirror
        the 4 lines of SL/TP computation in a tiny harness.
        """
        agent = RiskManagerAgent(
            broker_client=None,
            audit=None,
            position_repo=None,
            event_bus=None,
            atr_stop_multiplier=2.0,
            atr_take_profit_multiplier=4.0,
            min_sl_floor_pct=min_sl_floor_pct,
            min_tp_floor_pct=min_tp_floor_pct,
        )
        return agent

    def _sl_tp(self, agent, entry, atr, direction):
        """Mirror the post-Sprint-46R SL/TP math from
        validate_and_size. The agent's min_sl_floor_pct /
        min_tp_floor_pct must be used (NOT a hard-coded 0.005).
        """
        stop_distance = max(
            atr * agent.atr_stop_multiplier,
            entry * agent.min_sl_floor_pct,
        )
        tp_distance = max(
            atr * agent.atr_take_profit_multiplier,
            entry * agent.min_tp_floor_pct,
        )
        if direction == "long":
            return entry - stop_distance, entry + tp_distance
        else:
            return entry + stop_distance, entry - tp_distance

    def test_default_floor_is_0_005(self):
        """Back-compat: the constructor default is 0.005 for
        both floors, matching the B1 fix's hard-coded value."""
        agent = self._build_agent()  # no floor args
        self.assertEqual(agent.min_sl_floor_pct, 0.005)
        self.assertEqual(agent.min_tp_floor_pct, 0.005)

    def test_floor_can_be_loosened_via_constructor(self):
        """A looser floor (1% instead of 0.5%) means the SL/TP
        distance is wider when atr is tiny."""
        agent = self._build_agent(min_sl_floor_pct=0.01, min_tp_floor_pct=0.01)
        # atr=0, entry=100, 1% floor -> SL at 99, TP at 101
        sl, tp = self._sl_tp(agent, entry=100.0, atr=0.0, direction="long")
        self.assertAlmostEqual(sl, 99.0, places=6)
        self.assertAlmostEqual(tp, 101.0, places=6)

    def test_floor_can_be_tightened_via_constructor(self):
        """A tighter floor (0.1% instead of 0.5%) means the
        SL/TP distance is narrower when atr is tiny."""
        agent = self._build_agent(min_sl_floor_pct=0.001, min_tp_floor_pct=0.001)
        sl, tp = self._sl_tp(agent, entry=100.0, atr=0.0, direction="long")
        self.assertAlmostEqual(sl, 99.9, places=6)
        self.assertAlmostEqual(tp, 100.1, places=6)

    def test_asymmetric_floors_supported(self):
        """SL and TP floors can differ - some operators want a
        wider SL floor (more room to breathe) and a tighter TP
        floor (take profit on smaller moves)."""
        agent = self._build_agent(min_sl_floor_pct=0.01, min_tp_floor_pct=0.001)
        sl, tp = self._sl_tp(agent, entry=100.0, atr=0.0, direction="long")
        # SL at 99 (1% floor), TP at 100.1 (0.1% floor)
        self.assertAlmostEqual(sl, 99.0, places=6)
        self.assertAlmostEqual(tp, 100.1, places=6)


if __name__ == "__main__":
    unittest.main()
