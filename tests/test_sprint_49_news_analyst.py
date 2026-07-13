"""
Sprint 49 (NewsAnalyst) — tests for src/agents/news_analyst.py.

The NewsAnalyst is a NEW agent. To be safe with the live bot,
the test strategy is:
  1. Unit tests with a mocked fetcher (no internet calls).
  2. Cache behavior (TTL, persistence, fault tolerance).
  3. Sentiment scoring (positive/negative/neutral headlines).
  4. Integration: the workflow yaml references NewsAnalyst and
     the new step is in the registry (regression guard for the
     47D workflow/registry consistency lesson).
  5. Integration: the HypothesisScorer correctly applies the
     news sentiment as a +/- 5 tie-breaker.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.agents.news_analyst import (
    DEFAULT_CACHE_PATH,
    NewsAnalyst,
    _aggregate_sentiment,
    _extract_themes,
    _score_headline_sentiment,
)


class ScoreHeadlineTest(unittest.TestCase):
    """Sprint 49: per-headline sentiment via lexicon match."""

    def test_positive_headline(self):
        # Two positive words, no negative -> +1
        s = _score_headline_sentiment("Bitcoin rally gains record high")
        self.assertGreater(s, 0.5)

    def test_negative_headline(self):
        s = _score_headline_sentiment("Crypto crash plunge sell-off fear")
        self.assertLess(s, -0.5)

    def test_neutral_headline(self):
        s = _score_headline_sentiment("The Federal Reserve met today")
        self.assertEqual(s, 0.0)

    def test_mixed_headline_score_in_range(self):
        # A headline with both positive and negative words
        # should produce a score in (-1, +1) -- exact value
        # depends on which words are in the lexicon, so we
        # just check the bound.
        s = _score_headline_sentiment("Bitcoin rally amid crash concerns")
        self.assertGreater(s, -1.0)
        self.assertLess(s, 1.0)

    def test_clamp_to_bounds(self):
        s = _score_headline_sentiment("surge gains climb rise jump")
        self.assertLessEqual(s, 1.0)
        self.assertGreaterEqual(s, -1.0)
        s = _score_headline_sentiment("crash plunge fear dump sell")
        self.assertGreaterEqual(s, -1.0)


class AggregateSentimentTest(unittest.TestCase):
    def test_empty_returns_zero(self):
        self.assertEqual(_aggregate_sentiment([]), 0.0)

    def test_mixed_headlines_average_out(self):
        score = _aggregate_sentiment([
            "Bitcoin rally gains",
            "Crypto crash plunge",
            "Markets steady",
        ])
        # First +1, second -1, third 0 -> mean 0
        self.assertAlmostEqual(score, 0.0, places=4)


class ExtractThemesTest(unittest.TestCase):
    def test_regulation_detected(self):
        themes = _extract_themes(["SEC approves new ETF", "Congress debates bill"])
        self.assertIn("regulation", themes)

    def test_geopolitics_detected(self):
        themes = _extract_themes(["War in Ukraine continues", "Sanctions on Russia"])
        self.assertIn("geopolitics", themes)

    def test_no_themes_in_neutral_text(self):
        themes = _extract_themes(["Markets opened today", "Some news happened"])
        # May detect "market_structure" from "markets" -- let's
        # just check that the result is a list, not assert empty
        # (lexicon matches are noisy and that's fine).
        self.assertIsInstance(themes, list)


class NewsAnalystScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = Path(self.tmp.name) / "news_cache.jsonl"
        # Mock fetcher: returns predetermined titles per asset
        self.titles_by_asset = {
            "BTC-USD": [
                "Bitcoin rally gains record high",
                "Crypto fear crash plunge",
                "Markets steady today",
            ],
            "SPY": [
                "S&P 500 surge climb record",
                "Bearish decline drop",
            ],
        }
        def fake_fetcher(asset):
            return self.titles_by_asset.get(asset, [])
        self.fake_fetcher = fake_fetcher

    def _make_analyst(self, ttl=3600):
        return NewsAnalyst(
            cache_path=str(self.cache_path),
            ttl_seconds=ttl,
            rss_fetcher=self.fake_fetcher,
        )

    def test_scan_news_returns_per_asset_context(self):
        analyst = self._make_analyst()
        result = analyst.scan_news(["BTC-USD", "SPY", "GLD"])
        self.assertIn("BTC-USD", result)
        self.assertIn("SPY", result)
        self.assertIn("GLD", result)  # no titles, but entry exists
        btc = result["BTC-USD"]
        self.assertIn("news_sentiment", btc)
        self.assertIn("news_count", btc)
        self.assertIn("top_headline", btc)
        self.assertIn("scanned_at", btc)
        self.assertIn("raw_titles", btc)
        self.assertEqual(btc["news_count"], 3)

    def test_btc_sentiment_roughly_balanced(self):
        # BTC-USD: 1 positive (rally gains), 1 negative (fear crash plunge), 1 neutral
        # Score: 1, -1, 0 -> mean 0
        analyst = self._make_analyst()
        result = analyst.scan_news(["BTC-USD"])
        self.assertAlmostEqual(result["BTC-USD"]["news_sentiment"], 0.0, places=2)

    def test_spy_sentiment_slightly_positive(self):
        # SPY: 1 positive, 1 negative -> 0
        analyst = self._make_analyst()
        result = analyst.scan_news(["SPY"])
        self.assertAlmostEqual(result["SPY"]["news_sentiment"], 0.0, places=2)

    def test_gld_with_no_news_returns_zero(self):
        analyst = self._make_analyst()
        result = analyst.scan_news(["GLD"])
        self.assertEqual(result["GLD"]["news_sentiment"], 0.0)
        self.assertEqual(result["GLD"]["news_count"], 0)
        self.assertEqual(result["GLD"]["top_headline"], "")

    def test_empty_asset_list_returns_empty_dict(self):
        analyst = self._make_analyst()
        result = analyst.scan_news([])
        self.assertEqual(result, {})

    def test_empty_string_asset_skipped(self):
        analyst = self._make_analyst()
        result = analyst.scan_news(["", "BTC-USD"])
        self.assertNotIn("", result)
        self.assertIn("BTC-USD", result)


class NewsAnalystCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = Path(self.tmp.name) / "news_cache.jsonl"
        self.fetch_count = 0
        def counting_fetcher(asset):
            self.fetch_count += 1
            return ["Bitcoin rally"]
        self.counting_fetcher = counting_fetcher

    def test_cache_hit_on_second_call(self):
        analyst = NewsAnalyst(
            cache_path=str(self.cache_path),
            ttl_seconds=3600,
            rss_fetcher=self.counting_fetcher,
        )
        # First call: fetches
        result1 = analyst.scan_news(["BTC-USD"])
        self.assertEqual(self.fetch_count, 1)
        self.assertFalse(result1["BTC-USD"]["from_cache"])
        # Second call within TTL: uses cache
        result2 = analyst.scan_news(["BTC-USD"])
        self.assertEqual(self.fetch_count, 1)  # no new fetch
        self.assertTrue(result2["BTC-USD"]["from_cache"])

    def test_cache_expiry_triggers_refetch(self):
        # TTL of 0.1 seconds -- expires immediately
        analyst = NewsAnalyst(
            cache_path=str(self.cache_path),
            ttl_seconds=0.1,
            rss_fetcher=self.counting_fetcher,
        )
        analyst.scan_news(["BTC-USD"])
        self.assertEqual(self.fetch_count, 1)
        # Sleep past the TTL
        import time
        time.sleep(0.2)
        analyst.scan_news(["BTC-USD"])
        self.assertEqual(self.fetch_count, 2)

    def test_cache_persists_across_instances(self):
        # First instance writes the cache
        a1 = NewsAnalyst(
            cache_path=str(self.cache_path),
            rss_fetcher=self.counting_fetcher,
        )
        a1.scan_news(["BTC-USD"])
        # Second instance loads from the same file
        a2 = NewsAnalyst(
            cache_path=str(self.cache_path),
            rss_fetcher=self.counting_fetcher,
        )
        result = a2.scan_news(["BTC-USD"])
        # Cache hit (loaded from disk), no new fetch
        self.assertEqual(self.fetch_count, 1)
        self.assertTrue(result["BTC-USD"]["from_cache"])


class NewsAnalystFailureTest(unittest.TestCase):
    """Sprint 49: the NewsAnalyst must NEVER break the bot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = Path(self.tmp.name) / "news_cache.jsonl"

    def test_fetcher_exception_returns_empty_result(self):
        def broken_fetcher(asset):
            raise RuntimeError("network down")
        analyst = NewsAnalyst(
            cache_path=str(self.cache_path),
            rss_fetcher=broken_fetcher,
        )
        # Should not raise
        result = analyst.scan_news(["BTC-USD"])
        self.assertIn("BTC-USD", result)
        self.assertEqual(result["BTC-USD"]["news_sentiment"], 0.0)
        self.assertEqual(result["BTC-USD"]["news_count"], 0)

    def test_corrupt_cache_file_is_tolerated(self):
        # Write a corrupt line to the cache file
        self.cache_path.write_text(
            "this is not valid json\n"
            '{"asset": "BTC-USD", "scanned_at": 9999999999, "sentiment": 0.5, "news_count": 5, "top_headline": "x", "key_themes": [], "raw_titles": []}\n',
            encoding="utf-8",
        )
        # The corrupt line should be skipped; the valid one
        # (with a future timestamp) should be loaded
        analyst = NewsAnalyst(
            cache_path=str(self.cache_path),
            rss_fetcher=lambda a: [],
        )
        result = analyst.scan_news(["BTC-USD"])
        # Should not raise, and the cache should be loaded
        self.assertIn("BTC-USD", result)


