"""
Sprint 46R (audit B9 framework): regression tests for the
logging_setup helper. The audit's B9 finding flagged ~221
print() calls without level distinction; the framework
established in Sprint 46R is setup_logging() + get_logger().
These tests lock down the contract so future edits don't
silently break it (e.g. accidentally setting the root level
to WARNING, which would silence the per-module INFO logs).
"""
from __future__ import annotations

import io
import logging
import unittest

from src.core.logging_setup import setup_logging, get_logger


class LoggingSetupTest(unittest.TestCase):
    def setUp(self):
        # Capture handler we install so we can read the output
        # without polluting other tests. The default root logger
        # in test runs is WARNING-only (Python's stdlib default),
        # so installing our own handler is necessary to observe
        # INFO and below.
        self._buf = io.StringIO()
        self._handler = logging.StreamHandler(self._buf)
        self._handler.setFormatter(
            logging.Formatter("%(levelname)s|%(name)s|%(message)s")
        )

    def _output(self) -> str:
        # flush any buffered output before reading
        for h in logging.getLogger().handlers:
            h.flush()
        return self._buf.getvalue()

    def test_get_logger_returns_named_logger(self):
        lg = get_logger("src.foo.bar")
        self.assertEqual(lg.name, "src.foo.bar")

    def test_setup_logging_is_idempotent(self):
        # Calling setup_logging() twice in the same process
        # (e.g. from main.py + a test) must NOT stack handlers
        # or duplicate every log line.
        root = logging.getLogger()
        handlers_before = list(root.handlers)
        setup_logging(level=logging.INFO)
        setup_logging(level=logging.INFO)
        # Exactly one handler should remain (not 2).
        self.assertEqual(len(root.handlers), 1,
                         f"expected 1 handler, got {len(root.handlers)}")

    def test_setup_logging_emits_info_at_info_level(self):
        setup_logging(level=logging.INFO)
        # Replace root's handler with our capture handler so
        # we can read what was emitted.
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(self._handler)
        root.setLevel(logging.INFO)

        get_logger("test.mod").info("hello world")
        out = self._output()
        self.assertIn("INFO", out)
        self.assertIn("test.mod", out)
        self.assertIn("hello world", out)

    def test_setup_logging_silences_debug_at_info_level(self):
        # The pre-Sprint-46R print() verbosity maps to INFO;
        # DEBUG chatter must NOT leak into the bot logs by
        # default, or the Coolify container logs will spam
        # every analysis cycle.
        setup_logging(level=logging.INFO)
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(self._handler)
        root.setLevel(logging.INFO)

        get_logger("test.mod").debug("should not appear")
        out = self._output()
        self.assertEqual(out, "",
                         f"DEBUG leaked at INFO level: {out!r}")


if __name__ == "__main__":
    unittest.main()
