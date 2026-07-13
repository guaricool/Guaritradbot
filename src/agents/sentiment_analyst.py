"""
Sprint 50 — SentimentAnalyst: social-sentiment from Reddit.

The NewsAnalyst (Sprint 49) catches official news headlines.
The SentimentAnalyst catches the RETAIL CROWD: r/wallstreetbets,
r/bitcoin, r/ethtrader. Tauric Research's TradingAgents has a
Sentiment Analyst role that aggregates "news headlines, StockTwits,
and Reddit chatter into a single sentiment read". StockTwits
doesn't have a free public API; Reddit's public JSON endpoint
(`reddit.com/r/{sub}/search.json`) does, so this sprint is
Reddit-only -- a future sprint can add StockTwits via a paid
data vendor if the bot's balance ever supports the cost.

The lexicon is shared with NewsAnalyst (same positive/negative
word lists, same scoring formula). This is intentional: the
bot already learned to weight positive vs negative words the
same way in the news feed; reusing the lexicon means the social
sentiment and news sentiment are on the SAME SCALE, so
combining them is meaningful.

Sources (free, no API key, no auth):
  - Reddit JSON: reddit.com/r/{subreddit}/search.json
    ?q={TICKER}&restrict_sr=on&t=day&limit=25
  - Subreddits: r/wallstreetbets (general market),
    r/bitcoin + r/ethtrader (BTC/ETH),
    r/stocks + r/investing (equities)

Caching:
  - Same 1-hour TTL + persisted JSONL cache pattern as
    NewsAnalyst (audit/social_sentiment_cache.jsonl).

Sentiment scoring:
  - Per Reddit post title, lexicon match (same as news).
  - Aggregate per asset: mean of recent post scores, clamped
    to [-1, +1]. This is the "social sentiment" the
    HypothesisScorer weights alongside news_sentiment.

Failure modes (same as NewsAnalyst -- bot is LIVE):
  - Reddit fetch fails (429, blocked, no internet) ->
    empty result for that asset, no raise.
  - JSON parse error -> empty.
  - Per-post sentiment calc fails -> skip that post.

Wiring (Sprint 50):
  - workflow yaml: new step `scan_social_sentiment` after
    `scan_news`. The result is stored in
    `state["scan_social_sentiment"]`.
  - HypothesisScorer: news_context + social_context are
    combined into a single sentiment tie-breaker
    (magnitude still capped at +/- 5 to keep news a
    SECONDARY signal, as the audit intended).
"""
from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from src.agents.news_analyst import (
    POSITIVE_WORDS,
    NEGATIVE_WORDS,
    _score_headline_sentiment,
    _aggregate_sentiment,
    _load_cache_file,
    _save_cache,
    _ts_to_iso,
)
from src.core.logging_setup import get_logger

logger = get_logger(__name__)


# Reddit user-agent: Reddit's public JSON API requires a
# descriptive UA. Generic UAs get 429s. Be polite.
_REDDIT_UA = "Guaritradbot/1.0 (Trading sentiment research; contact via dashboard)"

# Default subreddit per asset. The general fallback
# (r/wallstreetbets) is used for assets without a
# dedicated sub.
DEFAULT_SUBREDDITS = {
    "BTC-USD": ["Bitcoin", "bitcoin"],
    "ETH-USD": ["Ethereum", "ethereum"],
    "SPY": ["wallstreetbets", "stocks"],
    "QQQ": ["wallstreetbets", "stocks"],
    "GLD": ["wallstreetbets", "investing"],
    "USO": ["wallstreetbets", "investing"],
}

DEFAULT_CACHE_PATH = "audit/social_sentiment_cache.jsonl"
DEFAULT_TTL_SECONDS = 3600.0  # 1 hour


@dataclass
class _CacheEntry:
    asset: str
    scanned_at: float
    sentiment: float
    post_count: int
    top_post: str
    raw_titles: List[str] = field(default_factory=list)
    source_subs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _fetch_reddit(subreddit: str, query: str, timeout_s: float = 8.0) -> List[Dict[str, str]]:
    """Fetch Reddit search results as a list of {title, permalink}.
    Returns empty list on any failure.

    Reddit's public JSON endpoint:
      https://www.reddit.com/r/{sub}/search.json
        ?q={query}&restrict_sr=on&t=day&limit=25&sort=new

    We use sort=new so we get the most recent posts (matches
    the 24h lookback intent of the news scan).
    """
    from urllib.parse import quote
    url = (
        f"https://www.reddit.com/r/{subreddit}/search.json"
        f"?q={quote(query)}&restrict_sr=on&t=day&limit=25&sort=new"
    )
    req = Request(url, headers={"User-Agent": _REDDIT_UA})
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            data = resp.read().decode("utf-8", errors="replace")
    except (URLError, HTTPError, TimeoutError, OSError) as e:
        # 429 rate limit is the most common cause here. Log
        # at warning level (operator should know if Reddit
        # is blocking us).
        logger.warning(f"[SentimentAnalyst] Reddit fetch failed for r/{subreddit}: {e}")
        return []
    try:
        root = json.loads(data)
    except json.JSONDecodeError as e:
        logger.warning(f"[SentimentAnalyst] Reddit JSON parse failed for r/{subreddit}: {e}")
        return []
    children = (
        root.get("data", {}).get("children", []) if isinstance(root, dict) else []
    )
    out: List[Dict[str, str]] = []
    for c in children:
        d = c.get("data", {}) if isinstance(c, dict) else {}
        title = d.get("title", "").strip()
        if title:
            out.append({"title": title, "permalink": d.get("permalink", "")})
    return out


