"""
Bug fix: RiskManagerAgent.get_account_balance() used the REAL broker
balance to size every trade even in PAPER mode -- since no real order
is ever sent in paper mode (the B033 gate), that's a completely
different number from the bot's own virtual paper balance (config.
paper.starting_balance_usd + realized P&L, the same number
EquityTracker computes and the dashboard shows as "Paper Balance
(Simulated)"). Carlos's screenshot showed the exact consequence: a GLD
position sized at $38,414 notional (qty=104.16 @ $368.79) while the
dashboard showed a ~$1000 virtual paper balance -- the trade was
actually sized against Alpaca's real $100,000 paper-sandbox account
balance.

Fixed via an optional `paper_balance_provider` callable, consulted
FIRST in paper mode (before ever touching a broker).

Run: python -m unittest tests.test_paper_balance_provider -v
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
from src.data.asset_allocation import AllocationPolicy


def _write_mode_override(tmpdir, mandate_enabled: bool) -> str:
    path = os.path.join(tmpdir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mandate_enabled": mandate_enabled}, f)
    return path


class PaperBalanceProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.alpaca = MagicMock()
        # The "wrong" number the bug used to size against -- a large,
        # untouched real paper-sandbox balance.
        self.alpaca.get_usd_balance.return_value = 100_000.0
        self.crypto = MagicMock()
        self.crypto.get_usdt_balance.return_value = 22.08

    def _make_agent(self, live: bool, paper_balance_provider=None):
        return RiskManagerAgent(
            broker_client=self.crypto,
            alpaca_broker=self.alpaca,
            brokers_config={"crypto": {"symbols": ["BTC-USD"]}, "equity": {"symbols": ["GLD"]}},
            mode_override_path=_write_mode_override(self.tmpdir, live),
            paper_balance_provider=paper_balance_provider,
        )

    def test_paper_mode_uses_provider_not_real_broker_balance(self):
        agent = self._make_agent(live=False, paper_balance_provider=lambda: 999.75)
        bal, source = agent.get_account_balance(asset="GLD")
        self.assertEqual(bal, 999.75)
        self.assertEqual(source, "paper_simulated")
        # Bug fix regression guard: the real broker must never be
        # touched for sizing while a provider is wired in paper mode.
        self.alpaca.get_usd_balance.assert_not_called()

    def test_paper_mode_without_provider_falls_back_to_broker_balance(self):
        """Back-compat: omitting the provider (the default) must
        preserve the exact pre-fix behavior for any caller that
        hasn't wired one up yet."""
        agent = self._make_agent(live=False, paper_balance_provider=None)
        bal, source = agent.get_account_balance(asset="GLD")
        self.assertEqual(bal, 100_000.0)
        self.alpaca.get_usd_balance.assert_called_once()

    def test_live_mode_ignores_provider_uses_real_broker_balance(self):
        """The provider must NEVER substitute for the real balance in
        LIVE mode -- real orders need real capital math."""
        agent = self._make_agent(live=True, paper_balance_provider=lambda: 999.75)
        bal, source = agent.get_account_balance(asset="GLD")
        self.assertEqual(bal, 100_000.0)
        self.alpaca.get_usd_balance.assert_called_once()

    def test_provider_used_for_crypto_asset_too(self):
        agent = self._make_agent(live=False, paper_balance_provider=lambda: 1042.31)
        bal, source = agent.get_account_balance(asset="BTC-USD")
        self.assertEqual(bal, 1042.31)
        self.crypto.get_usdt_balance.assert_not_called()

    def test_provider_used_when_no_asset_given(self):
        agent = self._make_agent(live=False, paper_balance_provider=lambda: 1042.31)
        bal, source = agent.get_account_balance()
        self.assertEqual(bal, 1042.31)

    def test_provider_raising_falls_back_to_broker(self):
        def _broken():
            raise RuntimeError("equity tracker not ready")
        agent = self._make_agent(live=False, paper_balance_provider=_broken)
        bal, source = agent.get_account_balance(asset="GLD")
        self.assertEqual(bal, 100_000.0)

    def test_provider_returning_non_finite_falls_back_to_broker(self):
        agent = self._make_agent(live=False, paper_balance_provider=lambda: float("nan"))
        bal, source = agent.get_account_balance(asset="GLD")
        self.assertEqual(bal, 100_000.0)

    def test_position_sizing_matches_dashboard_balance_not_broker_balance(self):
        """End-to-end: a GLD hypothesis sized with the provider wired
        must produce a notional consistent with the ~$1000 paper
        balance, not the $100k real Alpaca sandbox balance -- the
        exact discrepancy from Carlos's screenshot."""
        agent = self._make_agent(live=False, paper_balance_provider=lambda: 999.75)
        agent.risk_per_trade_pct = 1.0
        hypothesis = {
            "asset": "GLD", "strategy": "SUPPORT_BOUNCE", "direction": "long",
            "price": 368.79, "atr_at_signal": 4.8,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)
        approved = result["approved_trades"]
        self.assertEqual(len(approved), 1)
        notional = approved[0]["notional_usd"]
        # With a ~$1000 account, 1% risk, and a few-dollar ATR stop,
        # notional should be on the order of a few hundred dollars --
        # NOT tens of thousands (the $38,414 the bug produced against
        # a $100k account).
        self.assertLess(notional, 2000.0)

    def test_negative_paper_equity_clamps_to_zero_not_real_broker(self):
        """Carlos: 'el sistema no puede meter en una entrada mas que lo
        que esta en el balance' -- a deeply underwater paper account
        (e.g. a stale oversized position from before this sizing fix
        existed) must NOT fall through to the real Alpaca/binance
        balance for the next trade. That would repeat the exact bug
        that caused the negative equity: sizing new paper entries
        against real broker money."""
        agent = self._make_agent(live=False, paper_balance_provider=lambda: -37414.57)
        bal, source = agent.get_account_balance(asset="GLD")
        self.assertEqual(bal, 0.0)
        self.assertEqual(source, "paper_simulated_depleted")
        self.alpaca.get_usd_balance.assert_not_called()

    def test_negative_paper_equity_blocks_new_entries_end_to_end(self):
        agent = self._make_agent(live=False, paper_balance_provider=lambda: -37414.57)
        agent.risk_per_trade_pct = 1.0
        hypothesis = {
            "asset": "GLD", "strategy": "SUPPORT_BOUNCE", "direction": "long",
            "price": 368.79, "atr_at_signal": 4.8,
        }
        state = {"generate_hypotheses": {"hypotheses": [hypothesis]}}
        result = agent.validate_and_size({}, state)
        self.assertEqual(result["approved_trades"], [])
        self.alpaca.get_usd_balance.assert_not_called()


