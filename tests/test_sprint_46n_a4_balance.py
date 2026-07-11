"""
Sprint 46N tests — audit finding A4 (AUDITORIA_COMPLETA_2026-07-11.md).

A4: `RiskManagerAgent.get_account_balance()` had a $100 "simulated
balance" fallback (both when no broker is configured at all, and when
a configured broker's balance fetch fails/returns non-finite) that
NEVER checked whether the bot was actually in LIVE mode
(mandate_enabled=true). A broker outage in LIVE mode would silently
size REAL orders against a fabricated $100 balance instead of refusing
to trade -- far worse than aborting the cycle. The fix: both fallback
paths now raise instead of simulating whenever
`is_mandate_enabled(mode_override_path)` is True; the $100 simulated
fallback remains available in PAPER mode only (unchanged behavior
there, still gated by GUARICO_ALLOW_SIMULATED_BALANCE for the
fetch-failure path).

A4 also covers per-asset-class balance lookup: previously ALL sizing
used `self.broker` (the crypto/binance.us client) regardless of the
hypothesis's asset, so an SPY trade was sized against the crypto
USDT balance instead of Alpaca's real USD buying power. Now
`get_account_balance(asset=...)` routes through the same
asset-class table used for closes (`broker_routing.py`): equity
assets resolve to `alpaca_broker.get_usd_balance()`, everything else
to `broker.get_usdt_balance()`.

Run: python -m unittest tests.test_sprint_46n_a4_balance -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.risk_agent import RiskManagerAgent

_BROKERS_CONFIG = {
    "crypto": {"symbols": ["BTC-USD", "ETH-USD"]},
    "equity": {"symbols": ["SPY", "QQQ"]},
}


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


class _FakeCryptoBroker:
    def __init__(self, balance=500.0):
        self._balance = balance

    def get_usdt_balance(self):
        return self._balance


class _FakeAlpacaBroker:
    def __init__(self, balance=2000.0):
        self._balance = balance

    def get_usd_balance(self):
        return self._balance


class _FakeBrokerRaises:
    def get_usdt_balance(self):
        raise Exception("network timeout")

    def get_usd_balance(self):
        raise Exception("network timeout")


class LiveModeNeverSimulatesTest(unittest.TestCase):
    """Neither fallback path (no-broker, fetch-failure) may return the
    $100 simulated balance while mandate_enabled=true."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_no_broker_raises_in_live_mode(self):
        override_path = _write_mode_override(self.tmpdir, mandate_enabled=True)
        agent = RiskManagerAgent(
            broker_client=None,
            mode_override_path=override_path,
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
        )
        with self.assertRaises(RuntimeError) as ctx:
            agent.get_account_balance()
        self.assertIn("LIVE", str(ctx.exception))

    def test_no_broker_still_simulates_in_paper_mode(self):
        override_path = _write_mode_override(self.tmpdir, mandate_enabled=False)
        agent = RiskManagerAgent(
            broker_client=None,
            mode_override_path=override_path,
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
        )
        bal, source = agent.get_account_balance()
        self.assertEqual(bal, 100.0)
        self.assertEqual(source, "no_broker_sim")

    def test_fetch_failure_raises_in_live_mode_even_with_env_var_on(self):
        """The old GUARICO_ALLOW_SIMULATED_BALANCE=1 dev-mode escape
        hatch must NOT apply once the bot is actually live."""
        override_path = _write_mode_override(self.tmpdir, mandate_enabled=True)
        with unittest.mock.patch.dict(os.environ, {"GUARICO_ALLOW_SIMULATED_BALANCE": "1"}):
            agent = RiskManagerAgent(
                broker_client=_FakeBrokerRaises(),
                mode_override_path=override_path,
                correlation_check_enabled=False,
                tail_risk_check_enabled=False,
            )
            with self.assertRaises(RuntimeError) as ctx:
                agent.get_account_balance()
            self.assertIn("LIVE", str(ctx.exception))

    def test_fetch_failure_still_simulates_in_paper_mode_with_env_var_on(self):
        override_path = _write_mode_override(self.tmpdir, mandate_enabled=False)
        with unittest.mock.patch.dict(os.environ, {"GUARICO_ALLOW_SIMULATED_BALANCE": "1"}):
            agent = RiskManagerAgent(
                broker_client=_FakeBrokerRaises(),
                mode_override_path=override_path,
                correlation_check_enabled=False,
                tail_risk_check_enabled=False,
            )
            bal, source = agent.get_account_balance()
            self.assertEqual(bal, 100.0)
            self.assertEqual(source, "testnet_sim")

    def test_equity_asset_no_alpaca_broker_raises_in_live_mode(self):
        """An equity hypothesis with no alpaca_broker configured must
        also refuse to simulate in live mode (not just the crypto
        no-broker case)."""
        override_path = _write_mode_override(self.tmpdir, mandate_enabled=True)
        agent = RiskManagerAgent(
            broker_client=_FakeCryptoBroker(),
            alpaca_broker=None,
            brokers_config=_BROKERS_CONFIG,
            mode_override_path=override_path,
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
        )
        with self.assertRaises(RuntimeError) as ctx:
            agent.get_account_balance(asset="SPY")
        self.assertIn("LIVE", str(ctx.exception))


