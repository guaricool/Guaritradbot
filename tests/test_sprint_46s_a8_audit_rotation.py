"""
Sprint 46S (audit A8) — audit.jsonl monthly rotation tests.

The audit's exact complaint: "audit.jsonl crece sin límite y se relee
completo cada segundo" — no rotation anywhere, and (at the time) the
dashboard's tail loop re-parsed the whole file every second (that half
was already fixed separately, via byte-offset tailing in server.py).
This covers the rotation half: AuditLedger._maybe_rotate(), called at
the top of every append().
"""
import json
import os
import tempfile
import time
import unittest

from src.safety.audit_ledger import AuditLedger


def _backdate(path: str, months_ago_month_str: str) -> None:
    """Set a file's mtime to some time within the given 'YYYY-MM' month,
    so AuditLedger._maybe_rotate() sees it as belonging to that month."""
    dt = time.strptime(months_ago_month_str + "-15", "%Y-%m-%d")
    ts = time.mktime(dt)
    os.utime(path, (ts, ts))


class AuditRotationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "audit.jsonl")

    def _prev_month_str(self) -> str:
        now = time.localtime()
        year, month = now.tm_year, now.tm_mon
        if month == 1:
            year -= 1
            month = 12
        else:
            month -= 1
        return f"{year:04d}-{month:02d}"

    def test_no_rotation_within_same_month(self):
        ledger = AuditLedger(self.path)
        ledger.append("BOT_START", {})
        ledger.append("BOT_START", {})
        # Still a single file, no audit-YYYY-MM.jsonl created.
        siblings = os.listdir(self.tmpdir)
        self.assertEqual(siblings, ["audit.jsonl"])
        self.assertEqual(len(ledger.read_all()), 2)

    def test_rotates_when_file_belongs_to_earlier_month(self):
        ledger = AuditLedger(self.path)
        ledger.append("BOT_START", {"note": "last month"})
        prev_month = self._prev_month_str()
        _backdate(self.path, prev_month)

        # Next append should trigger a rotation: the existing
        # (backdated) file gets archived, and the new event lands in
        # a fresh audit.jsonl.
        ledger.append("BOT_START", {"note": "this month"})

        archive_path = os.path.join(self.tmpdir, f"audit-{prev_month}.jsonl")
        self.assertTrue(os.path.exists(archive_path), "archived file should exist after rotation")

        # The archive has the OLD event only.
        with open(archive_path, "r", encoding="utf-8") as f:
            archived_rows = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(archived_rows), 1)
        self.assertEqual(archived_rows[0]["note"], "last month")

        # The live file (self.path) has ONLY the new event.
        live_rows = ledger.read_all()
        self.assertEqual(len(live_rows), 1)
        self.assertEqual(live_rows[0]["note"], "this month")

    def test_rotation_does_not_clobber_existing_archive(self):
        """If audit-YYYY-MM.jsonl already exists (e.g. a prior restart
        already rotated this month out), a second rotation must APPEND
        to it, never overwrite/lose events."""
        ledger = AuditLedger(self.path)
        prev_month = self._prev_month_str()
        archive_path = os.path.join(self.tmpdir, f"audit-{prev_month}.jsonl")

        # Pre-seed an "already rotated" archive from an earlier restart.
        with open(archive_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event_type": "BOT_START", "note": "first rotation"}) + "\n")

        ledger.append("BOT_START", {"note": "leftover before second rotation"})
        _backdate(self.path, prev_month)
        ledger.append("BOT_START", {"note": "this month, after second rotation"})

        with open(archive_path, "r", encoding="utf-8") as f:
            archived_rows = [json.loads(line) for line in f if line.strip()]
        notes = [r["note"] for r in archived_rows]
        self.assertIn("first rotation", notes)
        self.assertIn("leftover before second rotation", notes)

        live_rows = ledger.read_all()
        self.assertEqual(len(live_rows), 1)
        self.assertEqual(live_rows[0]["note"], "this month, after second rotation")

    def test_empty_file_does_not_rotate(self):
        """A freshly-touched, empty file (e.g. right after __init__)
        must not be treated as "belongs to an earlier month" just
        because nothing has ever been written to it yet."""
        ledger = AuditLedger(self.path)
        _backdate(self.path, self._prev_month_str())
        # File is empty (0 bytes) — _maybe_rotate should no-op, and
        # the first-ever append just writes normally, no archive.
        ledger.append("BOT_START", {"note": "first ever event"})
        siblings = sorted(os.listdir(self.tmpdir))
        self.assertEqual(siblings, ["audit.jsonl"])

    def test_rotation_failure_does_not_block_append(self):
        """A rotation hiccup (e.g. a permissions error renaming the
        file) must never prevent the actual event from being written —
        best-effort, matching every other safety-net pattern in this
        codebase."""
        ledger = AuditLedger(self.path)
        ledger.append("BOT_START", {"note": "last month"})
        _backdate(self.path, self._prev_month_str())

        original_rename = os.rename

        def _boom(*args, **kwargs):
            raise OSError("simulated rename failure")

        import src.safety.audit_ledger as audit_ledger_module
        audit_ledger_module.os.rename = _boom
        try:
            event = ledger.append("BOT_START", {"note": "should still be written"})
        finally:
            audit_ledger_module.os.rename = original_rename

        self.assertEqual(event["note"], "should still be written")
        # Rotation failed, so both events end up in the same
        # (un-rotated) file — degraded but not data-losing.
        rows = ledger.read_all()
        notes = [r["note"] for r in rows]
        self.assertIn("should still be written", notes)


if __name__ == "__main__":
    unittest.main()
