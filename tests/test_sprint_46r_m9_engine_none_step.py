"""
Sprint 46R (audit M9 resto): regression tests for the
"step returns None satisfies depends_on" bug.

The audit's exact wording:
  "Un paso del workflow que retorna None igualmente satisface
   depends_on (engine.py:42) - los pasos siguientes corren
   con datos faltantes."

Pre-Sprint-46R fix:
  - _check_depends_on only checked that the dep key existed
    in `state`. A step whose action returned None was still
    considered "ran" and downstream steps got None as their
    input data.
  - The result was silent data loss: three steps later, the
    action method tried to subscript `state["foo"]["bar"]` on
    a None and crashed with a confusing NoneType error that
    had no obvious link to the upstream step that returned
    None in the first place.

The fix is two layers:
  1. Source: if a step's action returns None and the step is
     not marked `optional: true` in the YAML, raise
     WorkflowStepReturnedNoneError immediately with a clear
     pointer to the offending step.
  2. Boundary: even if the upstream step WAS marked optional
     and returned None, a downstream step that depends on it
     still fails (via _check_depends_on) with a clear
     WorkflowDependencyError explaining which dep produced
     None.

Tests cover:
  - None result on a non-optional step raises the new error
  - None result on an optional step is allowed BUT
    downstream fails with WorkflowDependencyError
  - Valid (non-None) result is unaffected
  - Optional step returning a real value is unaffected
"""
from __future__ import annotations

import unittest

from src.workflows.engine import (
    WorkflowAgentFaultError,
    WorkflowDependencyError,
    WorkflowEngine,
    WorkflowStepReturnedNoneError,
)


def _make_engine_with_steps(steps, side_effects):
    """Build a WorkflowEngine with N steps. Each step's
    agent+action points to a mock that returns the next item
    in `side_effects` (so step[i] returns side_effects[i]).
    """
    agents = {}
    for i, step in enumerate(steps):
        agent_name = step["agent"]
        action_name = step["action"]
        if agent_name not in agents:
            agents[agent_name] = {}
        # The engine calls the action as action_method(inputs=...,
        # state=...) — kwargs, not positional. Use **kwargs so
        # the lambda accepts both styles.
        agents[agent_name][action_name] = (
            lambda _i=i, **_: side_effects[_i]
        )

    # Wrap in a fake agent class so getattr(agent, action) works.
    class _FakeAgent:
        pass

    wrapped = {}
    for name, actions in agents.items():
        a = _FakeAgent()
        for action_name, fn in actions.items():
            setattr(a, action_name, fn)
        wrapped[name] = a

    return WorkflowEngine(wrapped)


