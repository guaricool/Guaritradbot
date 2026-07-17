"""
MacroAnalyst — SHADOW MODE macro/geopolitical event scan.

Carlos asked whether the bot could "study markets for external
influences from what's happening in the world" and use that to spot
a possible entry. He picked, explicitly:
  1. Reinforcement only, never a standalone entry generator (same
     tie-breaker role NewsAnalyst/SentimentAnalyst already play).
  2. Shadow mode first — log what it WOULD have flagged, don't wire
     it into real scoring until there's evidence it's worth trusting.
  3. RSS + macro calendar coverage, not an LLM reading headlines —
     deterministic, free, auditable, same design philosophy as
     NewsAnalyst's lexicon scorer (no new cost cap needed).

DESIGN (intentionally conservative, mirrors LLMAnalyst Sprint 55):

1. SHADOW ONLY.
   Every scan is logged to the audit ledger as a `MACRO_SIGNAL_SHADOW`
   event with the detected event tags and the per-asset-class bias
   they imply. This is NEVER read by HypothesisScorer or any other
   part of the trading decision. After enough shadow data (same
   30+ day bar LLMAnalyst set for itself), compare the flagged bias
   against what actually happened to the flagged asset classes before
   giving this any real weight.

2. WHY THIS ISN'T ONE MORE PER-ASSET NEWS SCAN.
   NewsAnalyst already scans PER-TICKER headlines (Yahoo's per-symbol
   RSS) for asset-specific news. A Fed rate decision or a CPI print
   is a MARKET-WIDE event that may not even appear in "BTC-USD"'s own
   ticker feed, or may appear late. This scans a handful of
   macro-sensitive tickers (10Y yield, S&P 500, gold futures, dollar
   index) whose OWN headline feeds tend to carry macro-wide stories,
   and classifies events independent of any single traded asset, then
   maps a bias onto broad ASSET CLASSES (crypto / equity / commodity)
   rather than one symbol.

3. PHRASE-LEVEL PATTERNS, NOT BAG-OF-WORDS.
   NewsAnalyst's per-headline lexicon (count positive vs negative
   words) is the right tool for general sentiment, but it would
   misclassify macro headlines badly -- "Fed REFUSES to cut rates"
   contains "cut" but means the opposite of a rate cut. Macro event
   detection here uses proximity regex ("cut/lower ... rate", "raise/
   hike ... rate") instead of counting individual words, which is far
   more precise for this specific, narrower classification task.

4. FAIL-OPEN.
   RSS fetch failure, parse error, or no matching event -> empty
   result. The workflow continues normally. Trading must never depend
   on this being reachable.

Wiring: workflow yaml adds a `scan_macro` step (parallel to scan_news,
no dependency on it) whose result lands in `state["scan_macro"]` and
is currently NOT read by anything else -- see point 1.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.logging_setup import get_logger
from src.agents.news_analyst import (
    _fetch_yahoo_rss,
    _load_cache_file,
    _save_cache,
    _ts_to_iso,
    _resolve_wf_args,
)

logger = get_logger(__name__)


# Macro-sensitive tickers whose own headline feeds tend to carry
# market-wide stories (rate decisions, CPI prints, geopolitical risk),
# not tickers the bot trades. ^TNX = 10-year treasury yield, ^GSPC =
# S&P 500 index, GC=F = gold futures, DX-Y.NYB = US dollar index.
MACRO_PROXY_TICKERS = ["^TNX", "^GSPC", "GC=F", "DX-Y.NYB"]

# (event_name, compiled pattern). Proximity patterns ("cut ... rate"
# within ~30 chars) instead of bag-of-words -- see module docstring
# point 3 for why that matters here specifically.
_EVENT_PATTERNS = [
    ("fed_rate_cut", re.compile(
        r"\b(cut|cuts|cutting|lower|lowers|lowering|slash|slashes|ease|eases|easing)\b"
        r".{0,30}\b(rate|rates)\b", re.IGNORECASE)),
    ("fed_rate_hike", re.compile(
        r"\b(hike|hikes|hiking|raise|raises|raised|raising|increase|increases|increasing|tighten|tightening)\b"
        r".{0,30}\b(rate|rates)\b", re.IGNORECASE)),
    ("inflation_hot", re.compile(
        r"\b(cpi|inflation)\b.{0,30}\b(hot|higher|above|exceed|exceeds|beats?|surge|surges|accelerat\w*)\b",
        re.IGNORECASE)),
    ("inflation_cool", re.compile(
        r"\b(cpi|inflation)\b.{0,30}\b(cool|cools|cooling|lower|below|eases?|easing|slow|slows|slowing|decelerat\w*)\b",
        re.IGNORECASE)),
    ("recession_signal", re.compile(
        r"\brecession\b|\bgdp\b.{0,30}\b(contract\w*|shrink\w*|negative)\b", re.IGNORECASE)),
    ("geopolitical_crisis", re.compile(
        r"\b(war|invasion|invades?|sanctions?|conflict|missile|strikes?|attack\w*)\b", re.IGNORECASE)),
    ("banking_crisis", re.compile(
        r"\bbank\w*\b.{0,30}\b(collapse\w*|failure\w*|contagion|run|bailout)\b", re.IGNORECASE)),
]

# Directional bias per event, per broad asset class, in [-1, +1].
# Reasoning (documented so a future review can challenge any of it):
#   - Dovish Fed (rate cuts) -> risk-on for crypto/equity, and
#     bullish gold (lower real rates make a non-yielding asset like
#     gold relatively more attractive).
#   - Hawkish Fed (rate hikes) -> the mirror image.
#   - Hot inflation implies MORE hawkish Fed action ahead -> bearish
#     risk assets; gold's reaction to inflation prints alone is mixed
#     (inflation hedge vs. rate-hike fear) so left neutral (0.0) here
#     rather than guessing a direction we're not confident in.
#   - Recession / geopolitical / banking crisis signals: risk-off for
#     crypto and equity, flight-to-safety bid for gold.
MACRO_EVENT_BIAS: Dict[str, Dict[str, float]] = {
    "fed_rate_cut": {"crypto": 1.0, "equity": 1.0, "commodity": 1.0},
    "fed_rate_hike": {"crypto": -1.0, "equity": -1.0, "commodity": -1.0},
    "inflation_hot": {"crypto": -1.0, "equity": -1.0, "commodity": 0.0},
    "inflation_cool": {"crypto": 1.0, "equity": 1.0, "commodity": 0.0},
    "recession_signal": {"crypto": -1.0, "equity": -1.0, "commodity": 1.0},
    "geopolitical_crisis": {"crypto": -1.0, "equity": -1.0, "commodity": 1.0},
    "banking_crisis": {"crypto": -1.0, "equity": -1.0, "commodity": 1.0},
}

ASSET_CLASSES = ("crypto", "equity", "commodity")

# Known limitation, documented rather than hidden: proximity regex
# still can't parse real negation ("Fed REFUSES to cut rates" still
# matches "cut ... rate"). This is a blunt but meaningful mitigation --
# if a negation marker appears in the ~20 chars right before the
# match, treat the headline as NOT a clear signal for that event
# rather than confidently (and wrongly) counting it. This trades
# missing some genuine events for not confidently misreading negated
# ones, which is the safer direction for a shadow-mode signal whose
# whole purpose is to be evaluated for trustworthiness later.
_NEGATION_PATTERN = re.compile(
    r"\b(not|won't|wont|refuses?|refused|unlikely|no plans?|ruled out|rules out|denies|denied|against)\b\s*\S*\s*$",
    re.IGNORECASE,
)
_NEGATION_LOOKBEHIND_CHARS = 20


def _is_negated(headline: str, match_start: int) -> bool:
    window = headline[max(0, match_start - _NEGATION_LOOKBEHIND_CHARS):match_start]
    return bool(_NEGATION_PATTERN.search(window))


def _detect_events(headlines: List[str]) -> Dict[str, int]:
    """Count how many headlines match each event pattern. A single
    headline can match more than one event (rare but possible, e.g.
    a recap story mentioning both CPI and the Fed). Skips matches
    immediately preceded by a negation marker -- see
    `_NEGATION_PATTERN`'s comment for why and its limits."""
    counts: Dict[str, int] = {}
    for headline in headlines:
        if not headline:
            continue
        for event_name, pattern in _EVENT_PATTERNS:
            m = pattern.search(headline)
            if m and not _is_negated(headline, m.start()):
                counts[event_name] = counts.get(event_name, 0) + 1
    return counts