def _resolve_subreddits(asset: str) -> List[str]:
    """Pick the subreddits to query for `asset`. Falls back
    to a general-market subreddit if the asset is not in the
    map. Returns a list of subreddit names (without r/)."""
    if asset in DEFAULT_SUBREDDITS:
        return DEFAULT_SUBREDDITS[asset]
    # Fallback: scan the general market subs
    return ["wallstreetbets", "stocks", "investing"]


class SentimentAnalyst:
    """Sprint 50: scan Reddit for the trading universe and emit
    a per-asset social sentiment that the HypothesisScorer
    combines with news sentiment for a tie-breaker.

    Thread-safety: stateless (the cache is the only mutable
    state, and the cache is loaded fresh per call)."""

    def __init__(
        self,
        cache_path: str = DEFAULT_CACHE_PATH,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        fetcher=None,
    ):
        self.cache_path = Path(cache_path)
        self.ttl_seconds = float(ttl_seconds)
        # Inject a fetcher for tests. Default to the real
        # Reddit JSON scraper.
        self._fetch = fetcher or _fetch_reddit

    def scan_social_sentiment(
        self,
        assets: List[str],
        use_cache: bool = True,
    ) -> Dict[str, Dict[str, Any]]:
        """Scan social sentiment for each asset. Returns a
        dict keyed by asset with the per-asset context. Empty
        dict per asset on any failure.

        Result structure per asset:
          {
              "asset": str,
              "social_sentiment": float,  # -1 to +1
              "post_count": int,
              "top_post": str,             # most recent non-empty title
              "raw_titles": List[str],
              "source_subs": List[str],    # which subs were scanned
              "scanned_at": str,
              "from_cache": bool,
          }
        """
        cache_raw = _load_cache_file(self.cache_path, self.ttl_seconds) if use_cache else {}
        cache = {k: _CacheEntry(**v) for k, v in cache_raw.items()}
        out: Dict[str, Dict[str, Any]] = {}
        for asset in assets:
            asset = asset.strip()
            if not asset:
                continue
            if use_cache and asset in cache:
                e = cache[asset]
                out[asset] = {
                    "asset": asset,
                    "social_sentiment": e.sentiment,
                    "post_count": e.post_count,
                    "top_post": e.top_post,
                    "raw_titles": e.raw_titles,
                    "source_subs": e.source_subs,
                    "scanned_at": _ts_to_iso(e.scanned_at),
                    "from_cache": True,
                }
                continue
            # Miss: fetch across all sub/keyword combos
            titles: List[str] = []
            subs_used: List[str] = []
            for sub in _resolve_subreddits(asset):
                # The query is the asset itself; r/wallstreetbets
                # uses "BTC" or "Bitcoin", etc. We try the
                # asset as-is first.
                for query in (asset, _asset_query_aliases(asset)):
                    try:
                        posts = self._fetch(sub, query)
                    except Exception as e:
                        logger.warning(
                            f"[SentimentAnalyst] fetch raised for "
                            f"r/{sub} q={query!r}: {e}. Skipping."
                        )
                        posts = []
                    titles.extend(p["title"] for p in posts)
                    if sub not in subs_used:
                        subs_used.append(sub)
            # Cap at 50 most recent to bound memory + scoring time
            titles = titles[:50]
            sentiment = _aggregate_sentiment(titles)
            entry = _CacheEntry(
                asset=asset,
                scanned_at=time.time(),
                sentiment=sentiment,
                post_count=len(titles),
                top_post=titles[0] if titles else "",
                raw_titles=titles[:10],
                source_subs=subs_used,
            )
            out[asset] = {
                "asset": asset,
                "social_sentiment": sentiment,
                "post_count": entry.post_count,
                "top_post": entry.top_post,
                "raw_titles": entry.raw_titles,
                "source_subs": entry.source_subs,
                "scanned_at": _ts_to_iso(entry.scanned_at),
                "from_cache": False,
            }
            cache[asset] = entry
        try:
            _save_cache(self.cache_path, cache)
        except OSError as e:
            logger.warning(f"[SentimentAnalyst] could not save cache: {e}")
        return out


def _asset_query_aliases(asset: str) -> List[str]:
    """Reddit-friendly aliases for assets (e.g. BTC-USD -> BTC,
    SPY -> SPY, etc). The first alias is the asset-as-is;
    subsequent are domain-specific search terms."""
    aliases = {
        "BTC-USD": ["Bitcoin", "BTC"],
        "ETH-USD": ["Ethereum", "ETH"],
        "SPY": ["SPY", "S&P 500"],
        "QQQ": ["QQQ", "Nasdaq"],
        "GLD": ["GLD", "gold"],
        "USO": ["USO", "oil"],
    }
    return aliases.get(asset, [])
