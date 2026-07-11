"""
Sprint 46N tests — audit finding C6 (AUDITORIA_COMPLETA_2026-07-11.md).

C6: docker-compose.yml only declared `bot_audit` as a persistent
volume for the `guaritradbot` service. `data_store/` — which holds
`positions.json` (every OPEN position) and `equity_state.json` (the
drawdown kill switch's peak-equity tracker) — was NOT a volume, so
every redeploy (new image build + container recreate) silently wiped
both files: the bot would forget every open position and reset its
drawdown peak with zero errors or warnings anywhere.

Fix: docker-compose.yml now mounts a named volume `bot_data_store` at
`/app/data_store` for the `guaritradbot` service, declared alongside
`bot_audit` under the top-level `volumes:` key. This test parses the
actual compose file (not a copy) to guard against the fix regressing.

Run: python -m unittest tests.test_sprint_46n_c6_data_store_volume -v
"""
import os
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DataStoreVolumeTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(ROOT, "docker-compose.yml")
        with open(path, encoding="utf-8") as f:
            self.compose = yaml.safe_load(f)

    def test_guaritradbot_mounts_data_store_volume(self):
        volumes = self.compose["services"]["guaritradbot"]["volumes"]
        data_store_mounts = [v for v in volumes if v.endswith(":/app/data_store")]
        self.assertEqual(
            len(data_store_mounts), 1,
            "guaritradbot service must mount exactly one volume at /app/data_store (C6 fix)",
        )

    def test_data_store_volume_is_named_not_bind_mount(self):
        volumes = self.compose["services"]["guaritradbot"]["volumes"]
        data_store_mount = next(v for v in volumes if v.endswith(":/app/data_store"))
        volume_name = data_store_mount.split(":")[0]
        # A named volume (not a bind-mount path like "./data_store" or
        # "/host/path") must be declared under the top-level volumes: key.
        self.assertIn(
            volume_name, self.compose["volumes"],
            f"'{volume_name}' must be declared under the top-level volumes: key "
            "(named volume, not a bind mount) so Docker persists it across "
            "container recreation (C6 fix)",
        )

    def test_audit_volume_still_present(self):
        """Regression guard: the pre-existing audit/ volume (which the
        C6 fix's comment refers to) must not have been dropped."""
        volumes = self.compose["services"]["guaritradbot"]["volumes"]
        audit_mounts = [v for v in volumes if v.endswith(":/app/audit")]
        self.assertEqual(len(audit_mounts), 1)
        volume_name = audit_mounts[0].split(":")[0]
        self.assertIn(volume_name, self.compose["volumes"])

    def test_compose_file_is_valid_yaml_with_expected_services(self):
        self.assertIn("guaritradbot", self.compose["services"])
        self.assertIn("dashboard", self.compose["services"])


if __name__ == "__main__":
    unittest.main()
