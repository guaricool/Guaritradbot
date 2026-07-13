"""
Sprint 46N — audit A5: scheduler mono-hilo suspende SL/TP + returns no
cacheados por ciclo.

Two independent fixes, covered separately below:

  Part 1 (main.py): `fast_monitor_tick` used to be registered on the
  SAME global `schedule` instance / single-threaded `while True:
  schedule.run_pending()` loop that also drives the hourly
  `job_with_monitor` cycle (src/execution/scheduler.py). A slow hourly
  cycle (many yfinance downloads with retries, plus per-hypothesis
  portfolio-risk-gate yfinance calls) could starve fast_monitor_tick
  for its entire duration, suspending SL/TP protection exactly when
  the bot is busiest. Fix: fast_monitor_tick now runs on its own
  daemon thread with its own timer (see main()'s `_fast_monitor_loop`/
  `_fast_monitor_thread`), guarded by a non-blocking
  `threading.Lock()` so overlapping ticks (if one runs long) are
  skipped rather than queued. Since `_fast_monitor_loop` is a closure
  defined inside `main()`, it isn't directly importable — instead this
  file (a) statically verifies main.py's source wires the thread up
  correctly (guards against a future regression silently re-merging
  the two schedules), and (b) unit-tests the exact non-blocking-lock
  skip-on-overlap PATTERN main.py uses, as a free-standing reproduction,
  to prove that pattern actually prevents overlapping runs.

  Part 2 (asset_correlation.py / tail_risk.py / risk_agent.py): the
  correlation (90d window) and CVaR (180d window) portfolio-risk gates
  each called `fetch_returns` (yfinance) fresh for every hypothesis
  evaluated in a single `validate_and_size()` cycle, even though nothing
  about the existing open book changes mid-cycle. Fix: `fetch_returns`
  gained an optional read-through `cache` dict; `analyze_assets`/
  `compute_portfolio_tail_risk` gained a `returns_cache` passthrough;
  `RiskManagerAgent.validate_and_size()` now seeds two SEPARATE
  per-cycle cache dicts (different windows -> must not be shared) and
  passes them into `_check_portfolio_correlation`/
  `_check_portfolio_tail_risk` every hypothesis in that cycle.

Run: python -m unittest tests.test_sprint_46n_a5_thread_and_cache -v
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _make_pos(asset: str, notional_usd: float, direction: str = "long"):
    from src.data_store.positions import Position
    return Position(
        asset=asset,
        direction=direction,
        entry_price=notional_usd,
        stop_loss=0.0,
        take_profit=0.0,
        qty=1.0,
        risk_usd=1.0,
        entry_ts=time.time(),
        strategy="test",
    )


def _make_risk(opens=None, **kwargs):
    from src.agents.risk_agent import RiskManagerAgent
    from src.data_store.positions import PositionRepository
    tmpdir = tempfile.mkdtemp()
    repo = PositionRepository(path=os.path.join(tmpdir, "positions.json"))
    for p in (opens or []):
        repo.positions.append(p)
    return RiskManagerAgent(position_repo=repo, **kwargs)


def _NoPolicy():
    """Sprint 44B's allocation-policy drift gate runs BEFORE the
    correlation/tail-risk gates and would otherwise reject a 100%-
    crypto or single-asset-class test book before those gates are
    even reached -- these cache-reuse tests care about the
    correlation/tail-risk gates specifically, not allocation drift,
    so it's disabled the same way `asset_concentration_check=False`
    disables the 44A concentration backstop."""
    from src.data.asset_allocation import AllocationPolicy
    return AllocationPolicy(enabled=False)


def _fake_series(n=200):
    import numpy as np
    import pandas as pd
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.Series(np.random.normal(0, 0.01, n), index=idx)


# ============================================================
# Part 2a: fetch_returns / analyze_assets read-through cache
# ============================================================

class FetchReturnsCacheTest(unittest.TestCase):
    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_cache_hit_skips_network_call(self, mock_yf):
        from src.analysis.asset_correlation import fetch_returns
        import pandas as pd
        df = pd.DataFrame(
            {"Close": [100.0 + i for i in range(30)]},
            index=pd.date_range("2024-01-01", periods=30, freq="D"),
        )
        mock_yf.return_value = df

        cache: dict = {}
        r1 = fetch_returns(["BTC-USD"], cache=cache)
        self.assertEqual(mock_yf.call_count, 1)
        self.assertIn("BTC-USD", cache)

        # Second call, same cache -> no additional network call.
        r2 = fetch_returns(["BTC-USD"], cache=cache)
        self.assertEqual(mock_yf.call_count, 1)
        self.assertIs(r2["BTC-USD"], r1["BTC-USD"])

    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_no_cache_always_fetches(self, mock_yf):
        """Default behavior (cache=None) is unchanged: every call fetches."""
        from src.analysis.asset_correlation import fetch_returns
        import pandas as pd
        df = pd.DataFrame(
            {"Close": [100.0 + i for i in range(30)]},
            index=pd.date_range("2024-01-01", periods=30, freq="D"),
        )
        mock_yf.return_value = df

        fetch_returns(["BTC-USD"])
        fetch_returns(["BTC-USD"])
        self.assertEqual(mock_yf.call_count, 2)

    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_partial_cache_only_fetches_missing_symbols(self, mock_yf):
        from src.analysis.asset_correlation import fetch_returns
        import pandas as pd
        df = pd.DataFrame(
            {"Close": [100.0 + i for i in range(30)]},
            index=pd.date_range("2024-01-01", periods=30, freq="D"),
        )
        mock_yf.return_value = df
        cache = {"BTC-USD": _fake_series(30)}

        fetch_returns(["BTC-USD", "ETH-USD"], cache=cache)
        # Only ETH-USD should have triggered a real fetch.
        self.assertEqual(mock_yf.call_count, 1)
        mock_yf.assert_called_with("ETH-USD", period="90d", interval="1d")


class AnalyzeAssetsCacheTest(unittest.TestCase):
    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_returns_cache_forwarded_and_reused(self, mock_yf):
        from src.analysis.asset_correlation import analyze_assets
        import pandas as pd
        df = pd.DataFrame(
            {"Close": [100.0 + i * 0.1 for i in range(60)]},
            index=pd.date_range("2024-01-01", periods=60, freq="D"),
        )
        mock_yf.return_value = df

        cache: dict = {}
        analyze_assets(["BTC-USD", "ETH-USD"], returns_cache=cache)
        self.assertEqual(mock_yf.call_count, 2)
        analyze_assets(["BTC-USD", "ETH-USD"], returns_cache=cache)
        # Second call with the SAME cache must not refetch either symbol.
        self.assertEqual(mock_yf.call_count, 2)

    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_no_cache_param_is_backward_compatible(self, mock_yf):
        from src.analysis.asset_correlation import analyze_assets
        mock_yf.return_value = None
        result = analyze_assets(["BTC-USD"])
        self.assertIsNone(result.well_diversified)  # N5 behavior untouched


# ============================================================
# Part 2b: compute_portfolio_tail_risk read-through cache
# ============================================================

class ComputePortfolioTailRiskCacheTest(unittest.TestCase):
    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_returns_cache_forwarded_and_reused(self, mock_yf):
        from src.analysis.tail_risk import compute_portfolio_tail_risk
        import pandas as pd
        df = pd.DataFrame(
            {"Close": [100.0 + i * 0.05 for i in range(200)]},
            index=pd.date_range("2024-01-01", periods=200, freq="D"),
        )
        mock_yf.return_value = df

        cache: dict = {}
        compute_portfolio_tail_risk({"BTC-USD": 50.0, "ETH-USD": 50.0}, returns_cache=cache)
        self.assertEqual(mock_yf.call_count, 2)
        compute_portfolio_tail_risk({"BTC-USD": 50.0, "ETH-USD": 50.0}, returns_cache=cache)
        self.assertEqual(mock_yf.call_count, 2)

    @patch("src.analysis.asset_correlation.safe_yf_download")
    def test_correlation_and_tail_risk_caches_must_stay_separate(self, mock_yf):
        """The two windows (90d correlation vs 180d CVaR) must NEVER
        share a cache dict -- a series fetched for one window would be
        silently reused for the other, wrong-length window otherwise.
        This test just documents/guards the contract by using two
        independent dicts and confirming both still fetch once each
        (i.e. nothing coalesces them internally)."""
        from src.analysis.asset_correlation import analyze_assets
        from src.analysis.tail_risk import compute_portfolio_tail_risk
        import pandas as pd
        df = pd.DataFrame(
            {"Close": [100.0 + i * 0.05 for i in range(200)]},
            index=pd.date_range("2024-01-01", periods=200, freq="D"),
        )
        mock_yf.return_value = df

        corr_cache: dict = {}
        tail_cache: dict = {}
        analyze_assets(["BTC-USD"], returns_cache=corr_cache)
        compute_portfolio_tail_risk({"BTC-USD": 100.0}, returns_cache=tail_cache)
        # Each cache independently populated -- two separate fetches,
        # not deduped against each other.
        self.assertEqual(mock_yf.call_count, 2)
        self.assertIn("BTC-USD", corr_cache)
        self.assertIn("BTC-USD", tail_cache)
        self.assertIsNot(corr_cache, tail_cache)


# ============================================================
# Part 2c: RiskManagerAgent wires two separate per-cycle caches
# ============================================================

class RiskAgentPerCycleCacheTest(unittest.TestCase):
    def test_validate_and_size_seeds_two_separate_caches(self):
        risk = _make_risk(opens=[_make_pos("BTC-USD", 50.0)])
        state = {"generate_hypotheses": {"hypotheses": []}}
        risk.validate_and_size({}, state)
        corr_cache = getattr(risk, "_cycle_correlation_returns_cache", None)
        tail_cache = getattr(risk, "_cycle_tail_risk_returns_cache", None)
        self.assertIsNotNone(corr_cache)
        self.assertIsNotNone(tail_cache)
        self.assertIsNot(corr_cache, tail_cache)

    def test_same_correlation_cache_object_reused_across_hypotheses(self):
        """Two hypotheses evaluated in ONE validate_and_size() cycle
        must both be checked against the SAME correlation-cache dict
        object (so the second hypothesis's correlation check doesn't
        re-fetch data the first one's check already pulled)."""
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0), _make_pos("ETH-USD", 50.0)],
            portfolio_stress_check=False,
            tail_risk_check_enabled=False,
            asset_concentration_check=False,
            allocation_policy=_NoPolicy(),
            max_open_trades=10,
        )
        seen_caches = []

        def _fake_analyze_assets(symbols, returns_cache=None, **kw):
            seen_caches.append(returns_cache)
            return MagicMock(well_diversified=True, avg_correlation=0.1)

        hyps = [
            {"asset": "SOL-USD", "strategy": "s", "direction": "long",
             "price": 100.0, "atr_at_signal": 2.0},
            {"asset": "GLD", "strategy": "s", "direction": "long",
             "price": 200.0, "atr_at_signal": 3.0},
        ]
        state = {"generate_hypotheses": {"hypotheses": hyps}}
        with patch("src.agents.risk_agent.analyze_assets", side_effect=_fake_analyze_assets):
            risk.validate_and_size({}, state)

        self.assertEqual(len(seen_caches), 2)
        self.assertIsNotNone(seen_caches[0])
        self.assertIs(seen_caches[0], seen_caches[1])

    def test_same_tail_risk_cache_object_reused_across_hypotheses(self):
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0)],
            portfolio_stress_check=False,
            correlation_check_enabled=False,
            asset_concentration_check=False,
            allocation_policy=_NoPolicy(),
            max_open_trades=10,
        )
        seen_caches = []

        def _fake_tail_risk(weights, returns_cache=None, **kw):
            seen_caches.append(returns_cache)
            return MagicMock(n_observations=100, cvar_95=-0.05)

        hyps = [
            {"asset": "SOL-USD", "strategy": "s", "direction": "long",
             "price": 100.0, "atr_at_signal": 2.0},
            {"asset": "GLD", "strategy": "s", "direction": "long",
             "price": 200.0, "atr_at_signal": 3.0},
        ]
        state = {"generate_hypotheses": {"hypotheses": hyps}}
        with patch("src.agents.risk_agent.compute_portfolio_tail_risk", side_effect=_fake_tail_risk):
            risk.validate_and_size({}, state)

        self.assertEqual(len(seen_caches), 2)
        self.assertIsNotNone(seen_caches[0])
        self.assertIs(seen_caches[0], seen_caches[1])

    def test_correlation_and_tail_risk_caches_are_different_objects_in_cycle(self):
        risk = _make_risk(
            opens=[_make_pos("BTC-USD", 50.0), _make_pos("ETH-USD", 50.0)],
            portfolio_stress_check=False,
            asset_concentration_check=False,
            allocation_policy=_NoPolicy(),
            max_open_trades=10,
        )
        corr_seen = []
        tail_seen = []

        def _fake_analyze_assets(symbols, returns_cache=None, **kw):
            corr_seen.append(returns_cache)
            return MagicMock(well_diversified=True, avg_correlation=0.1)

        def _fake_tail_risk(weights, returns_cache=None, **kw):
            tail_seen.append(returns_cache)
            return MagicMock(n_observations=100, cvar_95=-0.05)

        hyps = [{"asset": "SOL-USD", "strategy": "s", "direction": "long",
                 "price": 100.0, "atr_at_signal": 2.0}]
        state = {"generate_hypotheses": {"hypotheses": hyps}}
        with patch("src.agents.risk_agent.analyze_assets", side_effect=_fake_analyze_assets), \
             patch("src.agents.risk_agent.compute_portfolio_tail_risk", side_effect=_fake_tail_risk):
            risk.validate_and_size({}, state)

        self.assertEqual(len(corr_seen), 1)
        self.assertEqual(len(tail_seen), 1)
        self.assertIsNot(corr_seen[0], tail_seen[0])

    def test_direct_gate_call_without_validate_and_size_does_not_raise(self):
        """Defensive getattr(..., None): calling the gates directly
        (as many existing Sprint 45 tests do, without first calling
        validate_and_size) must not raise AttributeError just because
        the per-cycle cache attributes haven't been created yet."""
        risk = _make_risk(opens=[_make_pos("BTC-USD", 50.0), _make_pos("ETH-USD", 50.0)])
        fake_result = MagicMock(well_diversified=True, avg_correlation=0.1)
        with patch("src.agents.risk_agent.analyze_assets", return_value=fake_result):
            ok, _ = risk._check_portfolio_correlation("SOL-USD", 10.0)
        self.assertTrue(ok)


