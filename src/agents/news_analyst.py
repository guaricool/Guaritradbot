"""
Sprint 49 — NewsAnalyst: pre-market news sentiment.

Inspired by Tauric Research's TradingAgents "News Analyst" role:
before scoring a hypothesis, scan recent news for the asset and
weight the score by the prevailing sentiment. Tauric does this
with an LLM (sentiment via GPT-4); we do it with a deterministic
lexicon-based score to keep the bot free of LLM cost and to
match the rest of the bot's "deterministic + auditable"
design philosophy.

Sources (all free, no API key, no auth):
  - Yahoo Finance RSS: feeds.finance.yahoo.com/rss/2.0/headline
    ?s={TICKER}&region=US&lang=en-US
  - (Future) Reuters / MarketWatch RSS for cross-source sentiment

Caching:
  - Per-asset, 1-hour TTL. The bot's analysis cycle is 30 min
    (Sprint 46S), so we re-scan at most once an hour per asset
    and the cycle uses the cached result on the next tick.
  - The cache survives container restarts because it's
    persisted to audit/news_scan_cache.jsonl (same volume
    as the audit ledger).

Sentiment scoring (lexicon-based, NO LLM):
  - Per headline, count occurrences of positive words
    (rally, surge, climb, gain, beat, approval, breakout,
    record, etc.) vs negative (drop, fall, dump, plunge,
    sell, crash, lawsuit, hack, plunge, etc.).
  - Score per headline: (pos - neg) / max(pos + neg, 1), in [-1, +1].
  - Aggregate per asset: mean of recent headlines, then
    re-clamped to [-1, +1].

Failure modes (the bot is LIVE; news must NEVER break trading):
  - RSS fetch fails (timeout, no internet, blocked) ->
    log warning, return empty result for that asset. The
    workflow continues with no news context.
  - Malformed XML -> log warning, return empty.
  - Sentiment calculation fails for a headline -> skip that
    headline.

Wiring (Sprint 49):
  - workflow yaml: new step `scan_news` before `analyze_market`.
    The result is stored in `state["scan_news"]` and the
    downstream HypothesisScorer reads `state.get("scan_news",
    {})` to weight scores.
  - HypothesisScorer: when scoring a hypothesis, look up
    `news_context.get(asset, {}).get("news_sentiment", 0.0)` and
    add it as a small adjustment to bull_score (positive
    sentiment) or bear_score (negative sentiment). Magnitude
    is intentionally small (+/- 5 max) so the news context
    is a TIE-BREAKER, not a primary signal.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from src.core.logging_setup import get_logger

logger = get_logger(__name__)


# Lexicons. Compact on purpose -- false positives in a 30-word
# headline are worse than missing a few real positives.
POSITIVE_WORDS = frozenset({
    "rally", "rallies", "rallying", "surge", "surges", "surging",
    "climb", "climbs", "climbing", "climbed",
    "gain", "gains", "gained", "gaining",
    "beat", "beats", "beating",
    "approval", "approve", "approved", "approves",
    "breakout", "breakouts",
    "record", "records", "high", "highs",
    "jump", "jumps", "jumped", "jumping",
    "soar", "soars", "soared", "soaring",
    "rise", "rises", "risen", "rising",
    "rebound", "rebounds", "rebounded", "rebounding",
    "boost", "boosts", "boosted", "boosting",
    "win", "wins", "won", "winning",
    "buy", "buys", "bought", "buying",  # buy signal
    "growth", "grow", "grew",  # positive
    "strong", "stronger", "strongest",
    "bullish", "bull",  # when used as descriptor
    "optimistic", "optimism",
    "resilient", "resilience",
    "outperform", "outperforms", "outperformed",
    "above",  # in "trades above $X" (positive direction)
    "buyback",  # capital return = positive
})

NEGATIVE_WORDS = frozenset({
    "drop", "drops", "dropped", "dropping",
    "fall", "falls", "fell", "falling",
    "dump", "dumps", "dumped", "dumping",
    "plunge", "plunges", "plunged", "plunging",
    "crash", "crashes", "crashed", "crashing",
    "sell", "sells", "sold", "selling",  # sell signal
    "loss", "losses", "lost",
    "loss", "losses",  # duplicate removed
    "decline", "declines", "declined", "declining",
    "tumble", "tumbles", "tumbled", "tumbling",
    "slump", "slumps", "slumped", "slumping",
    "weak", "weaker", "weakest", "weakness",
    "bearish", "bear",
    "pessimistic", "pessimism",
    "fear", "fears", "feared", "fearing",
    "panic", "panics", "panicked", "panicking",
    "hack", "hacked", "hacking",
    "lawsuit", "lawsuits", "litigation", "sued", "sues",
    "fraud", "fraudulent",
    "investigation", "investigated", "investigating",
    "fine", "fined", "fines",  # regulatory penalty
    "ban", "bans", "banned", "banning",
    "delisting", "delisted",
    "below",  # in "trades below $X" (negative direction)
    "miss", "misses", "missed",  # missed estimates
    "missed",  # duplicate
    "concern", "concerns", "concerned",  # uncertainty
    "warn", "warns", "warned", "warning",
})


def _score_headline_sentiment(headline: str) -> float:
    """Lexicon-based sentiment score for a single headline.

    Returns a float in [-1, +1]. The score is the normalized
    difference between positive and negative word counts:
        score = (pos - neg) / max(pos + neg, 1)
    which gives 0 when no signal words are present, +1 when
    all are positive, -1 when all are negative.
    """
    if not headline:
        return 0.0
    tokens = re.findall(r"[a-z']+", headline.lower())
    pos = sum(1 for t in tokens if t in POSITIVE_WORDS)
    neg = sum(1 for t in tokens if t in NEGATIVE_WORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / total))


def _aggregate_sentiment(headlines: List[str]) -> float:
    """Mean of per-headline scores, re-clamped to [-1, +1]."""
    if not headlines:
        return 0.0
    scores = [_score_headline_sentiment(h) for h in headlines]
    mean = sum(scores) / len(scores)
    return max(-1.0, min(1.0, mean))


# ---------------------------------------------------------------------
# Cache: 1-hour TTL per asset. Persisted to disk so a restart
# doesn't re-scan everything (RSS feeds are slow and rate-limited).
# ---------------------------------------------------------------------

DEFAULT_CACHE_PATH = "audit/news_scan_cache.jsonl"
DEFAULT_TTL_SECONDS = 3600.0  # 1 hour


@dataclass
class _CacheEntry:
    asset: str
    scanned_at: float        # unix ts
    sentiment: float         # -1 to +1
    news_count: int
    top_headline: str
    key_themes: List[str] = field(default_factory=list)
    raw_titles: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _load_cache_file(path: Path, ttl_s: float) -> Dict[str, Dict[str, Any]]:
    """Load the cache from disk as a dict of raw dicts. Drops
    entries older than ttl_s. The caller is responsible for
    converting the raw dicts to its own typed dataclass (each
    agent has a different schema, so we don't unify at the
    cache layer).

    Fault-tolerant: any line that fails to parse is skipped.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                import json
                rec = json.loads(line)
                ts = rec.get("scanned_at", 0.0)
                if (time.time() - ts) > ttl_s:
                    continue  # stale
                out[rec["asset"]] = rec
    except (OSError, ValueError, KeyError) as e:
        logger.warning(f"[NewsAnalyst/SentimentAnalyst] could not load cache: {e}")
    return out


