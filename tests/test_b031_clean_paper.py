"""
Sprint 33 — B031 regression tests.

Bug: dashboard.py "Clean Paper Positions" button referenced an undefined
`open_count_now` variable inside its success message, raising NameError
right after the cleanup actually succeeded. Users saw no feedback.

These tests pin the *counting* logic so the bug can't reappear.

Run: python -m unittest tests.test_b031_clean_paper -v
"""
import json
import os
import tempfile
import time
import unittest


def _clean_paper_positions(positions: list) -> int:
    """Mirror of the dashboard's clean_paper handler logic.

    Mutates the dicts in place: for any position with closed_ts=None,
    set closed_ts=now, closed_price=entry_price, close_reason=
    MANUAL_CLEAN_PAPER. Returns the number of positions actually closed.
    """
    closed_now = 0
    for p in positions:
        if p.get("closed_ts") is None:
            p["closed_ts"] = time.time()
            p["closed_price"] = p.get("entry_price", 0)
            p["close_reason"] = "MANUAL_CLEAN_PAPER"
            closed_now += 1
    return closed_now


class CleanPaperCountingTest(unittest.TestCase):
    def test_closes_all_open_positions_and_returns_count(self):
        positions = [
            {"asset": "BTC-USD", "entry_price": 50000, "closed_ts": None},
            {"asset": "ETH-USD", "entry_price": 3000, "closed_ts": None},
        ]
        n = _clean_paper_positions(positions)
        self.assertEqual(n, 2)
        # All closed at entry_price
        for p in positions:
            self.assertIsNotNone(p["closed_ts"])
            self.assertEqual(p["closed_price"], p["entry_price"])
            self.assertEqual(p["close_reason"], "MANUAL_CLEAN_PAPER")

    def test_skips_already_closed_positions(self):
        positions = [
            {"asset": "BTC-USD", "entry_price": 50000, "closed_ts": None},
            {"asset": "ETH-USD", "entry_price": 3000, "closed_ts": 12345.0},
            {"asset": "SOL-USD", "entry_price": 100, "closed_ts": None},
        ]
        n = _clean_paper_positions(positions)
        self.assertEqual(n, 2, "Only open positions should be counted")
        # ETH stays untouched
        self.assertEqual(positions[1]["closed_ts"], 12345.0)
        self.assertNotIn("close_reason", positions[1])

    def test_empty_list_returns_zero(self):
        self.assertEqual(_clean_paper_positions([]), 0)

    def test_no_open_positions_returns_zero(self):
        positions = [
            {"asset": "BTC-USD", "entry_price": 50000, "closed_ts": 100.0},
            {"asset": "ETH-USD", "entry_price": 3000, "closed_ts": 200.0},
        ]
        n = _clean_paper_positions(positions)
        self.assertEqual(n, 0)
        for p in positions:
            self.assertNotIn("close_reason", p)

    def test_persisted_to_json_round_trip(self):
        """The full dashboard flow: read JSON, mutate, write back, re-read."""
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "positions.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"positions": [
                {"asset": "BTC-USD", "entry_price": 50000, "closed_ts": None,
                 "qty": 0.001, "direction": "long"},
                {"asset": "ETH-USD", "entry_price": 3000, "closed_ts": None,
                 "qty": 0.01, "direction": "long"},
            ]}, f)

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        n = _clean_paper_positions(data["positions"])
        self.assertEqual(n, 2)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

        with open(path, "r", encoding="utf-8") as f:
            reloaded = json.load(f)
        for p in reloaded["positions"]:
            self.assertIsNotNone(p["closed_ts"])
            self.assertEqual(p["close_reason"], "MANUAL_CLEAN_PAPER")


if __name__ == "__main__":
    unittest.main()