class WorkflowStepReturnsNoneTest(unittest.TestCase):
    def test_step_returning_none_raises_workflow_step_returned_none_error(self):
        """Source-level: a non-optional step returning None
        must raise WorkflowStepReturnedNoneError immediately.
        The previous behavior was to silently store None in
        state and let the next step crash with NoneType."""
        workflow = {
            "name": "test",
            "steps": [
                {"id": "a", "agent": "agt", "action": "do"},
            ],
        }
        engine = _make_engine_with_steps(workflow["steps"], [None])

        with self.assertRaises(WorkflowStepReturnedNoneError) as ctx:
            engine.run(workflow)
        # The error message should name the offending step so
        # the operator can find it.
        self.assertIn("'a'", str(ctx.exception))
        self.assertIn("returned None", str(ctx.exception))

    def test_optional_step_returning_none_is_allowed(self):
        """A step marked `optional: true` can return None
        without raising. The step is recorded in state as None
        (so the run completes), but downstream steps that
        depend on it must still fail at the depends_on
        boundary (see test_optional_none_breaks_downstream).
        """
        workflow = {
            "name": "test",
            "steps": [
                {"id": "a", "agent": "agt", "action": "do", "optional": True},
                # No step depends on 'a' - so this whole workflow
                # is valid even with a returning None.
            ],
        }
        engine = _make_engine_with_steps(workflow["steps"], [None])

        # Must NOT raise - the optional step is allowed to be a
        # no-op this cycle.
        final_state = engine.run(workflow)
        self.assertIn("a", final_state)
        self.assertIsNone(final_state["a"])

    def test_optional_none_breaks_downstream(self):
        """Even if step A is optional and returns None, step B
        that depends on A must FAIL with WorkflowDependencyError.
        This is the audit's exact complaint: "los pasos
        siguientes corren con datos faltantes." The fix is the
        _check_depends_on enhancement that treats None as
        equivalent to "did not run" for depends_on purposes.
        """
        workflow = {
            "name": "test",
            "steps": [
                {"id": "a", "agent": "agt", "action": "do", "optional": True},
                {"id": "b", "agent": "agt", "action": "use", "depends_on": ["a"]},
            ],
        }
        # 'a' returns None (allowed), 'b' would have returned
        # "B-OK" but the engine never gets to invoke it.
        engine = _make_engine_with_steps(
            workflow["steps"], [None, "B-OK"]
        )

        with self.assertRaises(WorkflowDependencyError) as ctx:
            engine.run(workflow)
        # The error message should explain the None-result issue.
        msg = str(ctx.exception)
        self.assertIn("'a'", msg)
        self.assertIn("returned None", msg)
        # 'b' is the step that's blocked.
        self.assertIn("'b'", msg)

    def test_normal_step_returning_value_unaffected(self):
        """The happy path - a non-None result - must not be
        affected by the new check."""
        workflow = {
            "name": "test",
            "steps": [
                {"id": "a", "agent": "agt", "action": "do"},
                {"id": "b", "agent": "agt", "action": "use", "depends_on": ["a"]},
            ],
        }
        engine = _make_engine_with_steps(
            workflow["steps"], [{"data": "ok"}, {"used": "a"}]
        )
        final_state = engine.run(workflow)
        self.assertEqual(final_state["a"], {"data": "ok"})
        self.assertEqual(final_state["b"], {"used": "a"})

    def test_optional_step_returning_real_value_works(self):
        """Optional is a "may be None" knob, not a "must be
        None" knob. A step marked optional that returns a real
        value should work just like a normal step.
        """
        workflow = {
            "name": "test",
            "steps": [
                {"id": "a", "agent": "agt", "action": "do", "optional": True},
                {"id": "b", "agent": "agt", "action": "use", "depends_on": ["a"]},
            ],
        }
        engine = _make_engine_with_steps(
            workflow["steps"], [{"data": 1}, "B-OK"]
        )
        final_state = engine.run(workflow)
        self.assertEqual(final_state["a"], {"data": 1})

    def test_existing_missing_dep_still_caught(self):
        """Regression guard for the original Sprint 43 H11 fix:
        a dep key that's never been run still raises
        WorkflowDependencyError with the 'missing from
        workflow' / 'not yet run' message."""
        workflow = {
            "name": "test",
            "steps": [
                # 'b' depends on 'a', but 'a' isn't in the workflow.
                {"id": "b", "agent": "agt", "action": "do", "depends_on": ["a"]},
            ],
        }
        engine = _make_engine_with_steps(workflow["steps"], ["B-OK"])
        with self.assertRaises(WorkflowDependencyError) as ctx:
            engine.run(workflow)
        msg = str(ctx.exception)
        self.assertIn("missing from workflow", msg)

    def test_existing_faulted_agent_still_caught(self):
        """Regression guard for Sprint 43 H11: an agent in
        FAULTED state still raises WorkflowAgentFaultError."""
        workflow = {
            "name": "test",
            "steps": [
                {"id": "a", "agent": "agt", "action": "do"},
            ],
        }
        engine = _make_engine_with_steps(workflow["steps"], ["ok"])

        # Replace the agent's state with a FAULTED Component.state.
        class _FaultedState:
            name = "FAULTED"
        engine.agents["agt"].state = _FaultedState()
        with self.assertRaises(WorkflowAgentFaultError):
            engine.run(workflow)


if __name__ == "__main__":
    unittest.main()
