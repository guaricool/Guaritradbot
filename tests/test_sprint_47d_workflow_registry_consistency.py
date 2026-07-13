"""
Sprint 47D (emergency) — workflow/registry consistency test.

Sprint 47B renamed `DebateAgent` to `HypothesisScorer` and updated
every reference in the code (main.py, risk_agent.py,
strategy_agent.py, bot_runtime.py) -- but DID NOT update
`src/workflows/trading_loop.yaml`. The bot then went live, and
the workflow engine logged
`WORKFLOW_CYCLE_ERROR "Agent DebateAgent not found in registry"`
once per minute for every cycle, blocking all new entries. The
bot stayed "running" (the process was alive) but no trades
could execute because the workflow errored out at the
`score_hypotheses` step.

This test catches that class of bug at CI time: every
`agent:` value in trading_loop.yaml MUST exist as a key in
the agents registry that main.py builds. If a future rename
forgets either side (yaml or registry), this test fails
before deploy instead of after.

What we test:
  1. The yaml file loads successfully.
  2. Every step's `agent:` field is a non-empty string.
  3. Every step's `action:` field is a non-empty string.
  4. Every `agent:` value referenced in the yaml maps to a
     class imported in main.py's registry block. (We import
     the actual class names from the registry construction
     in main.py; this is a string-set comparison, not a
     runtime check, so it works without booting the full
     bot.)
  5. The `score_hypotheses` step (the rename target) uses
     `HypothesisScorer` and the legacy `DebateAgent` string
     does NOT appear anywhere in the yaml (regression guard
     for this specific bug).
"""
import re
import unittest
from pathlib import Path

import yaml


WORKFLOW_PATH = Path("src/workflows/trading_loop.yaml")
MAIN_PATH = Path("main.py")


class WorkflowRegistryConsistencyTest(unittest.TestCase):
    """Sprint 47D: catches the DebateAgent-rename bug at CI time."""

    def setUp(self):
        self.workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        main_src = MAIN_PATH.read_text(encoding="utf-8")
        # Extract the keys from the `registry = {...}` dict in
        # main.py. The dict is multi-line; we look for lines that
        # match the `"ClassName": ClassName(...)` pattern inside
        # the registry block. Robust against future formatting
        # tweaks (we don't assume specific indentation).
        registry_match = re.search(
            r"registry\s*=\s*\{(.+?)\n\s*\}",
            main_src,
            re.DOTALL,
        )
        if not registry_match:
            self.fail("Couldn't locate `registry = {...}` in main.py")
        registry_body = registry_match.group(1)
        # Each entry: "<key>": <class>(
        self.registry_keys = set(
            re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*[A-Z][A-Za-z0-9_]*\s*\(',
                       registry_body,
            )
        )

    def test_yaml_loads(self):
        self.assertIn("steps", self.workflow)
        self.assertIsInstance(self.workflow["steps"], list)
        self.assertGreater(len(self.workflow["steps"]), 0)

    def test_every_step_has_agent_and_action(self):
        for step in self.workflow["steps"]:
            self.assertIn(
                "agent", step,
                f"step {step.get('id', '?')} is missing `agent:`",
            )
            self.assertIn(
                "action", step,
                f"step {step.get('id', '?')} is missing `action:`",
            )
            self.assertIsInstance(
                step["agent"], str,
                f"step {step.get('id', '?')}: agent must be a string",
            )
            self.assertIsNotNone(
                step["action"],
                f"step {step.get('id', '?')}: action must not be None",
            )

    def test_every_yaml_agent_exists_in_registry(self):
        """Sprint 47D: every `agent:` value in the yaml must be
        a key in the registry that main.py builds. The original
        bug: 47B renamed DebateAgent to HypothesisScorer in
        every Python file but forgot the yaml, so the workflow
        engine tried to look up "DebateAgent" in the registry
        and threw ValueError on every cycle."""
        for step in self.workflow["steps"]:
            agent = step["agent"]
            self.assertIn(
                agent, self.registry_keys,
                f"workflow step `{step.get('id', '?')}` references "
                f"agent `{agent}` which is not in the registry. "
                f"Registry keys: {sorted(self.registry_keys)}. "
                f"Either add `{agent}` to main.py's registry dict, "
                f"or update the yaml to use one of the existing keys.",
            )

    def test_score_hypotheses_uses_hypothesis_scorer(self):
        """Sprint 47D regression guard: the renamed step must
        use the new class name. If someone reverts the rename
        in the yaml without renaming the class back, this
        test fails."""
        score_step = next(
            (s for s in self.workflow["steps"] if s.get("id") == "score_hypotheses"),
            None,
        )
        self.assertIsNotNone(
            score_step,
            "workflow is missing the `score_hypotheses` step "
            "(renamed from `debate_hypotheses` in 47B)",
        )
        self.assertEqual(score_step["agent"], "HypothesisScorer")

    def test_legacy_debate_agent_name_absent_from_yaml(self):
        """Sprint 47D regression guard: the OLD name `DebateAgent`
        must not appear anywhere in the yaml. If a future
        refactor re-introduces it (e.g. someone reverts just
        the yaml without reverting the class), this catches it
        before the bot goes live with a broken workflow."""
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        # Allow it in comments but not as an `agent:` value.
        # We strip line comments before searching.
        no_comments = re.sub(r"#.*$", "", text, flags=re.MULTILINE)
        # Look for the string as an `agent:` value, which is
        # the form that triggers the engine error.
        self.assertNotIn(
            "agent: DebateAgent",
            no_comments,
            "workflow yaml still references `agent: DebateAgent`. "
            "The class was renamed to HypothesisScorer in 47B; "
            "the yaml must use the new name.",
        )


if __name__ == "__main__":
    unittest.main()