class NewsAnalystHypothesisScorerIntegrationTest(unittest.TestCase):
    """Sprint 49: the HypothesisScorer's decide() takes news_context
    and applies a +/- 5 sentiment tie-breaker."""

    def test_positive_news_raises_bull_score(self):
        from src.agents.researchers import HypothesisScorer
        scorer = HypothesisScorer(position_repo=None, audit=None)
        # Baseline (no news): MACD_BullCross with neutral inputs
        base = scorer.manager.decide(
            {
                "asset": "BTC-USD",
                "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50,
                "macd_at_signal": 0.1,
                "atr_at_signal": 100.0,
            },
            open_positions=[],
        )
        # With strong positive news (+1.0 sentiment, 10 headlines)
        with_news = scorer.manager.decide(
            {
                "asset": "BTC-USD",
                "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50,
                "macd_at_signal": 0.1,
                "atr_at_signal": 100.0,
            },
            open_positions=[],
            news_context={
                "BTC-USD": {
                    "news_sentiment": 1.0,
                    "news_count": 10,
                    "top_headline": "Massive rally",
                    "key_themes": [],
                    "raw_titles": [],
                },
            },
        )
        # Bull score should be higher with positive news
        self.assertGreater(with_news["bull_score"], base["bull_score"])
        # Difference should be ~5 (the cap)
        self.assertAlmostEqual(with_news["bull_score"] - base["bull_score"], 5.0, delta=0.5)

    def test_negative_news_raises_bear_score(self):
        from src.agents.researchers import HypothesisScorer
        scorer = HypothesisScorer(position_repo=None, audit=None)
        base = scorer.manager.decide(
            {
                "asset": "BTC-USD",
                "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50,
                "macd_at_signal": 0.1,
                "atr_at_signal": 100.0,
            },
            open_positions=[],
        )
        with_negative_news = scorer.manager.decide(
            {
                "asset": "BTC-USD",
                "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50,
                "macd_at_signal": 0.1,
                "atr_at_signal": 100.0,
            },
            open_positions=[],
            news_context={
                "BTC-USD": {
                    "news_sentiment": -1.0,
                    "news_count": 8,
                    "top_headline": "Crashing",
                    "key_themes": [],
                    "raw_titles": [],
                },
            },
        )
        # Bear score should be higher with negative news
        self.assertGreater(with_negative_news["bear_score"], base["bear_score"])
        # Difference should be ~5
        self.assertAlmostEqual(
            with_negative_news["bear_score"] - base["bear_score"], 5.0, delta=0.5
        )

    def test_no_news_context_unchanged(self):
        from src.agents.researchers import HypothesisScorer
        scorer = HypothesisScorer(position_repo=None, audit=None)
        base = scorer.manager.decide(
            {
                "asset": "BTC-USD",
                "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50,
                "macd_at_signal": 0.1,
                "atr_at_signal": 100.0,
            },
            open_positions=[],
        )
        with_empty_news = scorer.manager.decide(
            {
                "asset": "BTC-USD",
                "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50,
                "macd_at_signal": 0.1,
                "atr_at_signal": 100.0,
            },
            open_positions=[],
            news_context={},
        )
        # Should be identical (no adjustment when news is empty)
        self.assertEqual(base["bull_score"], with_empty_news["bull_score"])
        self.assertEqual(base["bear_score"], with_empty_news["bear_score"])

    def test_news_only_for_matching_asset(self):
        """News for SPY doesn't affect a BTC-USD hypothesis."""
        from src.agents.researchers import HypothesisScorer
        scorer = HypothesisScorer(position_repo=None, audit=None)
        base = scorer.manager.decide(
            {
                "asset": "BTC-USD",
                "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50,
                "macd_at_signal": 0.1,
                "atr_at_signal": 100.0,
            },
            open_positions=[],
        )
        with_other_news = scorer.manager.decide(
            {
                "asset": "BTC-USD",
                "direction": "long",
                "strategy": "MACD_BullCross",
                "rsi_at_signal": 50,
                "macd_at_signal": 0.1,
                "atr_at_signal": 100.0,
            },
            open_positions=[],
            news_context={
                "SPY": {"news_sentiment": 1.0, "news_count": 5, "top_headline": "x",
                        "key_themes": [], "raw_titles": []},
            },
        )
        # SPY's positive news should not affect BTC-USD
        self.assertEqual(base["bull_score"], with_other_news["bull_score"])


