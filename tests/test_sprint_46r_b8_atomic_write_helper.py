"""
Sprint 46R (audit B8): regression tests for the atomic_write_text
helper. Audit B8 found 7 tmp+replace() call sites that lacked the
fsync() that audit_ledger.py correctly had; this helper centralizes
the pattern. The tests here lock down the contract so future edits
to atomic_write.py don't silently lose the durability properties.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.core.atomic_write import atomic_write_text


class AtomicWriteTextTest(unittest.TestCase):
    def test_writes_text_and_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "nested" / "subdir" / "file.json"
            atomic_write_text(target, '{"hello": "world"}')
            self.assertEqual(target.read_text(encoding="utf-8"),
                             '{"hello": "world"}')

    def test_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "f.json"
            target.write_text("old content", encoding="utf-8")
            atomic_write_text(target, "new content")
            self.assertEqual(target.read_text(encoding="utf-8"),
                             "new content")

    def test_no_stray_tmp_file_on_success(self):
        # The audit's C7 quarantine test was specifically about
        # a .tmp file surviving a bot crash. Make sure the
        # helper cleans it up on success too -- otherwise a
        # mid-write crash leaves .tmp behind and the *next*
        # boot's load picks up the stale (incomplete) tmp.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "f.json"
            atomic_write_text(target, "ok")
            tmp = target.with_suffix(target.suffix + ".tmp")
            self.assertFalse(tmp.exists(),
                             f"stray .tmp survived: {tmp}")

    def test_no_stray_tmp_file_on_failure(self):
        # If the write itself fails (parent dir unwritable
        # for instance), the .tmp must be cleaned up so a
        # half-written file doesn't survive to confuse the
        # next caller.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "f.json"
            # Force a failure by making the target a *directory*
            # -- open() will fail with IsADirectoryError.
            target.mkdir()
            try:
                atomic_write_text(target, "won't write")
            except (IsADirectoryError, OSError):
                pass
            tmp = target.with_suffix(target.suffix + ".tmp")
            self.assertFalse(tmp.exists(),
                             f"stray .tmp survived failure: {tmp}")

    def test_unicode_payload_preserved(self):
        # Sanity: non-ASCII payload round-trips intact. The bot
        # writes Spanish-language audit messages, so this
        # matters in production.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "f.txt"
            payload = "Sprint 46R: ñ, á, é — emojis 📈✅❌"
            atomic_write_text(target, payload)
            self.assertEqual(target.read_text(encoding="utf-8"),
                             payload)


if __name__ == "__main__":
    unittest.main()
