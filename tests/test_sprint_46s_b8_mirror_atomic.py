"""
Sprint 46S tests — audit follow-up (audit_2026-07-12_torvalds.md #1).

Sprint 46R (audit B8) fixed non-atomic writes in `data_store/positions.json`
and centralized the tmp+fsync+rename pattern in
`src.core.atomic_write.atomic_write_text`. But the SAME `_save()` call
also writes a "mirror" copy to `audit/positions.json` so the dashboard
container (which doesn't share `data_store/`) can read the bot's open
positions. Pre-Sprint-46S, that mirror write used a plain
`Path.write_text()` -- no tmp file, no fsync, no atomic rename.

Failure mode the fix closes: if the bot is OOM-killed / sent SIGTERM
mid-_save() while the mirror is being written, the dashboard reads a
truncated JSON file and silently displays 0 open positions. The primary
`data_store/positions.json` (which the bot itself reads on restart) was
already crash-safe; this fix makes the dashboard view equally safe.

Regression: these tests lock down the contract that the mirror write
goes through `atomic_write_text`, not `Path.write_text` directly.
If someone reverts the fix to use `Path.write_text` again, these
tests will catch it.

Run: python -m unittest tests.test_sprint_46s_b8_mirror_atomic -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.core import atomic_write as atomic_write_mod  # noqa: E402
from src.data_store import positions as positions_mod  # noqa: E402
from src.data_store.positions import Position, PositionRepository  # noqa: E402


class MirrorAtomicWriteTest(unittest.TestCase):
    def setUp(self):
        # The mirror path is hard-coded as a relative path
        # `Path("audit/positions.json")` inside _save(). chdir into a
        # temp dir so the mirror lands in <tmpdir>/audit/positions.json
        # instead of overwriting the real one during the test run.
        self._orig_cwd = os.getcwd()
        self.tmpdir = tempfile.mkdtemp()
        os.chdir(self.tmpdir)

        self.primary = os.path.join(self.tmpdir, "positions.json")
        self.mirror_dir = os.path.join(self.tmpdir, "audit")
        self.mirror = os.path.join(self.mirror_dir, "positions.json")

    def tearDown(self):
        os.chdir(self._orig_cwd)
        # Best-effort cleanup; mkdtemp already created a unique dir
        # but chdir-ing back keeps the real audit/ untouched.

    def _make_repo_with_one_position(self) -> PositionRepository:
        repo = PositionRepository(path=self.primary)
        repo.add_open(Position(
            asset="BTC",
            direction="long",
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            qty=0.01,
            risk_usd=10.0,
            entry_ts=1700000000.0,
            strategy="momentum_v1",
        ))
        return repo

    def test_mirror_file_is_written_on_save(self):
        """Sanity: after a _save() the mirror file exists at
        audit/positions.json and contains valid JSON."""
        repo = self._make_repo_with_one_position()

        self.assertTrue(
            os.path.exists(self.mirror),
            f"mirror file missing at {self.mirror} -- _save() did not "
            f"write the dashboard copy",
        )
        with open(self.mirror, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("positions", data)
        self.assertEqual(len(data["positions"]), 1)
        self.assertEqual(data["positions"][0]["asset"], "BTC")

    def test_mirror_content_matches_primary(self):
        """The dashboard sees the same data the bot has on disk."""
        repo = self._make_repo_with_one_position()

        with open(self.primary, "r", encoding="utf-8") as f:
            primary_data = json.load(f)
        with open(self.mirror, "r", encoding="utf-8") as f:
            mirror_data = json.load(f)
        # saved_at can differ by a millisecond between the two writes,
        # but the positions list must be byte-identical.
        self.assertEqual(
            mirror_data["positions"],
            primary_data["positions"],
            "mirror positions diverged from primary",
        )

    def test_mirror_write_goes_through_atomic_write_text(self):
        """Contract: the mirror write MUST use atomic_write_text, not
        raw Path.write_text. If this test fails, someone reverted the
        Sprint 46S fix to use the non-atomic Path.write_text pattern,
        re-introducing the dashboard-corruption failure mode."""
        repo = self._make_repo_with_one_position()

        with mock.patch.object(
            positions_mod, "atomic_write_text",
            wraps=positions_mod.atomic_write_text,
        ) as spy:
            # Trigger another _save via close_position -- if the fix
            # is in place, atomic_write_text is called for BOTH the
            # primary path AND the mirror path.
            open_pos = next(p for p in repo.positions if p.is_open)
            repo.close_position(open_pos.position_id, 51000.0, "TP_HIT")

        called_paths = [
            call.args[0] for call in spy.call_args_list
            if call.args
        ]
        # Normalize: spy may receive str OR Path; the mirror is
        # constructed in positions.py as a *relative* Path
        # (`Path("audit/positions.json")`) so it shows up here as
        # the literal string "audit/positions.json" regardless of
        # cwd -- compare the tail of the path, not the absolute form.
        called_paths_str = [str(p).replace("\\", "/") for p in called_paths]
        primary_normalized = str(self.primary).replace("\\", "/")
        mirror_normalized = self.mirror.replace("\\", "/")
        self.assertIn(
            primary_normalized, called_paths_str,
            f"atomic_write_text was not called for the primary file "
            f"(expected {primary_normalized}, saw {called_paths_str})",
        )
        # The mirror in _save() is hard-coded as Path("audit/positions.json"),
        # so it always shows up as the string "audit/positions.json" in
        # the call args (relative form, not resolved against cwd).
        self.assertIn(
            "audit/positions.json", called_paths_str,
            f"atomic_write_text was not called for the mirror file -- "
            f"Sprint 46S fix may have regressed to Path.write_text. "
            f"Calls seen: {called_paths_str}",
        )
        # Belt-and-suspenders: also confirm the resolved mirror path
        # exists on disk, in case the fix path changes later.
        self.assertTrue(
            os.path.exists(mirror_normalized),
            f"mirror file missing at {mirror_normalized}",
        )

    def test_mirror_write_failure_does_not_corrupt_primary(self):
        """If the mirror write itself fails (disk full on /app/audit,
        permission denied, etc.), the primary file MUST still be on
        disk and intact -- the bot must not be allowed to die because
        the dashboard's view failed to update."""
        # Simulate a disk-full on the mirror by making the audit dir
        # non-writable. The primary file is in self.tmpdir, NOT under
        # audit/, so it should still write successfully.
        os.makedirs(self.mirror_dir, exist_ok=True)
        os.chmod(self.mirror_dir, 0o555)  # read+execute only, no write

        try:
            repo = self._make_repo_with_one_position()
        finally:
            os.chmod(self.mirror_dir, 0o755)  # restore for tearDown

        # The bot's primary state is still good:
        self.assertTrue(
            os.path.exists(self.primary),
            "primary file was lost when mirror write failed -- "
            "the mirror exception must be caught",
        )
        with open(self.primary, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["positions"]), 1)
        self.assertEqual(data["positions"][0]["asset"], "BTC")

    def test_no_stray_tmp_left_in_audit_dir_after_save(self):
        """Atomic writes use <file>.tmp as a staging area. A successful
        _save() must clean it up -- otherwise the next _save() that
        also crashes (or the next boot's load) sees a stale .tmp
        alongside the canonical file."""
        repo = self._make_repo_with_one_position()
        tmp_path = self.mirror + ".tmp"
        self.assertFalse(
            os.path.exists(tmp_path),
            f"stray .tmp survived a successful _save(): {tmp_path}",
        )


if __name__ == "__main__":
    unittest.main()