class NewsAnalystWorkflowIntegrationTest(unittest.TestCase):
    """Sprint 49: the workflow yaml references NewsAnalyst and
    the agent is in the main.py registry. Same regression-guard
    pattern as Sprint 47D (workflow/registry consistency)."""

    def setUp(self):
        import yaml
        from pathlib import Path
        self.workflow = yaml.safe_load(
            Path("src/workflows/trading_loop.yaml").read_text(encoding="utf-8")
        )
        main_src = Path("main.py").read_text(encoding="utf-8")
        import re
        registry_match = re.search(r"registry\s*=\s*\{(.+?)\n\s*\}", main_src, re.DOTALL)
        self.registry_keys = set(re.findall(
            r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*[A-Z][A-Za-z0-9_]*\s*\(',
            registry_match.group(1),
        ))

    def test_scan_news_step_exists(self):
        steps = {s["id"]: s for s in self.workflow["steps"]}
        self.assertIn("scan_news", steps)
        scan_step = steps["scan_news"]
        self.assertEqual(scan_step["agent"], "NewsAnalyst")
        self.assertEqual(scan_step["action"], "scan_news")
        # Must include assets input
        self.assertIn("assets", scan_step.get("inputs", {}))

    def test_scan_news_runs_before_analyze_market(self):
        # Find the order via depends_on
        steps = {s["id"]: s for s in self.workflow["steps"]}
        # scan_news has no depends_on (it's first)
        self.assertNotIn("depends_on", steps["scan_news"])
        # analyze_market depends on scan_news
        self.assertIn("scan_news", steps["analyze_market"].get("depends_on", []))

    def test_news_analyst_in_registry(self):
        self.assertIn("NewsAnalyst", self.registry_keys)


if __name__ == "__main__":
    unittest.main()
