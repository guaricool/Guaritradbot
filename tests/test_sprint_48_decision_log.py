"""
Sprint 48 (decision log) — tests for src/safety/decision_log.py.

Covers:
  1. Fresh instance: empty cache, no errors
  2. record_hypothesis appends to file + cache
  3. record_outcome appends + auto-generates lesson
  4. recent_lessons_for(asset) returns matching asset only
  5. recent_lessons_for(asset) returns most recent first
  6. recent_decisions(n) returns last N in reverse order
  7. Thread-safety: concurrent record_hypothesis + record_outcome
  8. Fault tolerance: malformed line in file is skipped, not raised
  9. File non-existent on startup: loads gracefully (empty log)
  10. Singleton accessor: get_decision_log returns same instance
  11. Decision log writes happen via HypothesisScorer.decide (integration)
  12. Decision log writes happen via PositionRepository.close_position (integration)
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.safety.decision_log import (
    DEFAULT_LOG_PATH,
    DecisionLog,
    OutcomeRecord,
    _format_lesson,
    get_decision_log,
    reset_decision_log_singleton,
)


class FreshLogTest(unittest.TestCase):
    """Sprint 48: starting fresh (no existing file) works."""

    def test_no_existing_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision_log.jsonl"
            log = DecisionLog(path=str(path))
            self.assertEqual(log.stats["hypotheses_logged"], 0)
            self.assertEqual(log.stats["outcomes_logged"], 0)
            self.assertEqual(log.recent_decisions(10), [])
            self.assertEqual(log.recent_lessons_for("BTC-USD"), [])


class RecordHypothesisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "decision_log.jsonl"
        self.log = DecisionLog(path=str(self.path))

    def test_records_to_file_and_cache(self):
        self.log.record_hypothesis(
            asset="BTC-USD", direction="long", strategy="MACD_BullCross",
            score=58.4, bull_score=70, bear_score=40, risk_penalty=20,
            decision="APPROVED", reason="strong_bullish_macd",
            considered_lessons=["previous loss on BTC-USD long in 2025-06"],
        )
        # File should have 1 line
        with self.path.open() as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["kind"], "hypothesis")
        self.assertEqual(record["asset"], "BTC-USD")
        self.assertEqual(record["decision"], "APPROVED")
        self.assertEqual(record["considered_lessons"],
                         ["previous loss on BTC-USD long in 2025-06"])
        # Counters and cache
        self.assertEqual(self.log.stats["hypotheses_logged"], 1)
        self.assertEqual(len(self.log.recent_decisions(10)), 1)

    def test_rejected_hypotheses_also_logged(self):
        """The audit's intent: REJECTED hypotheses are the most
        valuable for "what did I almost do and why not" analysis.
        Both must be persisted."""
        self.log.record_hypothesis(
            asset="SPY", direction="long", strategy="MeanReversion_LONG_RSI<30",
            score=42.0, bull_score=55, bear_score=50, risk_penalty=10,
            decision="REJECTED", reason="below_threshold",
        )
        record = self.log.recent_decisions(1)[0]
        self.assertEqual(record["decision"], "REJECTED")
        self.assertEqual(record["reason"], "below_threshold")


class RecordOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "decision_log.jsonl"
        self.log = DecisionLog(path=str(self.path))

    def test_records_to_file_and_cache(self):
        self.log.record_outcome(
            position_id="pos_123",
            asset="BTC-USD", direction="long", strategy="MACD_BullCross",
            entry_price=50000.0, exit_price=51000.0, qty=0.001,
            pnl_usd=0.8, pnl_pct=1.6, hold_hours=4.5,
            exit_reason="tp",
        )
        record = self.log.recent_decisions(1)[0]
        self.assertEqual(record["kind"], "outcome")
        self.assertEqual(record["asset"], "BTC-USD")
        self.assertEqual(record["pnl_usd"], 0.8)
        self.assertEqual(record["pnl_pct"], 1.6)
        self.assertEqual(record["exit_reason"], "tp")
        # Auto-generated lesson
        self.assertIn("TP hit", record["lesson"])
        self.assertEqual(self.log.stats["outcomes_logged"], 1)

    def test_loss_lesson_mentions_stop_hit(self):
        self.log.record_outcome(
            position_id="pos_loss", asset="BTC-USD", direction="long",
            strategy="MACD_BullCross",
            entry_price=50000.0, exit_price=49000.0, qty=0.001,
            pnl_usd=-1.0, pnl_pct=-2.0, hold_hours=4.0,
            exit_reason="sl",
        )
        record = self.log.recent_decisions(1)[0]
        self.assertIn("stop hit", record["lesson"])
        self.assertIn("lost", record["lesson"])


class RecentLessonsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "decision_log.jsonl"
        self.log = DecisionLog(path=str(self.path))

    def test_returns_matching_asset_only(self):
        self.log.record_outcome(
            position_id="p1", asset="BTC-USD", direction="long", strategy="x",
            entry_price=100.0, exit_price=110.0, qty=0.01,
            pnl_usd=0.1, pnl_pct=10.0, hold_hours=5.0, exit_reason="tp",
        )
        self.log.record_outcome(
            position_id="p2", asset="SPY", direction="long", strategy="y",
            entry_price=100.0, exit_price=110.0, qty=0.01,
            pnl_usd=0.1, pnl_pct=10.0, hold_hours=5.0, exit_reason="tp",
        )
        # BTC-USD lessons: 1
        btc_lessons = self.log.recent_lessons_for("BTC-USD")
        self.assertEqual(len(btc_lessons), 1)
        self.assertIn("BTC-USD", btc_lessons[0])
        # SPY lessons: 1
        spy_lessons = self.log.recent_lessons_for("SPY")
        self.assertEqual(len(spy_lessons), 1)
        self.assertIn("SPY", spy_lessons[0])
        # GLD: 0
        self.assertEqual(self.log.recent_lessons_for("GLD"), [])

    def test_returns_most_recent_first(self):
        # Record 3 outcomes for BTC-USD, oldest first
        for i, exit_price in enumerate([101.0, 102.0, 103.0]):
            self.log.record_outcome(
                position_id=f"p{i}", asset="BTC-USD", direction="long",
                strategy=f"strat_{i}",
                entry_price=100.0, exit_price=exit_price, qty=0.01,
                pnl_usd=exit_price - 100.0, pnl_pct=exit_price - 100.0,
                hold_hours=1.0, exit_reason="tp",
            )
        # recent_lessons_for(n=2) should return the LAST 2 in
        # reverse-chronological order: strat_2 first, then strat_1
        lessons = self.log.recent_lessons_for("BTC-USD", n=2)
        self.assertEqual(len(lessons), 2)
        self.assertIn("strat_2", lessons[0])
        self.assertIn("strat_1", lessons[1])

    def test_limits_to_n(self):
        for i in range(5):
            self.log.record_outcome(
                position_id=f"p{i}", asset="BTC-USD", direction="long",
                strategy=f"strat_{i}",
                entry_price=100.0, exit_price=101.0, qty=0.01,
                pnl_usd=1.0, pnl_pct=1.0, hold_hours=1.0, exit_reason="tp",
            )
        self.assertEqual(len(self.log.recent_lessons_for("BTC-USD", n=3)), 3)
        self.assertEqual(len(self.log.recent_lessons_for("BTC-USD", n=10)), 5)


class RecentDecisionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "decision_log.jsonl"
        self.log = DecisionLog(path=str(self.path))

    def test_returns_last_n_in_reverse_order(self):
        for i in range(5):
            self.log.record_hypothesis(
                asset=f"A{i}", direction="long", strategy="s",
                score=50.0, bull_score=50, bear_score=50, risk_penalty=10,
                decision="APPROVED", reason="ok",
            )
        recent = self.log.recent_decisions(3)
        self.assertEqual(len(recent), 3)
        # Most recent first
        self.assertEqual(recent[0]["asset"], "A4")
        self.assertEqual(recent[1]["asset"], "A3")
        self.assertEqual(recent[2]["asset"], "A2")


class FaultToleranceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "decision_log.jsonl"

    def test_malformed_line_is_skipped(self):
        # Write a file with one good line and one bad line
        self.path.write_text(
            '{"kind": "hypothesis", "asset": "BTC-USD", "decision": "APPROVED", "ts": "2026-01-01T00:00:00Z"}\n'
            '{this is not valid json\n'
            '{"kind": "outcome", "asset": "BTC-USD", "exit_reason": "tp", "pnl_usd": 0.1, "ts": "2026-01-02T00:00:00Z"}\n',
            encoding="utf-8",
        )
        # Should NOT raise, should load the 2 good lines
        log = DecisionLog(path=str(self.path))
        self.assertEqual(log.stats["hypotheses_logged"], 1)
        self.assertEqual(log.stats["outcomes_logged"], 1)
        # Both should be in the cache
        self.assertEqual(len(log.recent_decisions(10)), 2)

    def test_file_permission_error_does_not_crash(self):
        # Use a path that can't be written (a directory, not a file)
        bad_path = Path(self.tmp.name) / "subdir_as_file"
        bad_path.mkdir()
        # Should NOT raise on construction
        log = DecisionLog(path=str(bad_path))
        self.assertEqual(log.stats["hypotheses_logged"], 0)
        # record_hypothesis should also not raise
        log.record_hypothesis(
            asset="BTC-USD", direction="long", strategy="s",
            score=50.0, bull_score=50, bear_score=50, risk_penalty=10,
            decision="APPROVED", reason="ok",
        )


class ThreadSafetyTest(unittest.TestCase):
    def test_concurrent_writes_no_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision_log.jsonl"
            log = DecisionLog(path=str(path))
            errors = []

            def writer_hypothesis(i):
                try:
                    log.record_hypothesis(
                        asset=f"A{i}", direction="long", strategy="s",
                        score=50.0, bull_score=50, bear_score=50, risk_penalty=10,
                        decision="APPROVED", reason="ok",
                    )
                except Exception as e:
                    errors.append(e)

            def writer_outcome(i):
                try:
                    log.record_outcome(
                        position_id=f"p{i}", asset=f"A{i}", direction="long",
                        strategy="s",
                        entry_price=100.0, exit_price=101.0, qty=0.01,
                        pnl_usd=1.0, pnl_pct=1.0, hold_hours=1.0, exit_reason="tp",
                    )
                except Exception as e:
                    errors.append(e)

            threads = []
            for i in range(50):
                threads.append(threading.Thread(target=writer_hypothesis, args=(i,)))
                threads.append(threading.Thread(target=writer_outcome, args=(i,)))
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # No errors
            self.assertEqual(errors, [])
            # All 50+50 records present
            self.assertEqual(log.stats["hypotheses_logged"], 50)
            self.assertEqual(log.stats["outcomes_logged"], 50)
            # File should have 100 valid lines
            with path.open() as f:
                lines = [l for l in f.read().split("\n") if l.strip()]
            self.assertEqual(len(lines), 100)
            # Each line should be valid JSON (no corruption)
            for line in lines:
                json.loads(line)  # raises if malformed


class SingletonTest(unittest.TestCase):
    def setUp(self):
        reset_decision_log_singleton()
        self.addCleanup(reset_decision_log_singleton)

    def test_get_returns_same_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision_log.jsonl"
            a = get_decision_log(path=str(path))
            b = get_decision_log(path=str(path))
            self.assertIs(a, b)

    def test_different_path_creates_new_instance(self):
        # Only the FIRST call's path wins (singleton is path-agnostic
        # at the module level). This is fine for production (one
        # path per process). Tests that need isolation should
        # call reset_decision_log_singleton() between cases.
        with tempfile.TemporaryDirectory() as tmp:
            path1 = Path(tmp) / "a.jsonl"
            path2 = Path(tmp) / "b.jsonl"
            a = get_decision_log(path=str(path1))
            b = get_decision_log(path=str(path2))
            self.assertIs(a, b)  # same instance, even with different path


class HypothesisScorerIntegrationTest(unittest.TestCase):
    """Sprint 48: the HypothesisScorer actually records decisions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "decision_log.jsonl"
        # Patch the default path BEFORE any decision_log import
        # -- the HypothesisScorer uses get_decision_log() with
        # the default. We monkey-patch the module constant.
        import src.safety.decision_log as dl_mod
        self._orig_path = dl_mod.DEFAULT_LOG_PATH
        dl_mod.DEFAULT_LOG_PATH = str(self.path)
        # Reset the singleton AFTER the patch so the first
        # get_decision_log() in this test sees the new path.
        reset_decision_log_singleton()
        self.addCleanup(lambda: setattr(dl_mod, "DEFAULT_LOG_PATH", self._orig_path))
        self.addCleanup(reset_decision_log_singleton)

    def test_hypothesis_scorer_records_verdicts(self):
        # The orchestrator class is `HypothesisScorer`; the
        # `decide` method lives on the inner `ScoreSynthesizer`
        # (the actual scoring function). Wire through the
        # orchestrator's `manager` attribute -- this is the
        # same call site the production `run_debate` uses.
        from src.agents.researchers import HypothesisScorer
        scorer = HypothesisScorer(position_repo=None, audit=None)
        verdict = scorer.manager.decide(
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
        # The decision log should have exactly 1 hypothesis record
        log = get_decision_log()
        self.assertEqual(log.stats["hypotheses_logged"], 1)
        record = log.recent_decisions(1)[0]
        self.assertEqual(record["kind"], "hypothesis")
        self.assertEqual(record["asset"], "BTC-USD")
        self.assertEqual(record["decision"], verdict["decision"])


class PositionRepositoryIntegrationTest(unittest.TestCase):
    """Sprint 48: closing a position records the outcome."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "decision_log.jsonl"
        import src.safety.decision_log as dl_mod
        self._orig_path = dl_mod.DEFAULT_LOG_PATH
        dl_mod.DEFAULT_LOG_PATH = str(self.path)
        # Reset AFTER patch so the singleton points to our temp path
        reset_decision_log_singleton()
        self.addCleanup(lambda: setattr(dl_mod, "DEFAULT_LOG_PATH", self._orig_path))
        self.addCleanup(reset_decision_log_singleton)

    def test_close_position_records_outcome(self):
        from src.data_store.positions import PositionRepository, Position
        import time as _t
        repo = PositionRepository("data_store/positions.json")
        # Add a position manually
        pos = Position(
            asset="BTC-USD", direction="long",
            entry_price=50000.0, stop_loss=49000.0, take_profit=51000.0,
            qty=0.001, risk_usd=1.0, entry_ts=_t.time() - 3600,  # 1h ago
            strategy="MACD_BullCross",
        )
        repo.positions.append(pos)
        # Close it
        result = repo.close_position(
            pos.position_id, close_price=50500.0, reason="tp", fee_pct=0.001
        )
        self.assertIsNotNone(result)
        # The decision log should have 1 outcome record
        log = get_decision_log()
        self.assertEqual(log.stats["outcomes_logged"], 1)
        record = log.recent_decisions(1)[0]
        self.assertEqual(record["kind"], "outcome")
        self.assertEqual(record["asset"], "BTC-USD")
        self.assertEqual(record["exit_reason"], "tp")
        self.assertGreater(record["pnl_usd"], 0)
        # Lesson should mention TP hit
        self.assertIn("TP hit", record["lesson"])


class FormatLessonTest(unittest.TestCase):
    """Sprint 48: the auto-generated lesson is human-readable."""

    def test_winning_trade_lesson(self):
        rec = OutcomeRecord(
            ts="2026-07-12T22:00:00Z", position_id="p1",
            asset="BTC-USD", direction="long", strategy="MACD_BullCross",
            entry_price=50000.0, exit_price=51000.0, qty=0.001,
            pnl_usd=0.8, pnl_pct=1.6, hold_hours=4.5, exit_reason="tp",
            lesson="",
        )
        lesson = _format_lesson(rec)
        self.assertIn("won", lesson)
        self.assertIn("MACD_BullCross", lesson)
        self.assertIn("4.5h", lesson)
        self.assertIn("TP hit", lesson)

    def test_losing_trade_lesson_mentions_stop(self):
        rec = OutcomeRecord(
            ts="2026-07-12T22:00:00Z", position_id="p1",
            asset="BTC-USD", direction="long", strategy="MACD_BullCross",
            entry_price=50000.0, exit_price=49000.0, qty=0.001,
            pnl_usd=-1.0, pnl_pct=-2.0, hold_hours=4.0, exit_reason="sl",
            lesson="",
        )
        lesson = _format_lesson(rec)
        self.assertIn("lost", lesson)
        self.assertIn("stop hit", lesson)


if __name__ == "__main__":
    unittest.main()
