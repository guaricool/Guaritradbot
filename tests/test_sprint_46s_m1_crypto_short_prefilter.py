"""
Sprint 46S (audit M1 follow-up) — suppress crypto "short" hypotheses
BEFORE the Debate Agent, instead of only rejecting them later in
RiskManagerAgent.validate_and_size.

Live audit evidence that motivated this (2026-07-12, dashboard Audit
feed, ~10:21 and ~11:21 cycles): every hourly cycle, BTC-USD produced
a Resistance_Fade SHORT (4h) and a MACD_BullCross LONG (1h). The
Debate Agent approved the (unexecutable) short and rejected the long
on its own merits -- net zero trades that hour even though nothing
was actually wrong with the bot's health or the market data. The
short was only ever rejected by RiskManagerAgent as
"crypto_short_not_supported" (binance.us spot has no margin/borrow),
which happens AFTER the debate stage has already spent its one shot
on the wrong candidate.

This suite covers StrategyAgent.evaluate_strategies' new filtering
block: crypto short hypotheses are dropped (with a HYPOTHESIS_SUPPRESSED
audit event) before being returned to the workflow, unless
allow_crypto_short=True is explicitly set (mirrors RiskManagerAgent's
flag of the same name).

Run: python -m unittest tests.test_sprint_46s_m1_crypto_short_prefilter -v
"""
import json
import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.strategy_agent import StrategyAgent
from src.safety.audit_ledger import AuditLedger


def _btc_macd_bearcross_df(n=31):
    """1h BTC-USD df with a strict MACD bear-cross on the last bar ->
    triggers a MACD_BearCross "short" hypothesis."""
    macd = [1.0] * (n - 1) + [-1.0]
    sig = [0.0] * n
    close = [50000.0] * n
    atr = [500.0] * n
    return pd.DataFrame({
        "Close": close,
        "MACD": macd,
        "MACD_Signal": sig,
        "ATR_14": atr,
    })


def _spy_rsi_overbought_df(n=21):
    """15m SPY df with a strict RSI overbought cross on the last bar ->
    triggers a MeanReversion_SHORT "short" hypothesis. SPY is
    EQUITY_GROWTH, not CRYPTO, so this must NOT be touched by the
    crypto-short prefilter (equity shorts are a separate, already-
    solved concern -- RiskManagerAgent's allow_equity_short, Sprint
    46N audit M3)."""
    rsi = [50.0] * (n - 1) + [75.0]
    rsi[-2] = 71.0  # prev also > 70 would break the strict-cross branch;
    rsi[-2] = 69.0  # prev must be <= 70 for the strict cross to fire
    close = [450.0] * n
    return pd.DataFrame({"Close": close, "RSI": rsi})


