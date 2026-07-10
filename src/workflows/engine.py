import yaml
import os

# Sprint 43 H11: agent state machine (Sprint 6 Component)
# The "agent is healthy" check needs to know which states are
# runnable. We treat READY/RUNNING/DEGRADED as runnable; FAULTED
# and STOPPED are not. This is a defensive default — operators
# can override per-agent.
_RUNNABLE_STATES = {"READY", "RUNNING", "DEGRADED"}


class WorkflowEngine:
    """
    Parses and executes YAML-based workflows for multi-agent orchestration.
    """
    def __init__(self, agents_registry):
        self.agents = agents_registry

    def load_workflow(self, filepath: str):
        with open(filepath, 'r') as file:
            return yaml.safe_load(file)

    def _check_depends_on(self, step: dict, state: dict, current_idx: int, all_steps: list) -> None:
        """
        Sprint 43 H11 fix: enforce depends_on from the YAML.

        The YAML allows `depends_on: [step_a, step_b]` on each
        step. The previous engine read it as documentation but
        never enforced it. If a YAML step listed a `depends_on`
        that wasn't met (e.g. because the order was changed or
        a step was renamed), the engine would run the step
        anyway, leading to race conditions or "input not ready"
        errors at runtime.

        The fix: before invoking a step, check that every
        step_id in its `depends_on` has already been run AND
        produced a result. If not, raise a WorkflowDependencyError
        with a clear message.
        """
        deps = step.get("depends_on") or []
        for dep in deps:
            if dep not in state:
                # Find where the dep was supposed to run
                dep_idx = next(
                    (i for i, s in enumerate(all_steps) if s.get("id") == dep),
                    None,
                )
                dep_status = "missing from workflow" if dep_idx is None else f"step index {dep_idx} (not yet run at step index {current_idx})"
                raise WorkflowDependencyError(
                    f"Step '{step.get('id')}' depends on '{dep}' but "
                    f"{dep_status}. Either reorder the YAML, add the "
                    f"missing step, or remove the depends_on."
                )

    def _check_agent_state(self, agent_name: str, agent) -> None:
        """
        Sprint 43 H11 fix: check the agent's Component.state
        (Sprint 6 state machine) before invoking it. If the
        agent is FAULTED or STOPPED, refuse to run it — that
        step is dead and should be skipped or retried by the
        caller. The previous engine read the state but didn't
        act on it.
        """
        state = getattr(agent, "state", None)
        if state is None:
            # No state machine — assume healthy (back-compat)
            return
        state_name = getattr(state, "name", str(state))
        if state_name not in _RUNNABLE_STATES:
            raise WorkflowAgentFaultError(
                f"Agent '{agent_name}' is in state '{state_name}' "
                f"(not in {_RUNNABLE_STATES}). The step is dead; "
                f"either restart the agent, skip the step, or "
                f"mark it optional in the YAML."
            )

    def run(self, workflow_data: dict):
        print(f"Starting workflow: {workflow_data.get('name')}")
        steps = workflow_data.get('steps', [])

        state = {}
        for i, step in enumerate(steps):
            step_id = step['id']
            agent_name = step['agent']
            action_name = step['action']
            inputs = step.get('inputs', {})

            print(f"[{step_id}] Delegating to {agent_name} -> {action_name}")

            if agent_name not in self.agents:
                raise ValueError(f"Agent {agent_name} not found in registry")

            agent = self.agents[agent_name]
            action_method = getattr(agent, action_name, None)

            if not action_method:
                raise ValueError(f"Action {action_name} not found on {agent_name}")

            # Sprint 43 H11: enforce depends_on + check agent state
            # before invoking the action. The previous engine did
            # neither — a step could run before its dependencies
            # were ready, and a FAULTED agent would still be
            # invoked.
            self._check_depends_on(step, state, i, steps)
            self._check_agent_state(agent_name, agent)

            # Pass inputs and current state to the agent
            result = action_method(inputs=inputs, state=state)
            state[step_id] = result

        print(f"Workflow '{workflow_data.get('name')}' completed.")
        return state


class WorkflowDependencyError(RuntimeError):
    """Raised when a step's depends_on references a step that
    hasn't run yet (or doesn't exist)."""


class WorkflowAgentFaultError(RuntimeError):
    """Raised when a step's agent is in a non-runnable state
    (FAULTED, STOPPED). The step is dead; the operator must
    restart the agent, skip the step, or mark it optional."""