def _load_cache(path: Path, ttl_s: float) -> Dict[str, _CacheEntry]:
    """Backward-compat wrapper for the NewsAnalyst cache.
    Returns _CacheEntry instances (the NewsAnalyst schema)."""
    raw = _load_cache_file(path, ttl_s)
    return {k: _CacheEntry(**v) for k, v in raw.items()}


def _save_cache(path: Path, entries: Dict[str, _CacheEntry]) -> None:
    """Persist the cache to disk. Atomic (write+replace via
    tmp+rename) so a power loss can't leave a half-written file."""
    import json
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for e in entries.values():
                f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------

def _extract_themes(titles: List[str]) -> List[str]:
    """Best-effort theme extraction. Looks for a small list of
    known themes and returns the ones mentioned in the titles.
    Used as a context hint in the lesson -- a future sprint
    could use this to weight by theme importance."""
    themes = []
    blob = " ".join(titles).lower()
    theme_keywords = {
        "regulation": ["regulation", "sec", "regulatory", "law", "bill",
                       "congress", "senator", "approved", "ban", "fired"],
        "geopolitics": ["war", "russia", "china", "iran", "ukraine",
                        "sanctions", "tariff", "trump", "biden",
                        "white house", "fed", "powell", "fomc"],
        "market_structure": ["etf", "fund", "flows", "institutional",
                             "whale", "liquidation", "futures",
                             "open interest", "funding rate"],
        "technical": ["breakout", "support", "resistance", "ma",
                      "moving average", "rsi", "macd", "pattern"],
        "security": ["hack", "exploit", "stolen", "breach", "vulnerability",
                     "compromised"],
        "adoption": ["partner", "integration", "launch", "announce",
                     "institutional", "treasury", "etf", "adopt"],
    }
    for theme, kws in theme_keywords.items():
        if any(kw in blob for kw in kws):
            themes.append(theme)
    return themes[:5]  # cap