# ============================================================
# Part 1: fast_monitor_tick decoupled onto its own thread
# ============================================================

class MainPySourceWiringTest(unittest.TestCase):
    """Static checks against main.py's source, so a future refactor
    can't silently re-merge fast_monitor_tick back onto the shared
    `schedule` instance without this test failing."""

    @classmethod
    def setUpClass(cls):
        # Sprint 46T (audit M6): the fast-monitor thread moved from
        # main.py into src/runtime/bot_runtime.py when the runtime
        # class was extracted. Concatenate both so the static-source
        # tests below keep catching the patterns the audit cared about
        # (own thread, own lock, NOT on the shared `schedule` instance,
        # and only spun up in daemon mode — not in --once).
        paths = [
            os.path.join(ROOT, "main.py"),
            os.path.join(ROOT, "src", "runtime", "bot_runtime.py"),
        ]
        cls.src = "\n".join(open(p, encoding="utf-8").read() for p in paths)
        cls.main_only_src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        cls.runtime_src = open(
            os.path.join(ROOT, "src", "runtime", "bot_runtime.py"),
            encoding="utf-8",
        ).read()

    def test_fast_monitor_has_its_own_thread(self):
        # Pattern is in bot_runtime.py after the 46T extraction.
        self.assertIn("_fast_monitor_thread = threading.Thread(", self.runtime_src)
        self.assertIn("target=self._fast_monitor_loop", self.runtime_src)
        self.assertIn("daemon=True", self.runtime_src)

    def test_fast_monitor_no_longer_shares_schedule_instance(self):
        # The old Sprint 46I wiring registered fast_monitor_tick
        # directly on the shared `schedule` object; that line must be
        # gone (in EITHER file) now that it has its own thread/timer.
        for label, src in (("main.py", self.main_only_src),
                            ("bot_runtime.py", self.runtime_src)):
            self.assertNotIn(
                "schedule.every(_fast_monitor_minutes).minutes.do(fast_monitor_tick)",
                src,
                f"schedule.every(...).do(fast_monitor_tick) still in {label}",
            )

    def test_fast_monitor_thread_guarded_by_lock(self):
        self.assertIn("_fast_monitor_lock = threading.Lock()", self.runtime_src)
        self.assertIn("_fast_monitor_lock.acquire(blocking=False)", self.runtime_src)

    def test_thread_only_started_outside_once_mode(self):
        # Sprint 46T (audit M6): in bot_runtime.py, the thread is
        # started inside `_start_fast_monitor_thread()` which is
        # called from `run()` only when `not self.once`. The
        # construction site is many lines below the gate (the
        # `_start_fast_monitor_thread` method has a long docstring),
        # so we check the CALL site (`self._start_fast_monitor_thread()`)
        # rather than the construction site.
        self.assertIn("if not self.once:", self.runtime_src)
        idx = self.runtime_src.index("self._start_fast_monitor_thread()")
        # Walk backward to the nearest `if` statement above the call
        # (the gate). Look at up to 400 chars of context to cross the
        # method boundary if needed.
        preceding = self.runtime_src[max(0, idx - 400):idx]
        self.assertIn("if not self.once:", preceding,
                      "thread start must be gated on `if not self.once:`")


