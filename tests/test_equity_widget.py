"""
Sprint 24 tests — Live Equity Tracker Dashboard widget.

Tests the equity widget's data preparation and display logic.
The Streamlit UI itself is hard to test in CI, so we test the
underlying data structures and the helper that reads the persisted state.
"""
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.safety.equity_tracker import (
    EquityTracker, EquitySnapshot, persist_tracker, load_tracker,
)


class EquityWidgetDataTest(unittest.TestCase):
    """Verify the data shape that the dashboard widget consumes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "equity_state.json")
        self.tracker = EquityTracker(
            starting_balance=10.0, history_size=50,
        )
        # Simulate 5 cycles of evolution
        self.tracker.update({})
        self.tracker.update({})
        self.tracker.update({})
        self.tracker.update({})
        self.tracker.update({})

    def test_persisted_state_has_required_fields(self):
        persist_tracker(self.tracker, self.path)
        with open(self.path) as f:
            data = json.load(f)
        # Dashboard widget reads these
        self.assertIn("history", data)
        self.assertIn("starting_balance", data)
        self.assertIn("max_equity", data)
        # Each snapshot in history has these
        for snap in data["history"]:
            self.assertIn("total_equity", snap)
            self.assertIn("delta_usd", snap)
            self.assertIn("delta_pct", snap)
            self.assertIn("realized_pnl", snap)
            self.assertIn("unrealized_pnl", snap)
            self.assertIn("drawdown_pct", snap)
            self.assertIn("open_positions", snap)
            self.assertIn("closed_positions", snap)
            self.assertIn("iso", snap)
            self.assertIn("timestamp", snap)

    def test_equity_series_parsing(self):
        """The widget reads _eq_series for the sparkline."""
        persist_tracker(self.tracker, self.path)
        with open(self.path) as f:
            data = json.load(f)
        history = data["history"]
        # Dashboard takes last 50
        series = [float(s.get("total_equity", 0.0)) for s in history[-50:]]
        self.assertEqual(len(series), len(history))
        # All values should be floats
        for v in series:
            self.assertIsInstance(v, float)

    def test_latest_snapshot_extraction(self):
        """The widget reads the last snapshot for the big number."""
        persist_tracker(self.tracker, self.path)
        with open(self.path) as f:
            data = json.load(f)
        latest = data["history"][-1]
        # Widget reads these directly
        self.assertIn("total_equity", latest)
        self.assertIn("delta_usd", latest)
        self.assertIn("delta_pct", latest)
        self.assertIn("realized_pnl", latest)
        self.assertIn("unrealized_pnl", latest)
        self.assertIn("drawdown_pct", latest)
        self.assertIn("iso", latest)


class EquityColorLogicTest(unittest.TestCase):
    """Test the positive/negative color logic the widget uses."""

    def test_positive_when_delta_positive(self):
        snap = EquitySnapshot(
            timestamp=0, iso="x",
            starting_balance=10.0,
            realized_pnl=0.5, unrealized_pnl=0.0,
            total_equity=10.5, delta_usd=0.5, delta_pct=5.0,
            open_positions=0, closed_positions=1,
            drawdown_usd=0.0, drawdown_pct=0.0,
        )
        # Widget logic: emoji = "🟢" if _eq_delta >= 0 else "🔴"
        self.assertEqual("🟢" if snap.delta_usd >= 0 else "🔴", "🟢")
        self.assertEqual("equity-positive" if snap.delta_usd >= 0 else "equity-negative", "equity-positive")

    def test_negative_when_delta_negative(self):
        snap = EquitySnapshot(
            timestamp=0, iso="x",
            starting_balance=10.0,
            realized_pnl=-0.3, unrealized_pnl=0.0,
            total_equity=9.7, delta_usd=-0.3, delta_pct=-3.0,
            open_positions=0, closed_positions=1,
            drawdown_usd=-0.3, drawdown_pct=-3.0,
        )
        self.assertEqual("🟢" if snap.delta_usd >= 0 else "🔴", "🔴")
        self.assertEqual("equity-positive" if snap.delta_usd >= 0 else "equity-negative", "equity-negative")


if __name__ == "__main__":
    unittest.main()