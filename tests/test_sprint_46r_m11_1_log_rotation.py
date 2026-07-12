"""
Sprint 46R (audit M11.1): regression tests for the Docker
log-rotation config in docker-compose.yml.

The audit's M11 finding: "Ningún servicio configura logging: en
compose (los logs crecen sin límite según el default del host)."
The fix: a `logging:` block with the json-file driver + max-size
+ max-file on both the bot and the dashboard services.

These tests guard against silent removal of the block by a
future refactor (e.g. a Coolify UI edit that overwrites
docker-compose.yml). They also document the audit's reasoning
inline, so the next person who sees "20m x 5" knows why.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKER_COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"


class ComposeLogRotationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = DOCKER_COMPOSE_PATH.read_text(encoding="utf-8")

    def _has_logging_block_for_service(self, service_name: str) -> bool:
        """Find the `logging:` block that belongs to `service_name`.

        docker-compose.yml uses YAML's flat-indent service block, so
        a service's `logging:` is nested under the same indent as
        `healthcheck:`, `volumes:`, `deploy:`, etc. We look for
        `  <service_name>:` followed by `    logging:` somewhere
        before the next `  <other_service>:` or `volumes:` (the
        top-level volumes block).
        """
        # Find this service's block start
        svc_re = re.compile(
            r"^  " + re.escape(service_name) + r":\s*$",
            re.MULTILINE,
        )
        m = svc_re.search(self.compose)
        if not m:
            return False
        start = m.end()
        # Find the end (next top-level `^  <name>:` or `^volumes:`)
        end_re = re.compile(r"^  [a-z_]+:\s*$|^volumes:\s*$", re.MULTILINE)
        end_m = end_re.search(self.compose, start)
        end = end_m.start() if end_m else len(self.compose)
        block = self.compose[start:end]
        return "logging:" in block and "max-size" in block and "max-file" in block

    def test_bot_service_has_logging_block(self):
        self.assertTrue(
            self._has_logging_block_for_service("guaritradbot"),
            "guaritradbot service must have a `logging:` block with "
            "max-size + max-file (audit M11.1 — log rotation cap).",
        )

    def test_dashboard_service_has_logging_block(self):
        self.assertTrue(
            self._has_logging_block_for_service("dashboard"),
            "dashboard service must have a `logging:` block with "
            "max-size + max-file (audit M11.1 — log rotation cap).",
        )

    def test_logging_uses_json_file_driver(self):
        # The audit's choice: json-file (the default) because it
        # supports rotation natively. Other drivers (syslog, journald)
        # would route logs out of the container, but Coolify expects
        # `docker logs <container>` to work for the dashboard's
        # tail-feed.
        self.assertIn("driver: json-file", self.compose,
                      "Expected json-file driver for log rotation")

    def test_max_size_20m_or_smaller(self):
        # The audit recommended "20MB x 5 files" (= 100MB max per
        # service). We accept anything <= 50MB because 100MB per
        # service is plenty and 50MB gives headroom for people who
        # want to be more aggressive.
        m = re.search(r'max-size:\s*"(\d+)m"', self.compose)
        self.assertIsNotNone(m,
                             "Expected max-size: \"<N>m\" in logging config")
        size_mb = int(m.group(1))
        self.assertLessEqual(size_mb, 50,
                             f"max-size={size_mb}MB is too large; "
                             f"audit M11.1 recommends ~20MB per file")

    def test_max_file_3_or_more(self):
        # At least 3 files of rotation history (so Carlos can see
        # what the bot was doing 1+ hours ago, not just the most
        # recent 20MB).
        m = re.search(r'max-file:\s*"(\d+)"', self.compose)
        self.assertIsNotNone(m, "Expected max-file: \"<N>\" in logging config")
        count = int(m.group(1))
        self.assertGreaterEqual(count, 3,
                                f"max-file={count} is too few; audit M11.1 "
                                f"recommends at least 3-5 files of history")


if __name__ == "__main__":
    unittest.main()