class NonBlockingLockSkipPatternTest(unittest.TestCase):
    """Free-standing reproduction of the exact concurrency pattern
    main.py's `_fast_monitor_loop` uses (non-blocking lock acquire;
    skip this tick if a previous one is still running) -- proves the
    pattern itself is correct: a slow-running tick does not block a
    fast-running one from being *attempted*, and overlapping runs of
    the guarded body never happen concurrently.
    """

    def test_slow_tick_does_not_block_fast_ticks_from_being_attempted(self):
        lock = threading.Lock()
        attempts = []
        max_concurrent = {"n": 0, "peak": 0}
        guard = threading.Lock()

        def guarded_tick(tick_id, duration):
            if not lock.acquire(blocking=False):
                attempts.append((tick_id, "skipped"))
                return
            try:
                with guard:
                    max_concurrent["n"] += 1
                    max_concurrent["peak"] = max(max_concurrent["peak"], max_concurrent["n"])
                time.sleep(duration)
                attempts.append((tick_id, "ran"))
            finally:
                with guard:
                    max_concurrent["n"] -= 1
                lock.release()

        # Start a "slow" tick that holds the lock for a while...
        t_slow = threading.Thread(target=guarded_tick, args=("slow", 0.3))
        t_slow.start()
        time.sleep(0.05)  # let it acquire the lock first

        # ...then attempt several "fast" ticks while it's still running.
        fast_threads = [
            threading.Thread(target=guarded_tick, args=(f"fast-{i}", 0.01))
            for i in range(3)
        ]
        for t in fast_threads:
            t.start()
        for t in fast_threads:
            t.join()
        t_slow.join()

        # None of the fast ticks should have run concurrently with the
        # slow one -- they must all have been skipped.
        skipped = [a for a in attempts if a[1] == "skipped"]
        self.assertEqual(len(skipped), 3)
        self.assertEqual(max_concurrent["peak"], 1)

    def test_ticks_run_sequentially_once_lock_is_free(self):
        lock = threading.Lock()
        results = []

        def guarded_tick(tick_id):
            if not lock.acquire(blocking=False):
                results.append((tick_id, "skipped"))
                return
            try:
                results.append((tick_id, "ran"))
            finally:
                lock.release()

        for i in range(5):
            guarded_tick(i)
        self.assertTrue(all(r[1] == "ran" for r in results))


if __name__ == "__main__":
    unittest.main()