def _fetch_yahoo_rss(asset: str, timeout_s: float = 8.0) -> List[str]:
    """Fetch Yahoo Finance RSS headlines for `asset`. Returns a
    list of titles (strings). Empty list on any failure.

    No LLM, no paid API. Yahoo's RSS is public.
    """
    url = (
        f"https://feeds.finance.yahoo.com/rss/2.0/headline"
        f"?s={asset}&region=US&lang=en-US"
    )
    req = Request(url, headers={"User-Agent": "Guaritradbot/1.0"})
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            data = resp.read().decode("utf-8", errors="replace")
    except (URLError, HTTPError, TimeoutError, OSError) as e:
        logger.warning(f"[NewsAnalyst] RSS fetch failed for {asset}: {e}")
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        logger.warning(f"[NewsAnalyst] RSS XML parse failed for {asset}: {e}")
        return []
    titles: List[str] = []
    for item in root.findall(".//item"):
        title = item.find("title")
        if title is not None and title.text:
            titles.append(title.text.strip())
    return titles[:30]  # cap at 30 most recent


# ---------------------------------------------------------------------
# Public agent
# ---------------------------------------------------------------------

class NewsAnalyst:
    """Sprint 49: scan recent news for the trading universe and
    emit a per-asset sentiment score that the downstream
    HypothesisScorer can use as a tie-breaker.

    Thread-safety: stateless (the cache is the only mutable
    state, and the cache is loaded fresh per call). Multiple
    concurrent calls are safe; the last writer wins on the
    cache save.
    """

    def __init__(
        self,
        cache_path: str = DEFAULT_CACHE_PATH,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        rss_fetcher=None,
    ):
        self.cache_path = Path(cache_path)
        self.ttl_seconds = float(ttl_seconds)
        # Inject a fetcher for tests. Default to the real Yahoo
        # Finance RSS scraper.
        self._fetch = rss_fetcher or _fetch_yahoo_rss

    def scan_news(
        self,
        *args,
        **kwargs,
    ) -> Dict[str, Dict[str, Any]]:
        """Scan news for each asset. Returns a dict keyed by asset
        with the per-asset news context. Empty dict per asset on
        any failure (RSS unreachable, no cache, parse error, etc.).

        The result structure per asset:
          {
              "asset": str,
              "news_sentiment": float,         # -1 to +1
              "news_count": int,
              "top_headline": str,             # most recent non-empty
              "key_themes": List[str],
              "raw_titles": List[str],
              "scanned_at": str,               # ISO 8601 UTC
              "from_cache": bool,             # whether this was a cache hit
          }

        Sprint 51: dual signature. The workflow engine
        (`src/workflows/engine.py:133`) calls every action as
        `action_method(inputs=<dict>, state=<dict>)`. Older
        callers and the Sprint 49/50 tests invoke this method
        directly as `scan_news(assets=[...], lookback_hours=24,
        use_cache=True)`. We support both to avoid the
        `TypeError: got an unexpected keyword argument 'inputs'`
        production crash documented in the live VPS log on
        2026-07-13.
        """
        # Sprint 51: dispatch on call shape. Workflow engine
        # passes (inputs=<dict>, state=<dict>); legacy callers
        # pass (assets=<list>, lookback_hours=<int>, use_cache=<bool>).
        inputs, state = _resolve_wf_args(args, kwargs, param_names=("assets", "lookback_hours", "use_cache"))
        assets = inputs.get("assets", []) if isinstance(inputs, dict) else []
        lookback_hours = int(inputs.get("lookback_hours", 24)) if isinstance(inputs, dict) else 24
        use_cache = bool(inputs.get("use_cache", True)) if isinstance(inputs, dict) else True
        # `state` is accepted for API uniformity with the
        # other workflow actions (researchers.run_debate,
        # strategy_agent.evaluate_strategies, etc.) but
        # scan_news is read-only and does not need it.
        del state
        return self._scan_news_impl(assets, lookback_hours, use_cache)

    def _scan_news_impl(
        self,
        assets: List[str],
        lookback_hours: int = 24,
        use_cache: bool = True,
    ) -> Dict[str, Dict[str, Any]]:
        """Implementation body of scan_news. See scan_news() for
        the dual signature and return contract."""
        cache = _load_cache(self.cache_path, self.ttl_seconds) if use_cache else {}
        out: Dict[str, Dict[str, Any]] = {}
        for asset in assets:
            asset = asset.strip()
            if not asset:
                continue
            # Cache hit?
            if use_cache and asset in cache:
                e = cache[asset]
                out[asset] = {
                    "asset": asset,
                    "news_sentiment": e.sentiment,
                    "news_count": e.news_count,
                    "top_headline": e.top_headline,
                    "key_themes": e.key_themes,
                    "raw_titles": e.raw_titles,
                    "scanned_at": _ts_to_iso(e.scanned_at),
                    "from_cache": True,
                }
                continue
            # Miss: fetch, score, cache.
            try:
                titles = self._fetch(asset)
            except Exception as e:
                logger.warning(
                    f"[NewsAnalyst] fetch raised for {asset}: {e}. "
                    f"Returning empty result for this asset."
                )
                titles = []
            sentiment = _aggregate_sentiment(titles)
            entry = _CacheEntry(
                asset=asset,
                scanned_at=time.time(),
                sentiment=sentiment,
                news_count=len(titles),
                top_headline=titles[0] if titles else "",
                key_themes=_extract_themes(titles),
                raw_titles=titles[:10],
            )
            out[asset] = {
                "asset": asset,
                "news_sentiment": sentiment,
                "news_count": entry.news_count,
                "top_headline": entry.top_headline,
                "key_themes": entry.key_themes,
                "raw_titles": entry.raw_titles,
                "scanned_at": _ts_to_iso(entry.scanned_at),
                "from_cache": False,
            }
            # Persist to cache (only the fresh entries; the
            # already-loaded ones are in the file).
            cache[asset] = entry
        # Best-effort cache write. Failure here is silent --
        # next call will just re-fetch.
        try:
            _save_cache(self.cache_path, cache)
        except OSError as e:
            logger.warning(f"[NewsAnalyst] could not save cache: {e}")
        return out


