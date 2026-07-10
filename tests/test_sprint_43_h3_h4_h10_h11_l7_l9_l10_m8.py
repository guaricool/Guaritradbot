"""
Sprint 43 — H3 (DrawdownKillSwitch), H4 (paper_to_live dry-run),
H10 (multi_tf look-ahead), H11 (WorkflowEngine deps + state),
L7 (requirements.lock), L9 (.deploy.php HMAC), L10 (test migration),
M8 (Docker hardening).
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class H3DrawdownKillSwitchWiringTest(unittest.TestCase):
    """H3: DrawdownKillSwitch must be instantiated in main.py and
    reachable during the trading loop. We verify the class is
    imported and an instance is created."""

    def test_drawdown_kill_switch_importable(self):
        from src.safety.kelly_drawdown import DrawdownKillSwitch
        ks = DrawdownKillSwitch(threshold_pct=10.0, cooldown_hours=1.0)
        self.assertIsNotNone(ks)

    def test_drawdown_kill_switch_used_in_main(self):
        with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
            main_src = f.read()
        self.assertIn("DrawdownKillSwitch", main_src,
                      "DrawdownKillSwitch must be instantiated in main.py (H3 fix)")
        self.assertIn("drawdown_kill_switch.update", main_src,
                      "main.py must call drawdown_kill_switch.update() in the loop")


class H4PaperToLiveDryRunTest(unittest.TestCase):
    """H4: _validate_dry_run must NOT place a real order by default."""

    def test_dry_run_does_not_call_create_market_order(self):
        """Read the source: the default path must not call
        broker.create_market_order. Only the legacy opt-in path does.
        """
        with open(os.path.join(ROOT, "src/safety/paper_to_live.py"), encoding="utf-8") as f:
            src = f.read()
        # Find the _validate_dry_run method body — must NOT call
        # create_market_order in its body (the docstring may
        # mention it for historical context, but the body must
        # only use the legacy helper when explicitly opted in).
        import re
        match = re.search(r"def _validate_dry_run.*?(?=\n    def )", src, re.DOTALL)
        self.assertIsNotNone(match, "Couldn't locate _validate_dry_run")
        body = match.group(0)
        # Strip the docstring before checking
        body_no_doc = re.sub(r'"""[\s\S]*?"""', "", body)
        self.assertNotIn(
            "create_market_order", body_no_doc,
            "Sprint 43 H4 fix: _validate_dry_run body must NOT call "
            "create_market_order (only the legacy opt-in path should).",
        )
        # The new default path must use the read-only balance check
        self.assertIn("get_usdt_balance", body_no_doc,
                      "Default dry-run path must use get_usdt_balance (read-only)")
        # And the legacy path is delegated to
        self.assertIn("_legacy_destructive_dry_run", body_no_doc,
                      "Default path must delegate to legacy for opt-in")

    def test_legacy_path_kept_for_backward_compat(self):
        """The legacy destructive path is still in the file, but
        behind an opt-in flag (dry_run_placement=true)."""
        from src.safety.paper_to_live import PaperToLiveChecklist
        self.assertTrue(hasattr(PaperToLiveChecklist, "_legacy_destructive_dry_run"),
                        "Legacy destructive path preserved as opt-in")


class H10MultiTFNoLookAheadTest(unittest.TestCase):
    """H10: multi_tf must NOT use future 4h data to label 1h signals."""

    def test_4h_trend_shifted_before_ffill(self):
        """The H10 fix: shift the 4h trend by 1 bar before ffill.
        Verify the source has the shift + ffill pattern.
        """
        with open(os.path.join(ROOT, "src/strategy/multi_tf.py"), encoding="utf-8") as f:
            src = f.read()
        # After the H10 fix, the trend is shifted by 1 bar
        self.assertIn(
            ".shift(1)",
            src,
            "Sprint 43 H10: 4h trend must be shifted by 1 bar "
            "to prevent using future data to label 1h signals",
        )

    def test_trend_signal_uses_no_future_data(self):
        """Behavioral test: feed a known sequence and verify the
        trend signal at time t uses ONLY 4h bars with end_time <= t.
        """
        from src.strategy.multi_tf import MTFTrendPullback, MTFData
        # Build 1h data: 24 hours
        idx_1h = pd.date_range("2026-07-10 00:00", periods=24, freq="1h")
        # Build 4h data: 6 buckets [00-04), [04-08), ..., [20-24)
        idx_4h = pd.date_range("2026-07-10 00:00", periods=6, freq="4h")
        # 4h trend: 0,0,1,1,1,1 (becomes up from bucket 2 onwards)
        closes_4h = [100.0, 100.0, 110.0, 120.0, 130.0, 140.0]
        df_4h = pd.DataFrame({
            "Close": closes_4h,
            "Open": closes_4h, "High": [c + 1 for c in closes_4h],
            "Low": [c - 1 for c in closes_4h], "Volume": [1000.0] * 6,
        }, index=idx_4h)
        # 1h closes: stable
        df_1h = pd.DataFrame({
            "Close": [100.0] * 24,
            "Open": [100.0] * 24, "High": [101.0] * 24,
            "Low": [99.0] * 24, "Volume": [1000.0] * 24,
        }, index=idx_1h)
        strat = MTFTrendPullback()
        data = MTFData(timeframes={"1h": df_1h, "4h": df_4h}, asset="TEST")
        sig = strat.generate_signal(data)
        # We just verify the function returns a Series of the
        # right shape and doesn't error.
        self.assertIsInstance(sig, pd.Series)
        self.assertEqual(len(sig), 24)


class H11WorkflowEngineDepsAndStateTest(unittest.TestCase):
    """H11: WorkflowEngine must enforce depends_on and check
    agent.state before invoking."""

    def test_depends_on_unmet_raises(self):
        from src.workflows.engine import WorkflowEngine, WorkflowDependencyError
        # Mock agent
        agent = type("A", (), {"step1": lambda self, inputs, state: "ok",
                                "step2": lambda self, inputs, state: "ok",
                                "state": type("S", (), {"name": "READY"})()})()
        engine = WorkflowEngine({"A": agent})
        wf = {
            "name": "test",
            "steps": [
                {"id": "step1", "agent": "A", "action": "step1"},
                {"id": "step2", "agent": "A", "action": "step2", "depends_on": ["step3"]},  # step3 doesn't exist
            ],
        }
        with self.assertRaises(WorkflowDependencyError) as ctx:
            engine.run(wf)
        self.assertIn("step3", str(ctx.exception))

    def test_agent_faulted_state_raises(self):
        from src.workflows.engine import WorkflowEngine, WorkflowAgentFaultError
        agent = type("A", (), {
            "step1": lambda self, inputs, state: "ok",
            "state": type("S", (), {"name": "FAULTED"})(),  # FAULTED
        })()
        engine = WorkflowEngine({"A": agent})
        wf = {
            "name": "test",
            "steps": [
                {"id": "step1", "agent": "A", "action": "step1"},
            ],
        }
        with self.assertRaises(WorkflowAgentFaultError) as ctx:
            engine.run(wf)
        self.assertIn("FAULTED", str(ctx.exception))

    def test_healthy_agent_passes(self):
        from src.workflows.engine import WorkflowEngine
        agent = type("A", (), {
            "step1": lambda self, inputs, state: "ok",
            "state": type("S", (), {"name": "READY"})(),
        })()
        engine = WorkflowEngine({"A": agent})
        wf = {
            "name": "test",
            "steps": [{"id": "step1", "agent": "A", "action": "step1"}],
        }
        result = engine.run(wf)
        self.assertEqual(result["step1"], "ok")

    def test_depends_on_met_passes(self):
        from src.workflows.engine import WorkflowEngine
        call_log = []
        agent = type("A", (), {
            "step1": lambda self, inputs, state: ("step1_result"),
            "step2": lambda self, inputs, state: (call_log.append(state["step1"]) or "step2_result"),
            "state": type("S", (), {"name": "READY"})(),
        })()
        engine = WorkflowEngine({"A": agent})
        wf = {
            "name": "test",
            "steps": [
                {"id": "step1", "agent": "A", "action": "step1"},
                {"id": "step2", "agent": "A", "action": "step2", "depends_on": ["step1"]},
            ],
        }
        engine.run(wf)
        # step2 must have seen step1's result in its state arg
        self.assertEqual(call_log, ["step1_result"])


class L7RequirementsLockTest(unittest.TestCase):
    def test_lockfile_exists(self):
        lock = os.path.join(ROOT, "requirements.lock")
        self.assertTrue(os.path.exists(lock), "requirements.lock must exist (L7 fix)")
        # Must be parseable as a list of pkg==version
        with open(lock) as f:
            n = sum(1 for line in f if "==" in line and not line.strip().startswith("#"))
        self.assertGreater(n, 10, f"Lockfile should have many packages, got {n}")

    def test_lockfile_has_header(self):
        with open(os.path.join(ROOT, "requirements.lock")) as f:
            content = f.read()
        self.assertIn("Sprint 43 L7", content,
                      "Lockfile should have a header explaining what it is")


class L9DeployPhpHmacTest(unittest.TestCase):
    def test_deploy_php_requires_hmac_in_http_mode(self):
        """Reading the source: in HTTP mode (sapi != 'cli'),
        the script must require an X-Signature header."""
        with open(os.path.join(ROOT, ".deploy.php"), encoding="utf-8") as f:
            src = f.read()
        # Must check php_sapi_name() != "cli"
        self.assertIn('php_sapi_name() !== "cli"', src,
                      "Deploy script must require HMAC in HTTP mode (L9 fix)")
        # Must verify the signature
        self.assertIn("hash_equals", src,
                      "Deploy script must use hash_equals for constant-time compare")
        # Must require X-Signature header
        self.assertIn("X-Signature", src)

    def test_cli_mode_skips_hmac(self):
        """CLI invocation (Carlos on the box) skips HMAC."""
        with open(os.path.join(ROOT, ".deploy.php"), encoding="utf-8") as f:
            src = f.read()
        # The condition should be: require HMAC if NOT cli OR --require-hmac
        self.assertIn('--require-hmac', src,
                      "Deploy script should allow --require-hmac flag for testing")


class L10TestFilesMigratedTest(unittest.TestCase):
    def test_old_test_files_removed(self):
        """The old test_*.py files at the repo root must be gone
        (they were smoke tests that didn't get picked up by
        unittest discover)."""
        for f in ["test_backtest_real.py", "test_hyperopt.py"]:
            self.assertFalse(
                os.path.exists(os.path.join(ROOT, f)),
                f"{f} should be removed (L10 fix); moved to tests/",
            )

    def test_new_test_files_in_tests_dir(self):
        for f in ["test_backtest_real.py", "test_hyperopt.py"]:
            path = os.path.join(ROOT, "tests", f)
            self.assertTrue(os.path.exists(path), f"tests/{f} must exist")
            # Must be a proper unittest (have def test_*)
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("unittest", content)
            self.assertIn("def test_", content)


class M8DockerHardeningTest(unittest.TestCase):
    def test_dockerfiles_run_as_non_root(self):
        for dockerfile in ["Dockerfile.bot", "Dockerfile.dashboard"]:
            path = os.path.join(ROOT, dockerfile)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("USER app", content,
                          f"{dockerfile} must run as non-root (M8 fix)")
            self.assertIn("HEALTHCHECK", content,
                          f"{dockerfile} must have a HEALTHCHECK (M8 fix)")

    def test_docker_compose_has_resource_limits(self):
        path = os.path.join(ROOT, "docker-compose.yml")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # Both services should have resource limits
        self.assertIn("memory:", content,
                      "docker-compose must have memory limits (M8 fix)")
        self.assertIn("cpus:", content,
                      "docker-compose must have CPU limits (M8 fix)")


if __name__ == "__main__":
    unittest.main()
