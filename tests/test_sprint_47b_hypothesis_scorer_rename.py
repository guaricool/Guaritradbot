"""
Sprint 47B (audit M1 resto) — HypothesisScorer naming regression test.

The audit's M1 complaint: the original `DebateAgent` and friends
(BullResearcher, BearResearcher, RiskTeam, PortfolioManager) had
names that implied a real multi-agent debate, but the code was
sequential deterministic scoring (if-elif with hard-coded weights
0.4/0.4/0.2 and a magic threshold 50). The fix: rename the
classes to honest names that match what the code actually does.

This test ensures the new names are present and the old names
are not (the renames are a "one-time migration" — if someone
adds back `class DebateAgent:` to researchers.py, that would
re-introduce the dishonest framing the audit called out).

What we test:
  1. All five renamed classes importable from src.agents.researchers
  2. None of the old class names exist as classes in that module
  3. The workflow YAML action `run_debate` is preserved (the
     method name didn't change, only the class name) so the
     workflow_engine still routes to it.
  4. HypothesisScorer.run_debate is callable with the same
     signature as the old DebateAgent.run_debate.
"""
import ast
import unittest
from pathlib import Path

from src.agents import researchers
from src.agents.researchers import (
    BearScorer,
    BullScorer,
    HypothesisScorer,
    RiskScorer,
    ScoreSynthesizer,
)


class HypothesisScorerRenamesTest(unittest.TestCase):
    """Sprint 47B: the audit's M1 was about HONEST NAMING. These
    tests lock in the new names so a future PR can't accidentally
    re-introduce the `DebateAgent` framing the audit called out."""

    def test_new_class_names_importable(self):
        # All five new names importable
        self.assertIsNotNone(BullScorer)
        self.assertIsNotNone(BearScorer)
        self.assertIsNotNone(RiskScorer)
        self.assertIsNotNone(ScoreSynthesizer)
        self.assertIsNotNone(HypothesisScorer)

    def test_old_class_names_absent_from_module(self):
        # The old names must not exist as classes in the module
        # -- otherwise the misleading "debate" framing is back.
        for old_name in (
            "BullResearcher", "BearResearcher", "RiskTeam",
            "PortfolioManager", "DebateAgent",
        ):
            self.assertFalse(
                hasattr(researchers, old_name),
                f"{old_name} should not exist in src.agents.researchers "
                f"after the Sprint 47B rename. The audit explicitly "
                f"called out the dishonest framing -- re-introducing "
                f"the old name invites the same confusion.",
            )

    def test_workflow_yaml_action_preserved(self):
        # The workflow YAML step `action: run_debate` must still
        # resolve. We can't run the workflow here (it needs the
        # full engine), but we can verify the method still exists
        # on the orchestrator class with the same name. If a
        # future rename changes `run_debate` to `run_scoring`
        # without also updating the YAML, the workflow will
        # silently skip the scoring step -- catch that here.
        self.assertTrue(hasattr(HypothesisScorer, "run_debate"))
        import inspect
        sig = inspect.signature(HypothesisScorer.run_debate)
        # Same signature as the old DebateAgent.run_debate:
        # (self, inputs: dict, state: dict) -> Dict[str, Any]
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["self", "inputs", "state"])

    def test_module_docstring_is_honest(self):
        # The audit's specific complaint: the docstring framed
        # the code as a "multi-agent debate" when it's sequential
        # scoring. The post-47B docstring must explicitly say so.
        docstring = researchers.__doc__ or ""
        # The new docstring should call out that the implementation
        # is scoring, not debating, and reference Sprint 47B / M1.
        self.assertIn("Sprint 47B", docstring)
        self.assertIn("scoring", docstring.lower())
        # The old title "Debate Multi-Agente" can appear in the
        # docstring as the OLD name (e.g. "formerly Debate Multi-Agente"
        # in the rename history), but the docstring must NOT lead
        # with it -- the title/subject must be the new honest
        # framing. Take the first non-empty line.
        first_line = next(
            (line.strip() for line in docstring.splitlines() if line.strip()),
            "",
        )
        self.assertFalse(
            first_line.startswith("Debate Multi-Agente"),
            f"Module docstring leads with the old dishonest title: "
            f"{first_line!r}. The audit's M1 specifically called this "
            f"out -- the docstring must lead with the new framing.",
        )

    def test_source_file_no_longer_uses_old_class_names(self):
        # Static source check: no `class DebateAgent:` /
        # `class BullResearcher:` etc. definitions or references
        # remain anywhere in the source tree. (Tests are allowed
        # to mention them -- this check is src/ + main.py only.)
        roots = [Path("src"), Path("main.py")]
        offenders: list[str] = []
        for root in roots:
            if root.is_file():
                files = [root]
            else:
                files = list(root.rglob("*.py"))
            for f in files:
                # Skip the renamed module itself (the migration
                # table in the docstring mentions the old names
                # for context).
                if f.name == "researchers.py":
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                # Use AST to find class definitions, not just
                # string matches (a comment mentioning the old
                # name is fine; a `class DebateAgent:` is not).
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name in (
                        "BullResearcher", "BearResearcher", "RiskTeam",
                        "PortfolioManager", "DebateAgent",
                    ):
                        offenders.append(f"{f}:{node.lineno} class {node.name}")
        self.assertEqual(
            offenders, [],
            f"Old debate-framing class names should not exist outside "
            f"the renamed module. Found: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
