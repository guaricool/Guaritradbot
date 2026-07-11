"""
Sprint 46N tests — audit finding C8 (AUDITORIA_COMPLETA_2026-07-11.md).

C8: dashboard API mutations (`close_position`, `close_all_positions`)
each constructed their OWN fresh `PositionRepository(path=...)` --
read whatever was on disk, closed the target position on that
throwaway copy, and saved. The bot's own long-lived in-memory
PositionRepository (constructed once in main.py) never learned about
that close -- it still held the position as OPEN in its `self.
positions` list. The next time ANYTHING triggered the bot's own
`_save()` (opening a new position, `fast_monitor_tick` closing a
DIFFERENT position, an OCO reconciliation, etc.), it overwrote
`positions.json` with its stale in-memory state -- silently
"resurrecting" the position the dashboard had just closed.

Fix: `src.api.state.set_position_repo()` registers the bot's live
PositionRepository instance; `get_position_repo()` returns it if set,
else falls back to a fresh disk-backed instance (tests / no live bot).
`state.close_position`/`close_all_positions`/`build_state_snapshot`
and every `PositionRepository(...)` construction in `server.py` now go
through `get_position_repo()`. `PositionRepository` also gained an
internal `threading.RLock` so concurrent access from the bot's
scheduler thread and the dashboard's request thread(s) is safe.

Run: python -m unittest tests.test_sprint_46n_c8_shared_repo -v
"""
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data_store.positions import Position, PositionRepository
from src.api import state as api_state


def _make_position(asset="BTC-USD", entry_price=50000.0):
    return Position(
        asset=asset, direction="long", entry_price=entry_price,
        stop_loss=entry_price * 0.98, take_profit=entry_price * 1.04,
        qty=0.001, risk_usd=10.0, entry_ts=1000.0, strategy="test",
    )


class GetPositionRepoFallbackTest(unittest.TestCase):
    """Without set_position_repo, get_position_repo must behave
    exactly as the old `PositionRepository(path=...)` call it
    replaced -- disk-backed, fresh instance, fully back-compat."""

    def setUp(self):
        api_state.set_position_repo(None)
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "positions.json")

    def tearDown(self):
        api_state.set_position_repo(None)

    def test_no_shared_repo_falls_back_to_disk(self):
        repo = PositionRepository(path=self.path)
        repo.add_open(_make_position())
        fetched = api_state.get_position_repo(self.path)
        self.assertEqual(fetched.count_open(), 1)

    def test_fallback_instances_are_independent(self):
        """Confirms the OLD (buggy) behavior when no repo is shared --
        each call to get_position_repo() without a registered shared
        instance gets its OWN copy, same as `PositionRepository(path=
        ...)` always did."""
        repo_a = api_state.get_position_repo(self.path)
        repo_b = api_state.get_position_repo(self.path)
        self.assertIsNot(repo_a, repo_b)


class ResurrectedPositionBugFixedTest(unittest.TestCase):
    """The core regression this fix addresses."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "positions.json")
        self.addCleanup(lambda: api_state.set_position_repo(None))

    def test_without_sharing_a_close_can_be_resurrected(self):
        """Demonstrates the BUG in isolation (not exercising the fix)
        -- proves the failure mode this ticket is about is real, so
        the fix below is proven to address an actual bug rather than
        a hypothetical one."""
        bot_repo = PositionRepository(path=self.path)
        pos = _make_position()
        bot_repo.add_open(pos)

        # Dashboard: a completely separate PositionRepository instance
        # reading the same file (the pre-fix behavior).
        dashboard_repo = PositionRepository(path=self.path)
        closed = dashboard_repo.close_position(
            pos.position_id, close_price=51000.0, reason="MANUAL_CLOSE_VIA_API"
        )
        self.assertIsNotNone(closed)
        self.assertEqual(bot_repo.count_open(), 1)  # bot's copy is stale

        # Bot's own scheduler later does something unrelated that
        # triggers ITS OWN _save() -- e.g. opening a new position.
        bot_repo.add_open(_make_position(asset="ETH-USD", entry_price=3000.0))

        # The dashboard's close got overwritten -- resurrected as open.
        reloaded = PositionRepository(path=self.path)
        reloaded_target = next(p for p in reloaded.all() if p.position_id == pos.position_id)
        self.assertTrue(
            reloaded_target.is_open,
            "bug reproduction failed -- position should have been resurrected",
        )

    def test_with_sharing_a_dashboard_close_is_not_resurrected(self):
        """The fix: once the bot's repo is registered via
        set_position_repo, a dashboard close (via get_position_repo)
        mutates the SAME object the bot's scheduler holds -- no second
        stale copy exists to resurrect anything."""
        bot_repo = PositionRepository(path=self.path)
        pos = _make_position()
        bot_repo.add_open(pos)

        api_state.set_position_repo(bot_repo)

        # Dashboard "close" -- now goes through the shared instance.
        dashboard_repo = api_state.get_position_repo(self.path)
        self.assertIs(dashboard_repo, bot_repo)
        closed = dashboard_repo.close_position(
            pos.position_id, close_price=51000.0, reason="MANUAL_CLOSE_VIA_API"
        )
        self.assertIsNotNone(closed)

        # Bot's own scheduler does something unrelated that triggers
        # its own _save() -- same object, so the close is already
        # reflected; this must NOT undo it.
        bot_repo.add_open(_make_position(asset="ETH-USD", entry_price=3000.0))

        reloaded = PositionRepository(path=self.path)
        reloaded_target = next(p for p in reloaded.all() if p.position_id == pos.position_id)
        self.assertFalse(
            reloaded_target.is_open,
            "position was resurrected even with the shared repo -- fix regressed",
        )

    def test_close_all_positions_uses_shared_repo(self):
        bot_repo = PositionRepository(path=self.path)
        p1 = _make_position(asset="BTC-USD")
        p2 = _make_position(asset="ETH-USD", entry_price=3000.0)
        bot_repo.add_open(p1)
        bot_repo.add_open(p2)
        api_state.set_position_repo(bot_repo)

        closed = api_state.close_all_positions(positions_path=self.path)
        self.assertEqual(len(closed), 2)
        self.assertEqual(bot_repo.count_open(), 0)


class PositionRepositoryThreadSafetyTest(unittest.TestCase):
    """Sprint 46N C8: PositionRepository must tolerate concurrent
    add_open/close_position/open() calls from multiple threads without
    raising or losing positions -- this is now a real scenario since
    the bot's scheduler thread and the dashboard's uvicorn request
    thread(s) can share one instance."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "positions.json")

    def test_concurrent_add_open_from_multiple_threads(self):
        repo = PositionRepository(path=self.path)
        n_threads = 8
        errors = []

        def worker(i):
            try:
                repo.add_open(_make_position(asset=f"ASSET{i}-USD", entry_price=100.0 + i))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(repo.count_open(), n_threads)

    def test_concurrent_close_position_is_safe(self):
        repo = PositionRepository(path=self.path)
        positions = [_make_position(asset=f"ASSET{i}-USD", entry_price=100.0 + i) for i in range(6)]
        for p in positions:
            repo.add_open(p)

        errors = []

        def closer(pid):
            try:
                repo.close_position(pid, close_price=200.0, reason="TEST")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=closer, args=(p.position_id,)) for p in positions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(repo.count_open(), 0)


if __name__ == "__main__":
    unittest.main()
