"""
Tests for MacroAnalyst (shadow-mode macro/geopolitical event scan).
See src/agents/macro_analyst.py's module docstring for the design
rationale: reinforcement-only (never a standalone entry generator),
shadow-mode first, deterministic RSS/regex (no LLM cost).

Run: python -m unittest tests.test_macro_analyst -v
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agents.macro_analyst import (
    MacroAnalyst,
    _detect_events,
    _bias_from_events,
    _is_negated,
)


class DetectEventsTest(unittest.TestCase):
    def test_fed_rate_cut_detected(self):
        events = _detect_events(["Fed cuts interest rates by 50 basis points"])
        self.assertEqual(events.get("fed_rate_cut"), 1)

    def test_fed_rate_hike_detected(self):
        events = _detect_events(["Fed hikes rates to combat inflation"])
        self.assertEqual(events.get("fed_rate_hike"), 1)

    def test_inflation_hot_detected(self):
        events = _detect_events(["CPI inflation surges above expectations"])
        self.assertEqual(events.get("inflation_hot"), 1)

    def test_inflation_cool_detected(self):
        events = _detect_events(["CPI inflation cools more than expected"])
        self.assertEqual(events.get("inflation_cool"), 1)

    def test_recession_signal_detected(self):
        events = _detect_events(["Economists warn of recession risk"])
        self.assertEqual(events.get("recession_signal"), 1)

    def test_geopolitical_crisis_detected(self):
        events = _detect_events(["War fears escalate as tensions rise"])
        self.assertEqual(events.get("geopolitical_crisis"), 1)

    def test_banking_crisis_detected(self):
        events = _detect_events(["Regional bank collapse sparks contagion fears"])
        self.assertEqual(events.get("banking_crisis"), 1)

    def test_no_event_in_neutral_headline(self):
        events = _detect_events(["Company announces new product line"])
        self.assertEqual(events, {})

    def test_multiple_headlines_accumulate_counts(self):
        events = _detect_events([
            "Fed cuts rates",
            "Fed cuts rates again",
            "War tensions escalate",
        ])
        self.assertEqual(events.get("fed_rate_cut"), 2)
        self.assertEqual(events.get("geopolitical_crisis"), 1)

    def test_empty_and_none_headlines_skipped(self):
        events = _detect_events(["", None, "Fed cuts rates"])
        self.assertEqual(events.get("fed_rate_cut"), 1)
        self.assertEqual(len(events), 1)


class NegationGuardTest(unittest.TestCase):
    """Known limitation, mitigated not eliminated: proximity regex
    can't truly parse negation. A nearby negation marker suppresses
    the match instead of confidently misreading it."""

    def test_refuses_to_cut_is_not_counted_as_a_cut(self):
        events = _detect_events(["Fed refuses to cut rates, cites persistent inflation"])
        self.assertNotIn("fed_rate_cut", events)

    def test_will_not_cut_is_not_counted_as_a_cut(self):
        events = _detect_events(["Powell says Fed will not cut rates this year"])
        self.assertNotIn("fed_rate_cut", events)

    def test_rules_out_hike_is_not_counted(self):
        events = _detect_events(["Fed rules out a rate hike for now"])
        self.assertNotIn("fed_rate_hike", events)

    def test_genuine_cut_still_detected_without_negation_nearby(self):
        events = _detect_events(["Fed cuts interest rates by 50 basis points"])
        self.assertIn("fed_rate_cut", events)

    def test_is_negated_false_when_no_negation_word_present(self):
        headline = "Fed cuts rates sharply"
        match_start = headline.index("cuts")
        self.assertFalse(_is_negated(headline, match_start))


class BiasFromEventsTest(unittest.TestCase):
    def test_no_events_returns_zero_bias_for_all_classes(self):
        bias = _bias_from_events({})
        self.assertEqual(bias, {"crypto": 0.0, "equity": 0.0, "commodity": 0.0})

    def test_rate_cut_is_bullish_for_all_three_classes(self):
        bias = _bias_from_events({"fed_rate_cut": 1})
        self.assertEqual(bias["crypto"], 1.0)
        self.assertEqual(bias["equity"], 1.0)
        self.assertEqual(bias["commodity"], 1.0)

    def test_rate_hike_is_bearish_for_all_three_classes(self):
        bias = _bias_from_events({"fed_rate_hike": 1})
        self.assertEqual(bias["crypto"], -1.0)
        self.assertEqual(bias["equity"], -1.0)
        self.assertEqual(bias["commodity"], -1.0)

    def test_recession_is_bearish_risk_bullish_gold(self):
        bias = _bias_from_events({"recession_signal": 1})
        self.assertEqual(bias["crypto"], -1.0)
        self.assertEqual(bias["equity"], -1.0)
        self.assertEqual(bias["commodity"], 1.0)

    def test_conflicting_events_partially_cancel(self):
        # One cut (bullish +1) and one hike (bearish -1) mentioned
        # equally often -> net zero.
        bias = _bias_from_events({"fed_rate_cut": 1, "fed_rate_hike": 1})
        self.assertAlmostEqual(bias["crypto"], 0.0, places=6)

    def test_bias_stays_within_bounds(self):
        bias = _bias_from_events({"fed_rate_cut": 5, "inflation_hot": 3})
        for v in bias.values():
            self.assertGreaterEqual(v, -1.0)
            self.assertLessEqual(v, 1.0)


class MacroAnalystShadowModeTest(unittest.TestCase):
    """The agent-level contract: scans, computes bias, logs a SHADOW
    audit event, and returns a result -- but never claims to be
    anything other than shadow-only."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = MagicMock()
        self.audit_events = []
        self.audit.append.side_effect = lambda et, p: self.audit_events.append((et, p))

    def test_scan_logs_MACRO_SIGNAL_SHADOW_event(self):
        fetcher = MagicMock(return_value=["Fed cuts interest rates by 50 basis points"])
        analyst = MacroAnalyst(
            cache_path=os.path.join(self.tmpdir, "cache.jsonl"),
            rss_fetcher=fetcher,
            audit=self.audit,
        )
        result = analyst.scan_macro(use_cache=False)
        self.assertEqual(result["bias"]["crypto"], 1.0)
        shadow_events = [e for e in self.audit_events if e[0] == "MACRO_SIGNAL_SHADOW"]
        self.assertEqual(len(shadow_events), 1)
        self.assertTrue(shadow_events[0][1]["shadow_only"])

    def test_workflow_call_shape_is_accepted(self):
        """Workflow engine calls every step as action(inputs=<dict>,
        state=<dict>) -- must not raise TypeError like the production
        bug documented in NewsAnalyst's docstring."""
        fetcher = MagicMock(return_value=[])
        analyst = MacroAnalyst(
            cache_path=os.path.join(self.tmpdir, "cache.jsonl"),
            rss_fetcher=fetcher,
            audit=self.audit,
        )
        result = analyst.scan_macro(inputs={}, state={"some": "state"})
        self.assertIn("bias", result)

    def test_rss_fetch_failure_is_fail_open(self):
        fetcher = MagicMock(side_effect=RuntimeError("network down"))
        analyst = MacroAnalyst(
            cache_path=os.path.join(self.tmpdir, "cache.jsonl"),
            rss_fetcher=fetcher,
            audit=self.audit,
        )
        result = analyst.scan_macro(use_cache=False)
        self.assertEqual(result["bias"], {"crypto": 0.0, "equity": 0.0, "commodity": 0.0})
        self.assertEqual(result["headline_count"], 0)

    def test_cache_hit_avoids_refetch(self):
        fetcher = MagicMock(return_value=["Fed cuts interest rates"])
        analyst = MacroAnalyst(
            cache_path=os.path.join(self.tmpdir, "cache.jsonl"),
            rss_fetcher=fetcher,
            audit=self.audit,
        )
        first = analyst.scan_macro(use_cache=True)
        self.assertFalse(first["from_cache"])
        second = analyst.scan_macro(use_cache=True)
        self.assertTrue(second["from_cache"])
        self.assertEqual(fetcher.call_count, len(__import__(
            "src.agents.macro_analyst", fromlist=["MACRO_PROXY_TICKERS"]
        ).MACRO_PROXY_TICKERS))

    def test_no_audit_configured_does_not_raise(self):
        fetcher = MagicMock(return_value=["Fed cuts interest rates"])
        analyst = MacroAnalyst(
            cache_path=os.path.join(self.tmpdir, "cache.jsonl"),
            rss_fetcher=fetcher,
            audit=None,
        )
        result = analyst.scan_macro(use_cache=False)
        self.assertIn("bias", result)


if __name__ == "__main__":
    unittest.main()