class PerAssetClassBalanceTest(unittest.TestCase):
    """An equity hypothesis must size against Alpaca's balance, a
    crypto hypothesis against the crypto broker's balance -- not a
    single shared number."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.override_path = _write_mode_override(self.tmpdir, mandate_enabled=False)

    def _make_agent(self, crypto_balance=500.0, equity_balance=2000.0):
        return RiskManagerAgent(
            broker_client=_FakeCryptoBroker(balance=crypto_balance),
            alpaca_broker=_FakeAlpacaBroker(balance=equity_balance),
            brokers_config=_BROKERS_CONFIG,
            mode_override_path=self.override_path,
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
        )

    def test_crypto_asset_uses_crypto_broker_balance(self):
        agent = self._make_agent(crypto_balance=500.0, equity_balance=2000.0)
        bal, source = agent.get_account_balance(asset="BTC-USD")
        self.assertEqual(bal, 500.0)

    def test_equity_asset_uses_alpaca_balance_not_crypto(self):
        agent = self._make_agent(crypto_balance=500.0, equity_balance=2000.0)
        bal, source = agent.get_account_balance(asset="SPY")
        self.assertEqual(bal, 2000.0)

    def test_no_asset_defaults_to_crypto_balance_backward_compat(self):
        """Omitting `asset` must preserve the exact pre-46N behavior:
        always resolve to the crypto broker."""
        agent = self._make_agent(crypto_balance=500.0, equity_balance=2000.0)
        bal, source = agent.get_account_balance()
        self.assertEqual(bal, 500.0)

    def test_unmapped_asset_falls_back_to_crypto_balance(self):
        agent = self._make_agent(crypto_balance=500.0, equity_balance=2000.0)
        bal, source = agent.get_account_balance(asset="DOGE-USD")
        self.assertEqual(bal, 500.0)

    def test_validate_and_size_sizes_equity_and_crypto_from_different_balances(self):
        """End-to-end: a cycle with both an SPY and a BTC-USD
        hypothesis must size each against its own broker's balance."""
        from src.data_store.positions import PositionRepository

        repo = PositionRepository(path=os.path.join(self.tmpdir, "positions.json"))
        agent = self._make_agent(crypto_balance=1000.0, equity_balance=100000.0)
        agent.position_repo = repo
        agent.asset_concentration_check = False
        agent.portfolio_stress_check = False
        agent.block_conflicting_asset_positions = False

        state = {
            "generate_hypotheses": {
                "hypotheses": [
                    {
                        "asset": "BTC-USD", "direction": "long", "strategy": "test",
                        "price": 50000.0, "atr_at_signal": 500.0,
                    },
                    {
                        "asset": "SPY", "direction": "long", "strategy": "test",
                        "price": 500.0, "atr_at_signal": 5.0,
                    },
                ]
            }
        }
        result = agent.validate_and_size({}, state)
        approved_by_asset = {t["asset"]: t for t in result["approved_trades"]}
        self.assertIn("BTC-USD", approved_by_asset)
        self.assertIn("SPY", approved_by_asset)
        # 1% risk of $1000 (crypto) = $10 risk / stop_distance=$1000 (2x ATR 500) -> qty tiny;
        # 1% risk of $100000 (equity) = $1000 risk -- notional should reflect the
        # MUCH bigger equity balance, i.e. BTC notional should be far smaller
        # than SPY notional given these balances, proving they used different
        # balances rather than one shared number.
        self.assertLess(
            approved_by_asset["BTC-USD"]["notional_usd"],
            approved_by_asset["SPY"]["notional_usd"],
        )


if __name__ == "__main__":
    unittest.main()
