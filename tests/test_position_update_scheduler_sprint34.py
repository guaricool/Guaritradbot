"""
Sprint 34 tests — PositionUpdateScheduler.

Verifies:
  - Per-position cadence: only emits POSITION_UPDATE after interval
    elapses for THAT position
  - Per-position independence: position A's clock doesn't affect B
  - Symbol variants resolution (BTC-USD ↔ BTCUSDT etc.)
  - min_pnl_usd threshold (skip dust)
  - clear_position() / clear_all() lifecycle
  - Disabled (interval=0) emits nothing

Run: python -m unittest tests.test_position_update_scheduler_sprint34 -v
"""
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.notifications.position_update_scheduler import PositionUpdateScheduler
from src.data_store.positions import PositionRepository, Position
from src.core.event_bus import EventBus


def _make_pos(asset, direction, entry_price, qty=0.001, age_s=0, sl=49000, tp=52000):
    return Position(
        asset=asset, direction=direction,
        entry_price=entry_price, stop_loss=sl, take_profit=tp,
        qty=qty, risk_usd=10.0,
        entry_ts=time.time() - age_s, strategy="test",
    )


class CadenceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.bus = EventBus()
        self.published = []
        self.bus.subscribe("POSITION_UPDATE", lambda d: self.published.append(d))

    def test_first_tick_emits_immediately(self):
        # Even though interval is 60min, the FIRST tick for a position
        # has last_ts=0, so (now - 0) >> 60min → emit.
        pos = _make_pos("BTC-USD", "long", 50000, age_s=60*30)  # 30 min old
        self.repo.add_open(pos)
        sched = PositionUpdateScheduler(self.repo, self.bus, interval_minutes=60)
        n = sched.tick({"BTC-USD": 50100})
        self.assertEqual(n, 1)
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.published[0]["asset"], "BTC-USD")
        self.assertAlmostEqual(self.published[0]["unrealized_pnl_usd"], 0.1, places=3)

    def test_second_tick_within_interval_does_not_emit(self):
        pos = _make_pos("BTC-USD", "long", 50000)
        self.repo.add_open(pos)
        sched = PositionUpdateScheduler(self.repo, self.bus, interval_minutes=60)
        sched.tick({"BTC-USD": 50100})  # first emit
        # immediately tick again — should NOT emit (interval not elapsed)
        n2 = sched.tick({"BTC-USD": 50100})
        self.assertEqual(n2, 0)
        self.assertEqual(len(self.published), 1)

    def test_per_position_independence(self):
        # Position A just emitted; Position B just opened.
        # B should emit on its first tick (last_ts=0), A should not.
        pos_a = _make_pos("BTC-USD", "long", 50000)
        pos_b = _make_pos("ETH-USD", "long", 3000, qty=0.01, sl=2950, tp=3150)
        self.repo.add_open(pos_a)
        self.repo.add_open(pos_b)  # ← BUG FIX: was missing in initial version
        sched = PositionUpdateScheduler(self.repo, self.bus, interval_minutes=60)
        sched.tick({"BTC-USD": 50100, "ETH-USD": 3000})  # both emit (first tick)
        self.assertEqual(len(self.published), 2)
        # B closes, A's interval is still ticking
        self.repo.close_position(pos_b.position_id, 3000, "TEST")
        # Pretend A's interval is up (manipulate internal state)
        sched._last_update[pos_a.position_id] = time.time() - 3601
        n = sched.tick({"BTC-USD": 50200})
        self.assertEqual(n, 1, "Only A should emit; B is closed")
        self.assertEqual(self.published[-1]["asset"], "BTC-USD")

    def test_clear_position_removes_from_state(self):
        pos = _make_pos("BTC-USD", "long", 50000)
        self.repo.add_open(pos)
        sched = PositionUpdateScheduler(self.repo, self.bus, interval_minutes=60)
        sched.tick({"BTC-USD": 50100})  # first emit
        self.assertIn(pos.position_id, sched._last_update)
        sched.clear_position(pos.position_id)
        self.assertNotIn(pos.position_id, sched._last_update)
        self.assertNotIn(pos.position_id, sched._first_seen)

    def test_clear_all_resets_state(self):
        for i in range(3):
            self.repo.add_open(_make_pos(f"X-{i}-USD", "long", 100+i, qty=0.01, sl=99+i, tp=101+i))
        sched = PositionUpdateScheduler(self.repo, self.bus, interval_minutes=60)
        sched.tick({f"X-{i}-USD": 100+i for i in range(3)})
        self.assertEqual(len(sched._last_update), 3)
        sched.clear_all()
        self.assertEqual(len(sched._last_update), 0)
        self.assertEqual(len(sched._first_seen), 0)

    def test_disabled_with_zero_interval(self):
        pos = _make_pos("BTC-USD", "long", 50000)
        self.repo.add_open(pos)
        sched = PositionUpdateScheduler(self.repo, self.bus, interval_minutes=0)
        n = sched.tick({"BTC-USD": 50100})
        self.assertEqual(n, 0)
        self.assertEqual(len(self.published), 0)


class SymbolVariantsTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.bus = EventBus()
        self.published = []
        self.bus.subscribe("POSITION_UPDATE", lambda d: self.published.append(d))

    def test_btc_usd_finds_btcusdt_in_prices(self):
        pos = _make_pos("BTC-USD", "long", 50000)
        self.repo.add_open(pos)
        sched = PositionUpdateScheduler(self.repo, self.bus, interval_minutes=60)
        # price dict has BTCUSDT (no hyphen)
        n = sched.tick({"BTCUSDT": 50100})
        self.assertEqual(n, 1, "Should resolve BTC-USD → BTCUSDT")
        self.assertEqual(self.published[0]["current_price"], 50100)

    def test_no_price_match_skips_silently(self):
        pos = _make_pos("BTC-USD", "long", 50000)
        self.repo.add_open(pos)
        sched = PositionUpdateScheduler(self.repo, self.bus, interval_minutes=60)
        n = sched.tick({"ETH-USD": 3000})  # wrong asset
        self.assertEqual(n, 0)
        self.assertEqual(len(self.published), 0)


class PnLThresholdTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.bus = EventBus()
        self.published = []
        self.bus.subscribe("POSITION_UPDATE", lambda d: self.published.append(d))

    def test_dust_pnl_skipped(self):
        # Position with $0.001 unrealized P&L (well below $0.50 threshold)
        pos = _make_pos("BTC-USD", "long", 50000, qty=0.0001)
        self.repo.add_open(pos)
        sched = PositionUpdateScheduler(
            self.repo, self.bus, interval_minutes=60, min_pnl_usd=0.5
        )
        n = sched.tick({"BTC-USD": 50010})  # +$0.001 (dust)
        self.assertEqual(n, 0)
        # Clock is NOT advanced on dust skip — next tick should re-evaluate
        # the P&L (e.g. if the price recovers enough to cross the threshold).
        self.assertNotIn(pos.position_id, sched._last_update)

    def test_above_threshold_emits(self):
        pos = _make_pos("BTC-USD", "long", 50000, qty=0.001)
        self.repo.add_open(pos)
        sched = PositionUpdateScheduler(
            self.repo, self.bus, interval_minutes=60, min_pnl_usd=0.5
        )
        n = sched.tick({"BTC-USD": 50100})  # +$0.10 — still below $0.50
        self.assertEqual(n, 0)
        # Second tick: P&L recovers to +$0.60 — should emit immediately
        # (clock was NOT advanced on the dust skip)
        n2 = sched.tick({"BTC-USD": 50600})
        self.assertEqual(n2, 1)


class PayloadShapeTest(unittest.TestCase):
    """Verify the POSITION_UPDATE payload has all the fields the
    NotificationAgent handler needs."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = PositionRepository(os.path.join(self.tmpdir, "positions.json"))
        self.bus = EventBus()
        self.published = []
        self.bus.subscribe("POSITION_UPDATE", lambda d: self.published.append(d))

    def test_payload_contains_all_required_fields(self):
        pos = _make_pos("BTC-USD", "long", 50000, qty=0.001, sl=49000, tp=52000, age_s=7200)
        self.repo.add_open(pos)
        sched = PositionUpdateScheduler(self.repo, self.bus, interval_minutes=60)
        sched.tick({"BTC-USD": 50500})

        payload = self.published[0]
        required = {
            "position_id", "asset", "direction", "entry_price", "current_price",
            "qty", "stop_loss", "take_profit", "unrealized_pnl_usd",
            "unrealized_pnl_pct", "duration_hours", "notional_usd",
        }
        missing = required - set(payload.keys())
        self.assertFalse(missing, f"Missing fields: {missing}")
        self.assertEqual(payload["direction"], "long")
        self.assertEqual(payload["duration_hours"], 2.0)


if __name__ == "__main__":
    unittest.main()