def _read_audit_events(audit_path, event_type):
    events = []
    if not os.path.exists(audit_path):
        return events
    with open(audit_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("event_type") == event_type:
                events.append(d)
    return events


class CryptoShortPrefilterDefaultOffTest(unittest.TestCase):
    """allow_crypto_short=False (the default) must drop crypto shorts
    before they're returned from evaluate_strategies."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_path = os.path.join(self.tmpdir, "audit.jsonl")
        self.audit = AuditLedger(self.audit_path)

    def test_btc_short_is_suppressed_by_default(self):
        agent = StrategyAgent(audit=self.audit)  # allow_crypto_short defaults False
        market_data = {"BTC-USD": {"1h": _btc_macd_bearcross_df()}}
        result = agent.evaluate_strategies(inputs={}, state={"analyze_market": {"market_data": market_data}})
        hyps = result["hypotheses"]
        shorts = [h for h in hyps if h["asset"] == "BTC-USD" and h["direction"] == "short"]
        self.assertEqual(shorts, [], "crypto short must be filtered out by default")

    def test_suppression_is_audited(self):
        agent = StrategyAgent(audit=self.audit)
        market_data = {"BTC-USD": {"1h": _btc_macd_bearcross_df()}}
        agent.evaluate_strategies(inputs={}, state={"analyze_market": {"market_data": market_data}})
        events = _read_audit_events(self.audit_path, "HYPOTHESIS_SUPPRESSED")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["asset"], "BTC-USD")
        self.assertEqual(events[0]["direction"], "short")
        self.assertEqual(events[0]["reason"], "crypto_short_not_supported")

    def test_no_hypothesis_generated_event_for_suppressed_short(self):
        """The suppressed short must not ALSO show up as a normal
        HYPOTHESIS_GENERATED event -- otherwise the audit feed would
        show it as a live candidate right before saying it was
        suppressed, which is exactly the confusing "approved then
        blocked" pattern this fix is trying to get rid of."""
        agent = StrategyAgent(audit=self.audit)
        market_data = {"BTC-USD": {"1h": _btc_macd_bearcross_df()}}
        agent.evaluate_strategies(inputs={}, state={"analyze_market": {"market_data": market_data}})
        generated = _read_audit_events(self.audit_path, "HYPOTHESIS_GENERATED")
        shorts_generated = [
            e for e in generated
            if e["payload"]["asset"] == "BTC-USD" and e["payload"]["direction"] == "short"
        ]
        self.assertEqual(shorts_generated, [])


class CryptoShortPrefilterOptInTest(unittest.TestCase):
    """allow_crypto_short=True must disable the filter entirely,
    mirroring RiskManagerAgent's own opt-in flag."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_path = os.path.join(self.tmpdir, "audit.jsonl")
        self.audit = AuditLedger(self.audit_path)

    def test_btc_short_kept_when_allowed(self):
        agent = StrategyAgent(audit=self.audit, allow_crypto_short=True)
        market_data = {"BTC-USD": {"1h": _btc_macd_bearcross_df()}}
        result = agent.evaluate_strategies(inputs={}, state={"analyze_market": {"market_data": market_data}})
        hyps = result["hypotheses"]
        shorts = [h for h in hyps if h["asset"] == "BTC-USD" and h["direction"] == "short"]
        self.assertEqual(len(shorts), 1)
        self.assertEqual(shorts[0]["strategy"], "MACD_BearCross")

    def test_no_suppression_event_when_allowed(self):
        agent = StrategyAgent(audit=self.audit, allow_crypto_short=True)
        market_data = {"BTC-USD": {"1h": _btc_macd_bearcross_df()}}
        agent.evaluate_strategies(inputs={}, state={"analyze_market": {"market_data": market_data}})
        events = _read_audit_events(self.audit_path, "HYPOTHESIS_SUPPRESSED")
        self.assertEqual(events, [])


class CryptoShortPrefilterEquityUnaffectedTest(unittest.TestCase):
    """The filter must be crypto-specific -- an equity short (SPY) is a
    separate, already-solved concern (RiskManagerAgent's
    allow_equity_short, Sprint 46N audit M3) and must NOT be touched
    here."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_path = os.path.join(self.tmpdir, "audit.jsonl")
        self.audit = AuditLedger(self.audit_path)

    def test_spy_short_untouched_by_crypto_filter(self):
        agent = StrategyAgent(audit=self.audit)  # allow_crypto_short=False (default)
        market_data = {"SPY": {"15m": _spy_rsi_overbought_df()}}
        result = agent.evaluate_strategies(inputs={}, state={"analyze_market": {"market_data": market_data}})
        hyps = result["hypotheses"]
        shorts = [h for h in hyps if h["asset"] == "SPY" and h["direction"] == "short"]
        self.assertEqual(len(shorts), 1, "equity shorts must not be dropped by the crypto-only prefilter")


class CryptoShortPrefilterLongUnaffectedTest(unittest.TestCase):
    """A crypto LONG hypothesis must never be touched by this filter,
    only "short" direction on a CRYPTO-class asset."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_path = os.path.join(self.tmpdir, "audit.jsonl")
        self.audit = AuditLedger(self.audit_path)

    def test_btc_long_kept_by_default(self):
        agent = StrategyAgent(audit=self.audit)
        n = 31
        macd = [-1.0] * (n - 1) + [1.0]  # strict bull cross on last bar
        sig = [0.0] * n
        df = pd.DataFrame({
            "Close": [50000.0] * n,
            "MACD": macd,
            "MACD_Signal": sig,
            "ATR_14": [500.0] * n,
        })
        market_data = {"BTC-USD": {"1h": df}}
        result = agent.evaluate_strategies(inputs={}, state={"analyze_market": {"market_data": market_data}})
        longs = [h for h in result["hypotheses"] if h["asset"] == "BTC-USD" and h["direction"] == "long"]
        self.assertEqual(len(longs), 1)
        self.assertEqual(longs[0]["strategy"], "MACD_BullCross")


if __name__ == "__main__":
    unittest.main()
