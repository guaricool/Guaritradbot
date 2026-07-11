"""
Sprint 46N tests — audit finding C7 (AUDITORIA_COMPLETA_2026-07-11.md).

C7: `PositionRepository._load()` caught ANY exception from a corrupt/
unparseable `positions.json` (bad JSON, a dataclass field mismatch,
etc.) with nothing but a `print()` — `self.positions` stayed at its
initial empty list, and the very next write (triggered by any
`add_open`/`close_position` call, via the atomic tmp+replace pattern
in `_save()`) would overwrite the corrupt file with that empty state,
permanently destroying whatever position history was in it with zero
chance of recovery.

Fix: a corrupt file is now quarantined (its raw bytes copied to a
timestamped `<name>.corrupt-<epoch>` file) BEFORE any write can touch
the original, and `self.load_error` / `self.quarantined_path` are set
so callers (main.py's startup sequence, in this repo) can surface the
failure loudly instead of silently continuing.

Run: python -m unittest tests.test_sprint_46n_c7_quarantine_corrupt_positions -v
"""
import glob
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data_store.positions import Position, PositionRepository


class QuarantineCorruptPositionsTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "positions.json")

    def test_missing_file_is_not_an_error(self):
        """No file at all (first-ever run) must NOT be treated as
        corruption -- that's the normal cold-start case."""
        repo = PositionRepository(path=self.path)
        self.assertIsNone(repo.load_error)
        self.assertIsNone(repo.quarantined_path)
        self.assertEqual(repo.positions, [])

    def test_valid_file_loads_without_quarantine(self):
        repo = PositionRepository(path=self.path)
        repo.add_open(Position(
            asset="BTC-USD", direction="long", entry_price=50000.0,
            stop_loss=49000.0, take_profit=52000.0, qty=0.001,
            risk_usd=10.0, entry_ts=1000.0, strategy="test",
        ))
        # Re-open a fresh repo against the same (valid) file.
        repo2 = PositionRepository(path=self.path)
        self.assertIsNone(repo2.load_error)
        self.assertIsNone(repo2.quarantined_path)
        self.assertEqual(len(repo2.positions), 1)

    def test_corrupt_json_is_quarantined_not_silently_dropped(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{this is not valid json!!!")

        repo = PositionRepository(path=self.path)

        self.assertIsNotNone(repo.load_error)
        self.assertEqual(repo.positions, [])
        self.assertIsNotNone(repo.quarantined_path)
        self.assertTrue(os.path.exists(repo.quarantined_path))
        # The quarantine file must contain the ORIGINAL corrupt bytes,
        # not something re-derived/sanitized.
        with open(repo.quarantined_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "{this is not valid json!!!")

    def test_corrupt_file_survives_a_subsequent_save(self):
        """The core regression: before the fix, the corrupt file's
        DATA was lost forever the moment anything triggered a save
        (add_open/close_position), because _save() overwrites
        self.path via atomic tmp+replace with whatever self.positions
        currently holds (empty, in the corruption case). The
        quarantine copy must survive that overwrite."""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"positions": [BROKEN')

        repo = PositionRepository(path=self.path)
        quarantine_path = repo.quarantined_path
        self.assertIsNotNone(quarantine_path)

        # Trigger a save -- this overwrites self.path, same as it
        # always did, but must NOT touch the quarantine copy.
        repo.add_open(Position(
            asset="SPY", direction="long", entry_price=500.0,
            stop_loss=490.0, take_profit=520.0, qty=1.0,
            risk_usd=10.0, entry_ts=1000.0, strategy="test",
        ))

        self.assertTrue(os.path.exists(quarantine_path))
        with open(quarantine_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"positions": [BROKEN')
        # self.path itself now has the NEW state (expected -- that's
        # normal operation going forward, not the bug).
        with open(self.path, "r", encoding="utf-8") as f:
            new_data = json.load(f)
        self.assertEqual(len(new_data["positions"]), 1)

    def test_corrupt_dataclass_fields_also_quarantined(self):
        """Valid JSON, but positions entries with fields that don't
        match the Position dataclass (e.g. after a schema change) must
        also be caught and quarantined -- not just JSON syntax errors."""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"positions": [{"totally_unexpected_field": 1}]}, f)

        repo = PositionRepository(path=self.path)
        self.assertIsNotNone(repo.load_error)
        self.assertIsNotNone(repo.quarantined_path)
        self.assertEqual(repo.positions, [])

    def test_quarantine_filename_is_unique_per_path(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not json")
        repo = PositionRepository(path=self.path)
        self.assertTrue(
            os.path.basename(repo.quarantined_path).startswith("positions.json.corrupt-")
        )

    def tearDown(self):
        for f in glob.glob(os.path.join(self.tmpdir, "*")):
            try:
                os.remove(f)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
