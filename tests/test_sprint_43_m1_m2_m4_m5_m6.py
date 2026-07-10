"""
Sprint 43 — M1, M2, M4, M5, M6 bundled tests.

M1: AuditLedger uses flock for cross-process safety.
M2: AlpacaBroker.create_market_order uses ONE read of mode_override
    per call (no TOCTOU between the read and the client selection).
M4: BacktestEngine removed (was dead code; M3 + M4 moot after removal).
M5: data_validator validates monotonic + unique index + staleness.
M6: market_analyst.fetch_one resamples 60m → 4h correctly.
"""
import os
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.audit_ledger import AuditLedger
from src.core.data_validator import validate_dataframe, DataIntegrityError


class M1AuditLedgerLockTest(unittest.TestCase):
    """M1: AuditLedger.append must take a file lock (POSIX) or
    fall back gracefully on platforms without fcntl (Windows).
    The test uses separate AuditLedger instances per thread
    (mimicking the bot + dashboard containers in production)."""

    def test_concurrent_writes_dont_corrupt(self):
        """Two writers → no corrupted JSONL lines."""
        import threading
        # Create the file first
        path = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
        Path(path).touch()
        errors = []
        def writer(tag, n):
            try:
                # Each thread has its own AuditLedger instance
                # (simulating two containers with the same volume).
                ledger = AuditLedger(path)
                for i in range(n):
                    ledger.append(tag, {"i": i, "tag": tag})
            except Exception as e:
                errors.append(e)
        t1 = threading.Thread(target=writer, args=("A", 15))
        t2 = threading.Thread(target=writer, args=("B", 15))
        t1.start(); t2.start()
        t1.join(); t2.join()
        # Every line must be valid JSON
        n_ok = 0
        n_bad = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                    n_ok += 1
                except json.JSONDecodeError:
                    n_bad += 1
        self.assertEqual(n_bad, 0, f"Got {n_bad} corrupted lines out of {n_ok + n_bad}")
        self.assertEqual(n_ok, 30, f"Expected 30 events, got {n_ok}")
        self.assertEqual(len(errors), 0, f"Writer threads raised: {errors}")

    def test_ledger_returns_event_dict(self):
        """Smoke test: append returns the event dict."""
        path = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
        ledger = AuditLedger(path)
        ev = ledger.append("TEST", {"foo": "bar"})
        self.assertEqual(ev["event_type"], "TEST")
        self.assertEqual(ev["foo"], "bar")
        self.assertIn("ts", ev)
        self.assertIn("iso", ev)


class M2AlpacaBrokerTOCTOUTest(unittest.TestCase):
    """M2: AlpacaBroker.create_market_order must read the paper
    flag exactly once per call. The fix uses self._paper_client /
    self._live_client (chosen from one read), not self._client()
    (which re-reads the file). We verify by counting the calls
    to the internal _alpaca_paper_mode helper."""

    def test_only_one_paper_mode_read_per_create_market_order(self):
        """The fix: the body of create_market_order calls
        _alpaca_paper_mode exactly once. The old code called it
        twice (once for is_paper, once via self._client()).
        We verify by reading the source — a more robust test
        than a behavioral mock would be.
        """
        import inspect
        from src.execution.alpaca_broker import AlpacaBroker
        source = inspect.getsource(AlpacaBroker.create_market_order)
        # Count occurrences of _alpaca_paper_mode
        n = source.count("_alpaca_paper_mode")
        # Expectation: 1 occurrence (the `is_paper = self._alpaca_paper_mode()` call)
        # + 1 in the docstring if any
        # The body should NOT call self._client() (which would add another read)
        self.assertNotIn(
            "self._client()", source,
            "create_market_order must NOT call self._client() (re-reads mode_override.json)",
        )
        self.assertEqual(
            n, 1,
            f"create_market_order should reference _alpaca_paper_mode exactly once "
            f"(the is_paper read), got {n}. The fix's whole point is to read the "
            f"file exactly once per order to avoid TOCTOU between paper↔live toggle.",
        )


class M4BacktestEngineRemovedTest(unittest.TestCase):
    """M4: BacktestEngine was the file with the silent 0-size
    position bug. It's been removed (dead code) — both M3 and
    M4 audit findings are moot. The live backtester is
    VectorizedBacktester (src/optimization/backtester.py) which
    already handles commission + slippage correctly."""

    def test_backtest_engine_does_not_exist(self):
        import os
        self.assertFalse(
            os.path.exists("src/backtest/engine.py"),
            "src/backtest/engine.py should be removed (M3/M4 moot)",
        )