class TwoTradesExceedBalanceInSameCycleTest(unittest.TestCase):
    """Bug fix: real production incident -- GLD ($800.83) and SPY
    ($801.30) each independently passed the per-trade cap
    (max_capital_per_trade_pct=80% of $1000) in the SAME cycle, and
    together left the paper account at -$601.31 (screenshot: "Paper
    Balance Available -$601.31"). Nothing checked their SUM against
    the account's actual available cash -- the (optional, disabled by
    default) MandateGate is the only component with a total-exposure
    cap. This is a basic solvency check that must run regardless of
    whether MandateGate is configured."""

    def setUp(self):
        from src.data_store.positions import PositionRepository
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.alpaca = MagicMock()
        self.alpaca.get_usd_balance.return_value = 100_000.0  # real sandbox, must be ignored
        mode_path = _write_mode_override(self.tmpdir, False)
        self.agent = RiskManagerAgent(
            alpaca_broker=self.alpaca,
            brokers_config={"equity": {"symbols": ["GLD", "SPY"]}},
            mode_override_path=mode_path,
            paper_balance_provider=lambda: 1000.0,
            position_repo=self.repo,
            risk_per_trade_pct=1.0,
            max_capital_per_trade_pct=80.0,
            min_order_usd=10.0,
            asset_concentration_check=False,
            allocation_policy=AllocationPolicy(enabled=False),
            correlation_check_enabled=False,
            tail_risk_check_enabled=False,
            portfolio_stress_check=False,
        )

    def test_second_trade_rejected_for_insufficient_cash(self):
        hyps = [
            {"asset": "GLD", "strategy": "SUPPORT_BOUNCE", "direction": "long",
             "price": 368.14, "atr_at_signal": 4.39},
            {"asset": "SPY", "strategy": "MEANREVERSION", "direction": "long",
             "price": 744.12, "atr_at_signal": 2.85},
        ]
        state = {"generate_hypotheses": {"hypotheses": hyps}}
        result = self.agent.validate_and_size({}, state)
        approved = result["approved_trades"]
        rejected = result["rejected_trades"]

        self.assertEqual(len(approved), 1, "Only one of the two ~$800 trades should fit in $1000")
        total_notional = sum(t["notional_with_fees_usd"] for t in approved)
        self.assertLessEqual(total_notional, 1000.0, "Approved trades must never exceed the paper balance")

        reasons = [r["reason"] for r in rejected]
        self.assertIn("insufficient_available_cash", reasons)


if __name__ == "__main__":
    unittest.main()
