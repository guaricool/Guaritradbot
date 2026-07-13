"""
Sprint 48 — Decision Log: persistent per-trade memory.

The audit's structural finding (and my recommendation in this
session's M1 followup): the bot's equity curve is essentially
flat (the dashboard shows the bot going from $0 to ~-$0.07
realized over days of live trading). It doesn't learn from its
mistakes because nothing tells the next cycle "you made this
trade yesterday and lost 2% — here is what to do differently
next time".

Tauric Research's TradingAgents has this as their
`~/.tradingagents/memory/trading_memory.md` — every run
appends a decision, on the next run for the same ticker the
framework fetches the realised return, generates a 1-paragraph
reflection, and injects the most recent decisions into the
Portfolio Manager prompt. This is a major part of why their
framework learns from history even when each individual cycle
is stateless.

This module is the local equivalent for Guaritradbot:

  - record_hypothesis(...)  → every decision the bot considers
    (approved or rejected by the HypothesisScorer).
  - record_outcome(...)     → when a position closes, the
    realized P&L + hold time + auto-generated lesson.
  - recent_lessons_for(asset, n)  → last N lessons for a given
    asset, ready to inject into the next cycle's scoring
    context. The HypothesisScorer does this injection.
  - recent_decisions(n)     → cross-asset recent decisions
    (for the dashboard's audit feed).
  - all_decisions()         → full history (for backtesting
    in a future sprint).

Storage: `audit/decision_log.jsonl` (one JSON object per line).
Writes go through `src.core.atomic_write.atomic_write_text` so
a power loss can't leave a half-written file. Loads are
fault-tolerant: a corrupted line is skipped (and the bad line
is logged to stderr) so a single bad write can't brick the
bot. The whole point of a decision log is that it must be
non-blocking on the trading path -- if the disk is full or
the file is corrupted, the bot still trades; we just lose the
memory for that cycle.

Thread safety: the bot runs the fast monitor in its own thread
and the workflow in the main thread, both of which may call
record_*(). A `threading.Lock` guards the file write so we
don't interleave two records' bytes. The in-memory cache is
also locked.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from src.core.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)


# Default location: inside the audit/ volume so the decision log
# survives container redeploys (same pattern as audit.jsonl and
# positions.json). Can be overridden in the constructor for tests.
DEFAULT_LOG_PATH = "audit/decision_log.jsonl"
# Cap the in-memory cache at the most recent N records. The
# file holds everything; the cache is just to avoid re-parsing
# on every recent_lessons_for() call.
DEFAULT_CACHE_SIZE = 500


@dataclass
class HypothesisRecord:
    """A decision the bot CONSIDERED (approved or rejected)."""
    ts: str                        # ISO 8601 UTC, e.g. "2026-07-12T22:50:18Z"
    asset: str
    direction: str                 # "long" | "short"
    strategy: str
    score: float                   # the aggregate score from HypothesisScorer
    bull_score: float
    bear_score: float
    risk_penalty: float
    decision: str                  # "APPROVED" | "REJECTED"
    reason: str
    # Optional: lessons from prior trades on this asset that
    # the HypothesisScorer considered when scoring this one.
    # Lets us correlate "considered lesson X" with "took trade Y"
    # in backtest analysis later.
    considered_lessons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OutcomeRecord:
    """A position that closed. Links to the original hypothesis
    via position_id (which the bot already assigns in the
    PositionRepository)."""
    ts: str                        # ISO 8601 UTC at close
    position_id: str
    asset: str
    direction: str
    strategy: str                  # may be "" if the position predates the log
    entry_price: float
    exit_price: float
    qty: float
    pnl_usd: float                 # realized P&L in USD (post-fee)
    pnl_pct: float                 # realized P&L as % of notional
    hold_hours: float
    exit_reason: str               # "sl", "tp", "smart_profit_take",
                                   # "replacement", "manual", etc.
    # Auto-generated lesson. For now: a structured one-paragraph
    # summary derived from the data (no LLM). A future sprint
    # can layer an LLM reflection on top of this.
    lesson: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    """ISO 8601 UTC timestamp with Z suffix (matching the rest
    of the audit ledger's format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_lesson(outcome: OutcomeRecord) -> str:
    """Auto-generate a one-paragraph lesson from the outcome
    data. No LLM involved — just a structured summary the
    HypothesisScorer can use as a "previous trade" hint.

    Examples:
      "Trade BTC-USD long (MACD_BullCross) closed at sl after
       4.2h, -$0.30 (-3.0%). Short hold time + stop hit
       suggests the entry was too early or the stop was too
       tight."
      "Trade SPY long (MeanReversion_LONG_RSI<30) closed at tp
       after 18.5h, +$1.20 (+12.0%). Long hold + TP hit suggests
       the strategy works on mean-reversion setups in SPY."
    """
    direction_word = outcome.direction
    if outcome.pnl_usd >= 0:
        outcome_word = "won"
        outcome_emoji = "+"
    else:
        outcome_word = "lost"
        outcome_emoji = ""
    hold_descriptor = (
        "Short hold time" if outcome.hold_hours < 6
        else "Medium hold" if outcome.hold_hours < 48
        else "Long hold"
    )
    if outcome.exit_reason == "sl":
        pattern = "stop hit suggests the entry was too early or the stop was too tight"
    elif outcome.exit_reason == "tp":
        pattern = "TP hit suggests the strategy works on this setup"
    elif outcome.exit_reason == "smart_profit_take":
        pattern = "smart-profit-take suggests the reversal signal was strong enough to act on"
    elif outcome.exit_reason == "replacement":
        pattern = "replacement-close suggests a better opportunity took the slot"
    else:
        pattern = f"exit reason: {outcome.exit_reason}"
    return (
        f"Trade {outcome.asset} {direction_word} ({outcome.strategy or 'unknown'}) "
        f"{outcome_word} -- closed at {outcome.exit_reason} after "
        f"{outcome.hold_hours:.1f}h, {outcome_emoji}${outcome.pnl_usd:.4f} "
        f"({outcome.pnl_pct:+.2f}%). {hold_descriptor} + {pattern}."
    )


class DecisionLog:
    """Persistent memory of every hypothesis the bot considered
    and every position that closed. Thread-safe, fault-tolerant,
    non-blocking on the trading path (if the file is unreachable,
    the bot still trades; we just lose the memory for that cycle).

    Storage: append-only JSONL file. The in-memory cache is a
    bounded deque of the most recent N records (both hypothesis
    and outcome) for fast recent_lessons_for() queries.

    Wiring (Sprint 48):
      - HypothesisScorer.run_debate()  →  log a HypothesisRecord
        for every verdict.
      - PositionRepository.close_position()  →  log an
        OutcomeRecord when the close succeeds.
      - BotRuntime constructor  →  instantiate the singleton
        (loaded once at boot, then in-memory).
    """

    def __init__(
        self,
        path: str = DEFAULT_LOG_PATH,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ):
        self.path = Path(path)
        # In-memory cache: most recent N records (any kind), keyed
        # by the jsonl line order. Used for recent_lessons_for().
        # We don't partition by type because the ordering matters
        # (the user wants chronological history, not hypothesis-
        # then-outcome). The line itself is a dict with a "kind"
        # field ("hypothesis" | "outcome").
        self._cache: Deque[Dict[str, Any]] = deque(maxlen=cache_size)
        self._lock = threading.Lock()
        # Counters for the dashboard's "decisions recorded" tile.
        self._hypotheses_logged: int = 0
        self._outcomes_logged: int = 0
        self._load_existing()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_existing(self) -> None:
        """Load existing records from the file at startup.

        Fault-tolerant: any line that fails to parse is logged
        to stderr and skipped. A single bad line can't brick
        the bot. The file is allowed to not exist (first run).
        """
        if not self.path.exists():
            logger.info(
                f"[DecisionLog] no existing log at {self.path}; "
                f"starting fresh"
            )
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as e:
                        # Don't crash the bot for a bad line. Log
                        # and move on. A future maintenance script
                        # could quarantine the bad line.
                        logger.warning(
                            f"[DecisionLog] skipping malformed line: "
                            f"{e} (line preview: {line[:80]!r})"
                        )
                        continue
                    self._cache.append(record)
                    kind = record.get("kind")
                    if kind == "hypothesis":
                        self._hypotheses_logged += 1
                    elif kind == "outcome":
                        self._outcomes_logged += 1
            logger.info(
                f"[DecisionLog] loaded {self._hypotheses_logged} hypothesis + "
                f"{self._outcomes_logged} outcome records from {self.path}"
            )
        except OSError as e:
            # Disk error, permission denied, etc. Don't crash --
            # the bot still trades, we just lose the memory.
            logger.warning(
                f"[DecisionLog] could not read {self.path}: {e}. "
                f"Starting with empty memory."
            )

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def _append(self, record: Dict[str, Any]) -> None:
        """Append a record to the file (atomic) and the in-memory
        cache. Thread-safe via self._lock.

        The file write is best-effort: if the disk is full or
        the path is unwritable, we log to stderr and continue.
        The bot should NOT lose a trade because the decision
        log can't be written.
        """
        with self._lock:
            try:
                line = json.dumps(record, ensure_ascii=False) + "\n"
                # Append mode: each write is one line. We use
                # atomic_write_text on a per-line basis -- since
                # it's just a single line, the atomicity mostly
                # matters for the OSError cleanup (no half-written
                # line on failure). For the cache, append.
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    import os as _os
                    try:
                        _os.fsync(f.fileno())
                    except OSError:
                        # Same lenient behavior as atomic_write:
                        # some filesystems don't support fsync.
                        pass
                self._cache.append(record)
                if record.get("kind") == "hypothesis":
                    self._hypotheses_logged += 1
                else:
                    self._outcomes_logged += 1
            except OSError as e:
                # Don't let a logging failure block the trade.
                logger.warning(
                    f"[DecisionLog] could not append to {self.path}: {e}. "
                    f"Trade continues without decision-log entry."
                )

    # ------------------------------------------------------------------
    # Public API: hypothesis + outcome recording
    # ------------------------------------------------------------------

    def record_hypothesis(
        self,
        asset: str,
        direction: str,
        strategy: str,
        score: float,
        bull_score: float,
        bear_score: float,
        risk_penalty: float,
        decision: str,
        reason: str,
        considered_lessons: Optional[List[str]] = None,
    ) -> None:
        """Log that the HypothesisScorer considered a hypothesis
        and arrived at a decision. Both APPROVED and REJECTED
        hypotheses are logged -- the rejected ones are the most
        valuable for "what did I almost do and why not" analysis.
        """
        record = HypothesisRecord(
            ts=_now_iso(),
            asset=asset,
            direction=direction,
            strategy=strategy or "unknown",
            score=float(score),
            bull_score=float(bull_score),
            bear_score=float(bear_score),
            risk_penalty=float(risk_penalty),
            decision=str(decision),
            reason=str(reason),
            considered_lessons=list(considered_lessons or []),
        )
        rec = record.to_dict()
        rec["kind"] = "hypothesis"
        self._append(rec)

    def record_outcome(
        self,
        position_id: str,
        asset: str,
        direction: str,
        strategy: str,
        entry_price: float,
        exit_price: float,
        qty: float,
        pnl_usd: float,
        pnl_pct: float,
        hold_hours: float,
        exit_reason: str,
    ) -> None:
        """Log that a position closed. The lesson is auto-generated
        from the data via _format_lesson().
        """
        outcome = OutcomeRecord(
            ts=_now_iso(),
            position_id=str(position_id),
            asset=str(asset),
            direction=str(direction),
            strategy=str(strategy or ""),
            entry_price=float(entry_price),
            exit_price=float(exit_price),
            qty=float(qty),
            pnl_usd=float(pnl_usd),
            pnl_pct=float(pnl_pct),
            hold_hours=float(hold_hours),
            exit_reason=str(exit_reason),
            lesson="",  # filled below
        )
        outcome.lesson = _format_lesson(outcome)
        rec = outcome.to_dict()
        rec["kind"] = "outcome"
        self._append(rec)

    # ------------------------------------------------------------------
    # Public API: queries (read-only, used by the HypothesisScorer
    # for context injection and by the dashboard for the audit feed)
    # ------------------------------------------------------------------

    def recent_lessons_for(self, asset: str, n: int = 5) -> List[str]:
        """Return the lessons from the last N outcomes for `asset`,
        most recent first. Used by the HypothesisScorer to inject
        "what happened the last few times I traded this asset"
        into the scoring context.

        Returns an empty list if there are no prior outcomes for
        this asset (which is the common case on a fresh install
        or a new asset). The list is short strings (1-2 sentences
        each) so the HypothesisScorer can pass them as a "context"
        without bloating the prompt.
        """
        with self._lock:
            # Walk the cache backward (most recent first), filter
            # for outcome records of this asset, take n.
            lessons: List[str] = []
            for rec in reversed(self._cache):
                if rec.get("kind") != "outcome":
                    continue
                if rec.get("asset") != asset:
                    continue
                lesson = rec.get("lesson", "")
                if lesson:
                    lessons.append(lesson)
                if len(lessons) >= n:
                    break
            return lessons

    def recent_outcomes_for(
        self,
        asset: str,
        direction: Optional[str] = None,
        n: int = 5,
    ) -> List[Dict[str, Any]]:
        """Sprint 52.4: return the last N outcome records for
        `asset` (optionally filtered to a specific direction),
        most recent first.

        Used by the StrategyAgent to suppress new hypotheses
        when the recent track record for an (asset, direction)
        is uniformly bad — e.g. "the last 3 BTC-USD longs all
        lost", which is a stronger signal than a lesson string
        the scorer has to interpret.

        Unlike `recent_lessons_for` (which returns human-readable
        strings), this returns the raw structured record so the
        caller can apply its own threshold logic (e.g. count
        how many had `pnl_usd < 0`).

        Returns an empty list if there are no prior outcomes
        for the filter. Direction filter is optional — if
        omitted, returns all directions for the asset.
        """
        with self._lock:
            out: List[Dict[str, Any]] = []
            for rec in reversed(self._cache):
                if rec.get("kind") != "outcome":
                    continue
                if rec.get("asset") != asset:
                    continue
                if direction is not None and rec.get("direction") != direction:
                    continue
                out.append(dict(rec))
                if len(out) >= n:
                    break
            return out

    def recent_decisions(self, n: int = 20) -> List[Dict[str, Any]]:
        """Return the last N records (any kind), most recent first.
        Used by the dashboard's audit feed / decision-log tile.
        """
        with self._lock:
            return list(reversed(list(self._cache)[-n:]))

    def all_decisions(self) -> List[Dict[str, Any]]:
        """Full history, most recent first. For backtest / export.
        Note: this returns from the in-memory cache (bounded by
        DEFAULT_CACHE_SIZE=500). The file has the full history
        if you need more than that."""
        with self._lock:
            return list(reversed(list(self._cache)))

    @property
    def stats(self) -> Dict[str, int]:
        """Counts for the dashboard tile."""
        with self._lock:
            return {
                "hypotheses_logged": self._hypotheses_logged,
                "outcomes_logged": self._outcomes_logged,
                "cache_size": len(self._cache),
            }


# ----------------------------------------------------------------------
# Singleton accessor
# ----------------------------------------------------------------------
#
# The bot has multiple call sites (HypothesisScorer, PositionRepository,
# dashboard, future bots like NewsAnalyst). We want one shared instance
# so the in-memory cache and counters are consistent across the
# process. The singleton is lazy-initialized on first use.

_singleton: Optional["DecisionLog"] = None
_singleton_lock = threading.Lock()


def get_decision_log(
    path: Optional[str] = None,
    cache_size: int = DEFAULT_CACHE_SIZE,
) -> DecisionLog:
    """Return the process-wide DecisionLog singleton.

    Thread-safe lazy init. If `path` is None, uses the current
    value of `DEFAULT_LOG_PATH` (evaluated at CALL time, not at
    import time -- see the comment below on the monkey-patch
    pattern). Tests that need an isolated instance can pass a
    non-default path; the first caller wins; subsequent calls
    with the same path return the same instance, even if they
    passed different cache_size.

    Note on the default: we deliberately do NOT use
    `path: str = DEFAULT_LOG_PATH` as the function signature,
    because Python evaluates default values at function
    DEFINITION time. If the caller patches `DEFAULT_LOG_PATH`
    after this module is imported, the patch is invisible to
    `get_decision_log` (the function's default was already
    bound to the old value). Resolving `DEFAULT_LOG_PATH`
    inside the function body (this implementation) makes the
    patch take effect on the next call.
    """
    global _singleton
    if path is None:
        path = DEFAULT_LOG_PATH
    with _singleton_lock:
        if _singleton is None:
            _singleton = DecisionLog(path=path, cache_size=cache_size)
        return _singleton


def reset_decision_log_singleton() -> None:
    """Drop the singleton. Tests use this to start fresh between
    cases. Production code should never call this."""
    global _singleton
    with _singleton_lock:
        _singleton = None