class M5DataValidatorMonotonicTest(unittest.TestCase):
    """M5: validate_dataframe must reject non-monotonic, duplicate
    timestamps, and stale data."""

    def _df(self, n=10, days_offset=0):
        # Use a date close to "now" so staleness tests work
        return pd.DataFrame({
            "Open": [100.0] * n,
            "High": [101.0] * n,
            "Low":  [99.0] * n,
            "Close": [100.0] * n,
        }, index=pd.date_range(pd.Timestamp.now() - pd.Timedelta(days=days_offset + 5),
                                periods=n, freq="1D"))

    def test_monotonic_index_passes(self):
        df = self._df()
        result = validate_dataframe(df)
        self.assertIsNotNone(result)

    def test_non_monotonic_index_rejected(self):
        df = self._df()
        # Reverse the index — no longer monotonic
        df = df.iloc[::-1]
        with self.assertRaises(DataIntegrityError) as ctx:
            validate_dataframe(df)
        self.assertIn("monotonically", str(ctx.exception))

    def test_duplicate_index_rejected(self):
        df = self._df()
        # Force a duplicate timestamp
        new_idx = list(df.index)
        new_idx[5] = new_idx[4]
        df.index = pd.DatetimeIndex(new_idx)
        with self.assertRaises(DataIntegrityError) as ctx:
            validate_dataframe(df)
        self.assertIn("duplicate", str(ctx.exception))

    def test_staleness_check_passes_when_fresh(self):
        df = self._df()
        result = validate_dataframe(df, max_staleness_seconds=86400)
        self.assertIsNotNone(result)

    def test_staleness_check_rejects_old_data(self):
        # Build df where the last bar is > 86400s old
        old_idx = pd.date_range(pd.Timestamp.now() - pd.Timedelta(days=30),
                                 periods=10, freq="1D")
        df = pd.DataFrame({
            "Open": [100.0] * 10, "High": [101.0] * 10,
            "Low": [99.0] * 10, "Close": [100.0] * 10,
        }, index=old_idx)
        with self.assertRaises(DataIntegrityError) as ctx:
            validate_dataframe(df, max_staleness_seconds=86400)
        self.assertIn("staleness", str(ctx.exception))

    def test_staleness_check_skipped_when_none(self):
        """Backward compat: max_staleness_seconds=None skips the check."""
        old_idx = pd.date_range(pd.Timestamp.now() - pd.Timedelta(days=30),
                                 periods=10, freq="1D")
        df = pd.DataFrame({
            "Open": [100.0] * 10, "High": [101.0] * 10,
            "Low": [99.0] * 10, "Close": [100.0] * 10,
        }, index=old_idx)
        # No max_staleness_seconds → no check
        result = validate_dataframe(df)
        self.assertIsNotNone(result)


class M6FetchOneResampleTest(unittest.TestCase):
    """M6: market_analyst.fetch_one('4h') must resample 60m→4h."""

    def test_fetch_one_4h_calls_resample(self):
        from src.agents.market_analyst import MarketAnalystAgent, _resample_ohlcv
        from src.agents import market_analyst as ma_mod
        ma = MarketAnalystAgent()
        # Build a fake 60m dataframe
        n_60m = 100
        df_60m = pd.DataFrame({
            "Open": [100.0 + 0.1 * i for i in range(n_60m)],
            "High": [101.0 + 0.1 * i for i in range(n_60m)],
            "Low":  [99.0 + 0.1 * i for i in range(n_60m)],
            "Close": [100.0 + 0.1 * i for i in range(n_60m)],
            "Volume": [1000.0] * n_60m,
        }, index=pd.date_range(pd.Timestamp.now(), periods=n_60m, freq="1h"))
        # Patch safe_yf_download to return our fake 60m data
        original = ma_mod.safe_yf_download
        ma_mod.safe_yf_download = lambda *a, **kw: df_60m
        try:
            # Spy on the module-level _resample_ohlcv function
            with patch.object(ma_mod, "_resample_ohlcv", wraps=_resample_ohlcv) as spy:
                result = ma.fetch_one("BTC-USD", interval="4h", period="1mo")
                self.assertIsNotNone(result)
                spy.assert_called_once()
        finally:
            ma_mod.safe_yf_download = original


if __name__ == "__main__":
    unittest.main()
