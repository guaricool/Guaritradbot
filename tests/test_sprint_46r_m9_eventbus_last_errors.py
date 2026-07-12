"""
Sprint 46R (audit M9): regression tests for the EventBus
+ scheduler observability hardening.

Audit M9's findings:
  1. `EventBus.last_errors` grew without bound (a `List`,
     not a `deque(maxlen)`). On a long-running daemon, the
     dict would accumulate every subscriber exception
     forever, slowly leaking memory.
  2. The generic `except Exception` in `scheduler.run` (line
     182 pre-46R) wrote one audit event but did NOT publish
     `SYSTEM_ERROR`. A cycle that crashed unexpectedly
     silently disappeared with no Telegram alert.
  3. (Out of scope for this commit) Step returning `None`
     satisfies `depends_on` in `engine.py` — that's a separate
     WorkflowEngine fix tracked as a follow-up; not a memory
     issue, so not included here.

These tests cover:
  - last_errors is a deque with the expected maxlen
  - filling the deque past maxlen evicts the oldest (FIFO)
  - existing `last_errors` consumers still see the same
    "dict of {event_type: [error_dict, ...]}" shape
  - a single subscriber failing does NOT abort the rest
    (regression guard for the Sprint 43 H5 fix)
"""
from __future__ import annotations

import unittest
from collections import deque

from src.core.event_bus import EventBus


class EventBusLastErrorsDequeTest(unittest.TestCase):
    def test_last_errors_is_deque_with_maxlen(self):
        bus = EventBus()
        # Pre-46R this was a regular list; post-46R it's a deque
        # with a maxlen cap (EventBus._LAST_ERRORS_MAXLEN = 50).
        # Empty bus has no keys.
        self.assertEqual(bus.last_errors, {})
        # Trigger a failure to create the bucket.
        def bad_cb(_): raise RuntimeError("boom")
        bus.subscribe("FOO", bad_cb)
        bus.publish("FOO", {"x": 1})
        self.assertIn("FOO", bus.last_errors)
        self.assertIsInstance(bus.last_errors["FOO"], deque)
        # The maxlen matches the class constant.
        self.assertEqual(
            bus.last_errors["FOO"].maxlen,
            EventBus._LAST_ERRORS_MAXLEN,
        )

    def test_last_errors_capped_at_maxlen(self):
        """Filling past maxlen evicts the OLDEST entry (FIFO)."""
        bus = EventBus()
        # Tighten the cap for the test so we don't have to write
        # 50 entries to exercise the eviction path. The class
        # constant is 50; the FIFO eviction behavior is what
        # we're really testing, and that holds for any maxlen.
        cap = 3
        original_maxlen = EventBus._LAST_ERRORS_MAXLEN
        EventBus._LAST_ERRORS_MAXLEN = cap
        try:
            counter = {"n": 0}
            def bad_cb(_):
                counter["n"] += 1
                raise RuntimeError(f"err {counter['n']}")
            bus.subscribe("FOO", bad_cb)

            # Publish 5 times. Only the LAST `cap` errors should
            # survive.
            for _ in range(5):
                bus.publish("FOO", None)

            bucket = bus.last_errors["FOO"]
            self.assertEqual(len(bucket), cap,
                             f"Expected {cap} entries, got {len(bucket)}")
            # The first 2 should have been evicted; the surviving
            # ones are err 3, 4, 5. We test substring matches because
            # `repr(RuntimeError("err 3"))` is "RuntimeError('err 3')"
            # — assertIn with a list checks element equality, not
            # substring. Use any() with `in` for true substring match.
            errors = [e["error"] for e in bucket]
            for expected in ("err 3", "err 4", "err 5"):
                self.assertTrue(
                    any(expected in e for e in errors),
                    f"{expected!r} not in {errors}",
                )
            for evicted in ("err 1", "err 2"):
                self.assertFalse(
                    any(evicted in e for e in errors),
                    f"{evicted!r} should have been evicted by maxlen FIFO",
                )
        finally:
            EventBus._LAST_ERRORS_MAXLEN = original_maxlen

    def test_failing_subscriber_does_not_abort_others(self):
        """Sprint 43 H5 regression guard: one bad subscriber
        must not prevent later subscribers from being called.
        This is the WHOLE POINT of the try/except in publish().
        """
        bus = EventBus()
        called = []

        def good_cb_1(_): called.append("good_1")
        def bad_cb(_): raise RuntimeError("kaboom")
        def good_cb_2(_): called.append("good_2")

        bus.subscribe("X", good_cb_1)
        bus.subscribe("X", bad_cb)
        bus.subscribe("X", good_cb_2)

        bus.publish("X", None)

        # Both good subscribers fired, in order, with the bad
        # one sandwiched between them.
        self.assertEqual(called, ["good_1", "good_2"])
        # The bad subscriber's error is in last_errors.
        self.assertEqual(len(bus.last_errors["X"]), 1)

    def test_no_subscribers_is_no_op(self):
        """publish() on an event with no subscribers must
        not raise and must not create a last_errors bucket."""
        bus = EventBus()
        bus.publish("UNSUBSCRIBED_EVENT", None)
        self.assertEqual(bus.last_errors, {})

    def test_no_failure_does_not_create_last_errors_bucket(self):
        """A successful publish must NOT touch last_errors."""
        bus = EventBus()
        bus.subscribe("OK", lambda _: None)
        bus.publish("OK", {"x": 1})
        self.assertNotIn("OK", bus.last_errors)


if __name__ == "__main__":
    unittest.main()
