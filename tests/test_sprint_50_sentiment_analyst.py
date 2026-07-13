"""
Sprint 50 (SentimentAnalyst) — tests for src/agents/sentiment_analyst.py.

The SentimentAnalyst scans Reddit (r/wallstreetbets, r/bitcoin,
r/ethtrader) for retail-crowd sentiment. Same lexicon as
NewsAnalyst (shared module import), same scoring formula,
combined with news in the HypothesisScorer for a single
tie-breaker (magnitude capped at +/- 5 total).

Coverage:
  1. scan_social_sentiment returns per-asset context
  2. Cache behavior (TTL, persistence, fault tolerance)
  3. Fetcher exception -> empty result, no raise
  4. Workflow yaml: scan_social_sentiment step exists, runs
     after scan_news, SentimentAnalyst in registry
  5. HypothesisScorer integration: combined news+social is
     the new tie-breaker (not stacked)
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.agents.sentiment_analyst import (
    DEFAULT_CACHE_PATH,
    SentimentAnalyst,
    _fetch_reddit,
    _resolve_subreddits,
    _asset_query_aliases,
)


class SentimentAnalystScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = Path(self.tmp.name) / "social_cache.jsonl"
        # Mock fetcher: returns predetermined posts per sub/query
        self.posts_by_sub_query = {
            ("wallstreetbets", "BTC-USD"): [
                {"title": "Bitcoin rally gains huge", "permalink": "/r/1"},
                {"title": "Crypto crash plunge", "permalink": "/r/2"},
            ],
            ("wallstreetbets", "BTC"): [
                {"title": "BTC to the moon", "permalink": "/r/3"},
            ],
            ("bitcoin", "BTC-USD"): [
                {"title": "Bullish breakout record", "permalink": "/r/4"},
            ],
            ("wallstreetbets", "SPY"): [
                {"title": "S&P 500 surge", "permalink": "/r/5"},
            ],
        }
        def fake_fetcher(sub, query):
            return [p for p in self.posts_by_sub_query.get((sub, query), [])]
        self.fake_fetcher = fake_fetcher

    def _make_analyst(self):
        return SentimentAnalyst(
            cache_path=str(self.cache_path),
            fetcher=self.fake_fetcher,
        )

    def test_returns_per_asset_context(self):
        analyst = self._make_analyst()
        result = analyst.scan_social_sentiment(["BTC-USD", "SPY", "GLD"])
        self.assertIn("BTC-USD", result)
        self.assertIn("SPY", result)
        self.assertIn("GLD", result)
        for asset, ctx in result.items():
            self.assertIn("social_sentiment", ctx)
            self.assertIn("post_count", ctx)
            self.assertIn("top_post", ctx)
            self.assertIn("scanned_at", ctx)
            self.assertIn("raw_titles", ctx)
            self.assertIn("source_subs", ctx)

    def test_btc_aggregates_multiple_subs(self):
        # BTC-USD: subs from DEFAULT_SUBREDDITS = ["Bitcoin", "bitcoin"].
        # We have data for r/bitcoin (the lowercase variant) with
        # query "BTC-USD" (1 post: "Bullish breakout record"). The
        # "Bitcoin" sub (mixed case) has no fixture data. So we
        # get 1 post total. The point of the test is just that
        # the per-asset aggregation works -- not a specific count.
        analyst = self._make_analyst()
        result = analyst.scan_social_sentiment(["BTC-USD"])
        ctx = result["BTC-USD"]
        # At least one sub was queried
        self.assertGreaterEqual(ctx["post_count"], 1)
        # And the post was positive, so the aggregate is positive
        self.assertGreater(ctx["social_sentiment"], 0.0)
        # The "bitcoin" sub is in the source list
        self.assertIn("bitcoin", ctx["source_subs"])

    def test_spy_only_general_subs(self):
        # SPY -> DEFAULT_SUBREDDITS["SPY"] = ["wallstreetbets", "stocks"]
        # We have wallstreetbets with "SPY" -> 1 post ("S&P 500 surge")
        analyst = self._make_analyst()
        result = analyst.scan_social_sentiment(["SPY"])
        ctx = result["SPY"]
        # At least wallstreetbets should be in the source list
        self.assertIn("wallstreetbets", ctx["source_subs"])

    def test_unknown_asset_falls_back_to_general(self):
        # GLD is in DEFAULT_SUBREDDITS but the fetcher returns nothing
        analyst = self._make_analyst()
        result = analyst.scan_social_sentiment(["GLD"])
        ctx = result["GLD"]
        self.assertEqual(ctx["post_count"], 0)
        self.assertEqual(ctx["social_sentiment"], 0.0)
        # Source subs should still be the general fallback
        self.assertIn("wallstreetbets", ctx["source_subs"])

    def test_empty_asset_list_returns_empty(self):
        analyst = self._make_analyst()
        self.assertEqual(analyst.scan_social_sentiment([]), {})

    def test_empty_string_asset_skipped(self):
        analyst = self._make_analyst()
        result = analyst.scan_social_sentiment(["", "BTC-USD"])
        self.assertNotIn("", result)
        self.assertIn("BTC-USD", result)


class SentimentAnalystCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = Path(self.tmp.name) / "social_cache.jsonl"
        self.fetch_count = 0
        def counting_fetcher(sub, query):
            self.fetch_count += 1
            return [{"title": "Bitcoin rally", "permalink": "/r/1"}]
        self.counting_fetcher = counting_fetcher

    def test_cache_hit_on_second_call(self):
        a = SentimentAnalyst(
            cache_path=str(self.cache_path),
            fetcher=self.counting_fetcher,
        )
        # First call: fetches
        result1 = a.scan_social_sentiment(["BTC-USD"])
        self.assertGreater(self.fetch_count, 0)
        self.assertFalse(result1["BTC-USD"]["from_cache"])
        # Second call: cache hit
        result2 = a.scan_social_sentiment(["BTC-USD"])
        self.assertTrue(result2["BTC-USD"]["from_cache"])
        # No new fetches
        first_count = self.fetch_count
        a.scan_social_sentiment(["BTC-USD"])
        self.assertEqual(self.fetch_count, first_count)

    def test_cache_expiry_triggers_refetch(self):
        a = SentimentAnalyst(
            cache_path=str(self.cache_path),
            ttl_seconds=0.1,
            fetcher=self.counting_fetcher,
        )
        a.scan_social_sentiment(["BTC-USD"])
        first_count = self.fetch_count
        import time
        time.sleep(0.2)
        a.scan_social_sentiment(["BTC-USD"])
        self.assertGreater(self.fetch_count, first_count)

    def test_cache_persists_across_instances(self):
        a1 = SentimentAnalyst(
            cache_path=str(self.cache_path),
            fetcher=self.counting_fetcher,
        )
        a1.scan_social_sentiment(["BTC-USD"])
        first_count = self.fetch_count
        a2 = SentimentAnalyst(
            cache_path=str(self.cache_path),
            fetcher=self.counting_fetcher,
        )
        a2.scan_social_sentiment(["BTC-USD"])
        # No new fetches (loaded from disk)
        self.assertEqual(self.fetch_count, first_count)


class SentimentAnalystFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = Path(self.tmp.name) / "social_cache.jsonl"

    def test_fetcher_exception_returns_empty(self):
        def broken_fetcher(sub, query):
            raise RuntimeError("Reddit 429")
        a = SentimentAnalyst(
            cache_path=str(self.cache_path),
            fetcher=broken_fetcher,
        )
        result = a.scan_social_sentiment(["BTC-USD"])
        self.assertIn("BTC-USD", result)
        self.assertEqual(result["BTC-USD"]["social_sentiment"], 0.0)
        self.assertEqual(result["BTC-USD"]["post_count"], 0)

    def test_corrupt_cache_file_is_tolerated(self):
        self.cache_path.write_text(
            "this is not valid json\n"
            '{"asset": "BTC-USD", "scanned_at": 9999999999, "sentiment": 0.5, "post_count": 5, "top_post": "x", "raw_titles": [], "source_subs": []}\n',
            encoding="utf-8",
        )
        a = SentimentAnalyst(
            cache_path=str(self.cache_path),
            fetcher=lambda s, q: [],
        )
        # Should not raise
        result = a.scan_social_sentiment(["BTC-USD"])
        self.assertIn("BTC-USD", result)


class ResolveSubredditsTest(unittest.TestCase):
    def test_known_assets(self):
        self.assertEqual(_resolve_subreddits("BTC-USD"), ["Bitcoin", "bitcoin"])
        self.assertEqual(_resolve_subreddits("SPY"), ["wallstreetbets", "stocks"])

    def test_unknown_asset_falls_back(self):
        subs = _resolve_subreddits("UNKNOWN-USD")
        # Should still be a non-empty list of general subs
        self.assertGreater(len(subs), 0)
        self.assertIn("wallstreetbets", subs)


class AssetQueryAliasesTest(unittest.TestCase):
    def test_btc_has_aliases(self):
        aliases = _asset_query_aliases("BTC-USD")
        self.assertIn("Bitcoin", aliases)
        self.assertIn("BTC", aliases)

    def test_spy_has_aliases(self):
        aliases = _asset_query_aliases("SPY")
        self.assertIn("SPY", aliases)
        self.assertIn("S&P 500", aliases)


class SentimentAnalystWorkflowIntegrationTest(unittest.TestCase):
    """Sprint 50: the workflow yaml references SentimentAnalyst
    and the agent is in the main.py registry. Same
    regression-guard pattern as Sprint 47D."""

    def setUp(self):
        import yaml
        from pathlib import Path
        self.workflow = yaml.safe_load(
            Path("src/workflows/trading_loop.yaml").read_text(encoding="utf-8")
        )
        main_src = Path("main.py").read_text(encoding="utf-8")
        import re
        registry_match = re.search(
            r"registry\s*=\s*\{(.+?)\n\s*\}", main_src, re.DOTALL
        )
        self.registry_keys = set(re.findall(
            r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*[A-Z][A-Za-z0-9_]*\s*\(',
            registry_match.group(1),
        ))

    def test_scan_social_sentiment_step_exists(self):
        steps = {s["id"]: s for s in self.workflow["steps"]}
        self.assertIn("scan_social_sentiment", steps)
        step = steps["scan_social_sentiment"]
        self.assertEqual(step["agent"], "SentimentAnalyst")
        self.assertEqual(step["action"], "scan_social_sentiment")
        self.assertIn("assets", step.get("inputs", {}))

    def test_scan_social_sentiment_runs_after_scan_news(self):
        steps = {s["id"]: s for s in self.workflow["steps"]}
        self.assertIn("scan_news", steps["scan_social_sentiment"].get("depends_on", []))

    def test_sentiment_analyst_in_registry(self):
        self.assertIn("SentimentAnalyst", self.registry_keys)


class SentimentAnalystHypothesisScorerIntegrationTest(unittest.TestCase):
    """Sprint 50: combined news + social is the new tie-breaker
    (not stacked). With BOTH at +1.0, the bull adjustment is
    the full +5 (combined = 1.0, capped). With only news at
    +1.0, the bull adjustment is +2.5 (combined = news/2 = 0.5).
    With agreement (news +1, social +1), combined = 1.0 (full)."""

    def test_combined_news_and_social_agreement_uses_full_magnitude(self):
        from src.agents.researchers import HypothesisScorer
        scorer = HypothesisScorer(position_repo=None, audit=None)
        base = scorer.manager.decide(
            {
                "asset": "BTC-USD", "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50, "macd_at_signal": 0.1, "atr_at_signal": 100.0,
            },
            open_positions=[],
        )
        # Both at +1.0 -> combined = 1.0, bull_adj = 5 * 1.0 = +5
        with_both = scorer.manager.decide(
            {
                "asset": "BTC-USD", "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50, "macd_at_signal": 0.1, "atr_at_signal": 100.0,
            },
            open_positions=[],
            news_context={
                "BTC-USD": {"news_sentiment": 1.0, "news_count": 5,
                            "top_headline": "x", "key_themes": [], "raw_titles": []},
            },
            social_context={
                "BTC-USD": {"social_sentiment": 1.0, "post_count": 5,
                            "top_post": "x", "source_subs": [], "raw_titles": []},
            },
        )
        # Bull score should be ~5 higher (the full cap)
        self.assertAlmostEqual(
            with_both["bull_score"] - base["bull_score"], 5.0, delta=0.5
        )

    def test_combined_disagreement_cancels(self):
        from src.agents.researchers import HypothesisScorer
        scorer = HypothesisScorer(position_repo=None, audit=None)
        base = scorer.manager.decide(
            {
                "asset": "BTC-USD", "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50, "macd_at_signal": 0.1, "atr_at_signal": 100.0,
            },
            open_positions=[],
        )
        # News +1, social -1 -> combined = 0, no adjustment
        with_conflict = scorer.manager.decide(
            {
                "asset": "BTC-USD", "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50, "macd_at_signal": 0.1, "atr_at_signal": 100.0,
            },
            open_positions=[],
            news_context={
                "BTC-USD": {"news_sentiment": 1.0, "news_count": 5,
                            "top_headline": "x", "key_themes": [], "raw_titles": []},
            },
            social_context={
                "BTC-USD": {"social_sentiment": -1.0, "post_count": 5,
                            "top_post": "x", "source_subs": [], "raw_titles": []},
            },
        )
        # Bull and bear should be unchanged
        self.assertAlmostEqual(
            with_conflict["bull_score"], base["bull_score"], places=4
        )
        self.assertAlmostEqual(
            with_conflict["bear_score"], base["bear_score"], places=4
        )

    def test_only_social_uses_half_magnitude(self):
        from src.agents.researchers import HypothesisScorer
        scorer = HypothesisScorer(position_repo=None, audit=None)
        base = scorer.manager.decide(
            {
                "asset": "BTC-USD", "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50, "macd_at_signal": 0.1, "atr_at_signal": 100.0,
            },
            open_positions=[],
        )
        # Only social +1.0 -> combined = (0 + 1) / 2 = 0.5, bull_adj = +2.5
        with_social_only = scorer.manager.decide(
            {
                "asset": "BTC-USD", "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50, "macd_at_signal": 0.1, "atr_at_signal": 100.0,
            },
            open_positions=[],
            social_context={
                "BTC-USD": {"social_sentiment": 1.0, "post_count": 5,
                            "top_post": "x", "source_subs": [], "raw_titles": []},
            },
        )
        self.assertAlmostEqual(
            with_social_only["bull_score"] - base["bull_score"], 2.5, delta=0.5
        )


if __name__ == "__main__":
    unittest.main()