def _ts_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _resolve_wf_args(args, kwargs, param_names):
    """Sprint 51: dual-signature dispatcher for workflow actions.

    The workflow engine (`src/workflows/engine.py:133`) calls
    every action as `action_method(inputs=<dict>, state=<dict>)`.
    Legacy code paths (CLI, tests, library users) call the same
    method with positional args matching `param_names`.

    This helper normalizes both call shapes to a `(inputs, state)`
    tuple so the method body can read `inputs.get("key")`
    without caring which path the caller took.

    Detection rule: if the first positional arg is a dict and has
    at least one key matching a known engine field ("assets" /
    "lookback_hours" / "use_cache"), OR if the kwargs contain an
    `inputs` key whose value is a dict, treat it as the workflow
    call shape. Otherwise it's a legacy call — synthesize an
    inputs dict from the positional/kwargs.
    """
    ENGINE_KEYS = {"assets", "lookback_hours", "use_cache", "state"}
    # Workflow engine call: scan_news(inputs={<dict>}, state={<dict>})
    if args and isinstance(args[0], dict) and any(k in args[0] for k in param_names):
        inputs = args[0]
        state = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
        return inputs, state
    if "inputs" in kwargs and isinstance(kwargs["inputs"], dict):
        inputs = kwargs["inputs"]
        state = kwargs.get("state", {})
        if not isinstance(state, dict):
            state = {}
        return inputs, state
    # Legacy direct call: scan_news(assets=[...], lookback_hours=24, use_cache=True)
    inputs = {}
    if args:
        inputs[param_names[0]] = args[0]
    for i, name in enumerate(param_names[1:], start=1):
        if i < len(args):
            inputs[name] = args[i]
    for k, v in kwargs.items():
        if k in param_names:
            inputs[k] = v
    return inputs, {}
