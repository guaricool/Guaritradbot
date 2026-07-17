"""
Carlos: "configura todo de manera que en paper la configuracion sea
agresiva, y cuando cambies a live tome los valores regulares
recomendados." RiskManagerAgent and StrategyAgent now each accept an
optional aggressive-profile override that's ONLY active while the bot
is in paper mode (mandate_enabled=false), checked fresh on every
validate_and_size()/evaluate_strategies() call so toggling paper/live
from the dashboard switches profiles immediately, no restart needed.

Run: python -m unittest tests.test_paper_vs_live_profiles -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.risk_agent import RiskManagerAgent
from src.agents.strategy_agent import StrategyAgent, DEFAULT_STRATEGY_PARAMS
from src.safety.audit_ledger import AuditLedger
from src.data_store.positions import PositionRepository


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


AGGRESSIVE_TRADING = {
    "risk_per_trade_pct": 2.0,
    "max_capital_per_trade_pct": 80,
    "atr_stop_multiplier": 1.5,
    "atr_take_profit_multiplier": 3.0,
    "max_open_trades": 8,
}

AGGRESSIVE_STRATEGY = {
    "rsi_oversold": 35,
    "rsi_overbought": 65,
    "adx_trend_min": 10,
}


class RiskManagerAgentProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.audit = AuditLedger(os.path.join(self.tmpdir, "audit.jsonl"))

    def _make_agent(self, live: bool):
        return RiskManagerAgent(
            broker_client=None,
            risk_per_trade_pct=1.0,
            max_capital_per_trade_pct=50,
            atr_stop_multiplier=2.0,
            atr_take_profit_multiplier=4.0,
            max_open_trades=5,
            mode_override_path=_write_mode_override(self.tmpdir, live),
            paper_overrides=AGGRESSIVE_TRADING,
            audit=self.audit,
            position_repo=self.repo,
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
        )

    def test_paper_mode_adopts_aggressive_profile(self):
        agent = self._make_agent(live=False)
        agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.risk_per_trade_pct, 2.0)
        self.assertEqual(agent.max_capital_per_trade_pct, 80)
        self.assertEqual(agent.atr_stop_multiplier, 1.5)
        self.assertEqual(agent.atr_take_profit_multiplier, 3.0)
        self.assertEqual(agent.max_open_trades, 8)

    def test_live_mode_keeps_recommended_profile(self):
        agent = self._make_agent(live=True)
        agent.broker = MagicMock()
        agent.broker.get_usdt_balance.return_value = 1000.0
        agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.risk_per_trade_pct, 1.0)
        self.assertEqual(agent.max_capital_per_trade_pct, 50)
        self.assertEqual(agent.atr_stop_multiplier, 2.0)
        self.assertEqual(agent.atr_take_profit_multiplier, 4.0)
        self.assertEqual(agent.max_open_trades, 5)

    def test_switching_mode_between_calls_switches_profile_immediately(self):
        """No restart needed -- toggling the dashboard mode switch must
        change the active profile on the very next cycle."""
        mode_path = _write_mode_override(self.tmpdir, False)
        agent = RiskManagerAgent(
            broker_client=None, risk_per_trade_pct=1.0, max_capital_per_trade_pct=50,
            atr_stop_multiplier=2.0, atr_take_profit_multiplier=4.0, max_open_trades=5,
            mode_override_path=mode_path, paper_overrides=AGGRESSIVE_TRADING,
            audit=self.audit, position_repo=self.repo,
            correlation_check_enabled=False, tail_risk_check_enabled=False,
        )
        agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.risk_per_trade_pct, 2.0)  # paper -> aggressive

        with open(mode_path, "w", encoding="utf-8") as f:
            json.dump({"mandate_enabled": True}, f)
        agent.broker = MagicMock()
        agent.broker.get_usdt_balance.return_value = 1000.0
        agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.risk_per_trade_pct, 1.0)  # now live -> recommended

    def test_no_overrides_configured_behaves_exactly_like_before(self):
        """Back-compat: omitting paper_overrides (the default) must
        never change behavior for any existing caller."""
        agent = RiskManagerAgent(
            broker_client=None, risk_per_trade_pct=1.0, max_capital_per_trade_pct=50,
            atr_stop_multiplier=2.0, atr_take_profit_multiplier=4.0, max_open_trades=5,
            mode_override_path=_write_mode_override(self.tmpdir, False),
            audit=self.audit, position_repo=self.repo,
            correlation_check_enabled=False, tail_risk_check_enabled=False,
        )
        agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.risk_per_trade_pct, 1.0)
        self.assertEqual(agent.max_open_trades, 5)

    def test_partial_override_only_changes_listed_keys(self):
        agent = RiskManagerAgent(
            broker_client=None, risk_per_trade_pct=1.0, max_capital_per_trade_pct=50,
            atr_stop_multiplier=2.0, atr_take_profit_multiplier=4.0, max_open_trades=5,
            mode_override_path=_write_mode_override(self.tmpdir, False),
            paper_overrides={"risk_per_trade_pct": 2.0},  # only this one key
            audit=self.audit, position_repo=self.repo,
            correlation_check_enabled=False, tail_risk_check_enabled=False,
        )
        agent.validate_and_size({}, {"generate_hypotheses": {"hypotheses": []}})
        self.assertEqual(agent.risk_per_trade_pct, 2.0)
        self.assertEqual(agent.max_capital_per_trade_pct, 50)  # unchanged
        self.assertEqual(agent.max_open_trades, 5)  # unchanged


class StrategyAgentProfileTest(unittest.TestCase):
    def _make_agent(self, tmpdir, live: bool, overrides=None):
        return StrategyAgent(
            mode_override_path=_write_mode_override(tmpdir, live),
            paper_params_overrides=overrides,
        )

    def _run(self, agent):
        agent.evaluate_strategies({}, {"analyze_market": {"market_data": {}}})

    def test_paper_mode_adopts_aggressive_params(self):
        tmpdir = tempfile.mkdtemp()
        agent = self._make_agent(tmpdir, live=False, overrides=AGGRESSIVE_STRATEGY)
        self._run(agent)
        self.assertEqual(agent.params["rsi_oversold"], 35)
        self.assertEqual(agent.params["rsi_overbought"], 65)
        self.assertEqual(agent.params["adx_trend_min"], 10)
        # Untouched keys keep the live/default value.
        self.assertEqual(agent.params["stoch_oversold"], DEFAULT_STRATEGY_PARAMS["stoch_oversold"])

    def test_live_mode_keeps_recommended_params(self):
        tmpdir = tempfile.mkdtemp()
        agent = self._make_agent(tmpdir, live=True, overrides=AGGRESSIVE_STRATEGY)
        self._run(agent)
        self.assertEqual(agent.params["rsi_oversold"], DEFAULT_STRATEGY_PARAMS["rsi_oversold"])
        self.assertEqual(agent.params["adx_trend_min"], DEFAULT_STRATEGY_PARAMS["adx_trend_min"])

    def test_switching_mode_switches_params_immediately(self):
        tmpdir = tempfile.mkdtemp()
        mode_path = os.path.join(tmpdir, "mode_override.json")
        with open(mode_path, "w", encoding="utf-8") as f:
            json.dump({"mandate_enabled": False}, f)
        agent = StrategyAgent(mode_override_path=mode_path, paper_params_overrides=AGGRESSIVE_STRATEGY)
        self._run(agent)
        self.assertEqual(agent.params["rsi_oversold"], 35)

        with open(mode_path, "w", encoding="utf-8") as f:
            json.dump({"mandate_enabled": True}, f)
        self._run(agent)
        self.assertEqual(agent.params["rsi_oversold"], DEFAULT_STRATEGY_PARAMS["rsi_oversold"])

    def test_no_overrides_behaves_exactly_like_before(self):
        tmpdir = tempfile.mkdtemp()
        agent = self._make_agent(tmpdir, live=False, overrides=None)
        self._run(agent)
        self.assertEqual(agent.params, DEFAULT_STRATEGY_PARAMS)

    def test_explicit_strategy_params_still_respected_as_the_live_base(self):
        """A caller-supplied strategy_params dict (not the module
        default) must be the LIVE baseline paper overrides layer on
        top of -- not silently replaced by DEFAULT_STRATEGY_PARAMS."""
        tmpdir = tempfile.mkdtemp()
        custom_base = {**DEFAULT_STRATEGY_PARAMS, "adx_trend_min": 25}
        agent = StrategyAgent(
            strategy_params=custom_base,
            mode_override_path=_write_mode_override(tmpdir, True),
            paper_params_overrides=AGGRESSIVE_STRATEGY,
        )
        self._run(agent)
        self.assertEqual(agent.params["adx_trend_min"], 25)  # live keeps custom base


if __name__ == "__main__":
    unittest.main()