def _bias_from_events(event_counts: Dict[str, int]) -> Dict[str, float]:
    """Combine every detected event's bias into one score per asset
    class, weighted by how many headlines mentioned it, clamped to
    [-1, +1]. Returns 0.0 for a class with no signal at all."""
    totals = {cls: 0.0 for cls in ASSET_CLASSES}
    weight_sum = 0
    for event_name, count in event_counts.items():
        bias = MACRO_EVENT_BIAS.get(event_name)
        if bias is None:
            continue
        for cls in ASSET_CLASSES:
            totals[cls] += bias[cls] * count
        weight_sum += count
    if weight_sum == 0:
        return {cls: 0.0 for cls in ASSET_CLASSES}
    return {cls: max(-1.0, min(1.0, totals[cls] / weight_sum)) for cls in ASSET_CLASSES}


DEFAULT_CACHE_PATH = "audit/macro_scan_cache.jsonl"
DEFAULT_TTL_SECONDS = 3600.0  # 1 hour -- macro events don't need faster than this


@dataclass
class _MacroCacheEntry:
    # Field name is "asset" (not e.g. "key") even though this scan
    # isn't per-asset -- `_load_cache_file` (shared with NewsAnalyst)
    # keys its output dict on `rec["asset"]` unconditionally, so this
    # has to match that shape to actually round-trip through the
    # shared cache loader. Always the literal string "macro".
    asset: str
    scanned_at: float
    event_counts: Dict[str, int] = field(default_factory=dict)
    bias: Dict[str, float] = field(default_factory=dict)
    headline_count: int = 0
    top_headline: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MacroAnalyst:
    """SHADOW MODE macro/geopolitical event scanner. See module
    docstring for the full design rationale and why this is
    deliberately NOT yet consumed by any trading decision."""

    def __init__(
        self,
        cache_path: str = DEFAULT_CACHE_PATH,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        rss_fetcher=None,
        audit=None,
    ):
        self.cache_path = Path(cache_path)
        self.ttl_seconds = float(ttl_seconds)
        self._fetch = rss_fetcher or _fetch_yahoo_rss
        self.audit = audit

    def scan_macro(self, *args, **kwargs) -> Dict[str, Any]:
        """Dual-signature entry point -- see NewsAnalyst.scan_news's
        docstring for why (`src/workflows/engine.py` calls every step
        as `action(inputs=<dict>, state=<dict>)`; direct/legacy callers
        pass positional args). This step takes no real inputs (macro
        scan isn't per-asset) but keeps the same shape for consistency
        and so `optional: true` workflow error handling behaves the
        same way as every other agent step.
        """
        inputs, state = _resolve_wf_args(args, kwargs, param_names=("use_cache",))
        use_cache = bool(inputs.get("use_cache", True)) if isinstance(inputs, dict) else True
        del state
        return self._scan_macro_impl(use_cache)

    def _scan_macro_impl(self, use_cache: bool = True) -> Dict[str, Any]:
        cache = _load_cache_file(self.cache_path, self.ttl_seconds) if use_cache else {}
        cached = cache.get("macro")
        if use_cache and cached:
            result = {
                "event_counts": cached.get("event_counts", {}),
                "bias": cached.get("bias", {}),
                "headline_count": cached.get("headline_count", 0),
                "top_headline": cached.get("top_headline", ""),
                "scanned_at": _ts_to_iso(cached.get("scanned_at", time.time())),
                "from_cache": True,
            }
            return result

        headlines: List[str] = []
        for ticker in MACRO_PROXY_TICKERS:
            try:
                headlines.extend(self._fetch(ticker))
            except Exception as e:
                logger.warning(f"[MacroAnalyst] fetch raised for {ticker}: {e}. Skipping.")

        event_counts = _detect_events(headlines)
        bias = _bias_from_events(event_counts)
        now = time.time()
        entry = _MacroCacheEntry(
            asset="macro",
            scanned_at=now,
            event_counts=event_counts,
            bias=bias,
            headline_count=len(headlines),
            top_headline=headlines[0] if headlines else "",
        )

        if self.audit is not None:
            try:
                self.audit.append("MACRO_SIGNAL_SHADOW", {
                    "event_counts": event_counts,
                    "bias": bias,
                    "headline_count": len(headlines),
                    "top_headline": entry.top_headline,
                    "shadow_only": True,
                    "note": "Not consumed by any trading decision yet -- see MacroAnalyst module docstring.",
                })
            except Exception:
                pass  # audit logging must never break the scan

        try:
            _save_cache(self.cache_path, {"macro": entry})
        except OSError as e:
            logger.warning(f"[MacroAnalyst] could not save cache: {e}")

        return {
            "event_counts": event_counts,
            "bias": bias,
            "headline_count": len(headlines),
            "top_headline": entry.top_headline,
            "scanned_at": _ts_to_iso(now),
            "from_cache": False,
        }
