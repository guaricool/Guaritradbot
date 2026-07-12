"""
Sprint 46R (audit M9): regression tests for the generic
exception handler in src.execution.scheduler.

Audit M9's exact wording:
  "el path generico de error del scheduler (scheduler.py:182-185)
   no publica SYSTEM_ERROR (sin alerta Telegram ante crash de
   un agente)"

Pre-46R, the `except Exception` branch in `Scheduler.run` only
wrote one WORKFLOW_CYCLE_ERROR audit event and logged to stdout
- it did NOT publish SYSTEM_ERROR, so a cycle that crashed
unexpectedly (a bug in a step body, a library exception, etc.)
silently disappeared with no Telegram alert. The
WorkflowAgentFaultError / WorkflowDependencyError branch above
had the SYSTEM_ERROR publish; the generic branch did not.

These tests cover:
  1. Generic exception → SYSTEM_ERROR publish + audit event
  2. WorkflowDependencyError → SYSTEM_ERROR publish (regression
     guard for the existing 157-181 branch)
  3. Successful cycle → no SYSTEM_ERROR, no audit event
  4. SYSTEM_ERROR publish itself failing is graceful
     (logged but does not crash the cycle)
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.execution.scheduler import EpochScheduler


def _make_scheduler(event_bus=None, audit=None):
    """Build a minimal Scheduler for testing run()'s exception
    handling without going through the full main.py startup."""
    s = EpochScheduler(
        engine=MagicMock(),
        workflow_data={"id": "test", "steps": []},
        audit=audit,
        event_bus=event_bus,
    )
    # Stub the engine so we control the cycle outcome without
    # actually running the YAML.
    s.engine = MagicMock()
    return s


class SchedulerGenericErrorPathTest(unittest.TestCase):
    def test_generic_exception_publishes_system_error(self):
        """M9: generic Exception in the cycle MUST publish SYSTEM_ERROR."""
        event_bus = MagicMock()
        audit = MagicMock()
        s = _make_scheduler(event_bus=event_bus, audit=audit)

        # Make the engine raise a generic exception.
        s.engine.run.side_effect = RuntimeError("oh no, an unexpected bug")
        s._save_state = MagicMock()

        s.job()

        # Audit got the WORKFLOW_CYCLE_ERROR event.
        audit.append.assert_called_once()
        evt_name, payload = audit.append.call_args.args
        self.assertEqual(evt_name, "WORKFLOW_CYCLE_ERROR")
        self.assertIn("oh no", payload["error"])

        # SYSTEM_ERROR was published.
        event_bus.publish.assert_called_once()
        evt_name, payload = event_bus.publish.call_args.args
        self.assertEqual(evt_name, "SYSTEM_ERROR")
        self.assertEqual(payload["kind"], "WORKFLOW_CYCLE_ERROR")
        self.assertIn("⛔", payload["error"])
        self.assertIn("oh no", payload["error"])

    def test_workflow_dependency_error_publishes_system_error(self):
        """Regression guard for the pre-existing 157-181 branch
        (Sprint 45 N6/H11 fix). Make sure it still fires
        SYSTEM_ERROR + audit."""
        from src.workflows.engine import WorkflowDependencyError

        event_bus = MagicMock()
        audit = MagicMock()
        s = _make_scheduler(event_bus=event_bus, audit=audit)

        s.engine.run.side_effect = WorkflowDependencyError("missing step dep")
        s._save_state = MagicMock()

        s.job()

        # Both: audit + SYSTEM_ERROR.
        audit.append.assert_called_once()
        self.assertEqual(audit.append.call_args.args[0], "WORKFLOW_CYCLE_ABORTED")
        event_bus.publish.assert_called_once()
        self.assertEqual(event_bus.publish.call_args.args[0], "SYSTEM_ERROR")
        self.assertEqual(
            event_bus.publish.call_args.args[1]["kind"],
            "WORKFLOW_CYCLE_ABORTED",
        )

    def test_successful_cycle_emits_no_system_error(self):
        """Happy path: engine returns cleanly, no SYSTEM_ERROR,
        no extra audit event."""
        event_bus = MagicMock()
        audit = MagicMock()
        s = _make_scheduler(event_bus=event_bus, audit=audit)
        s.engine.run.return_value = {"some": "state"}
        s._save_state = MagicMock()

        s.job()

        # No SYSTEM_ERROR published.
        event_bus.publish.assert_not_called()
        # Audit was not touched by the scheduler for cycle errors.
        cycle_error_calls = [
            c for c in audit.append.call_args_list
            if c.args[0] in ("WORKFLOW_CYCLE_ERROR", "WORKFLOW_CYCLE_ABORTED")
        ]
        self.assertEqual(cycle_error_calls, [],
                         "Successful cycle must not write cycle-error audit events")

    def test_system_error_publish_failure_does_not_crash_cycle(self):
        """If event_bus.publish itself raises (e.g. broken bus),
        the scheduler should log and continue - we MUST NOT
        crash the cycle from inside the error handler."""
        event_bus = MagicMock()
        event_bus.publish.side_effect = RuntimeError("event bus is broken")
        audit = MagicMock()
        s = _make_scheduler(event_bus=event_bus, audit=audit)

        s.engine.run.side_effect = RuntimeError("engine failed")
        s._save_state = MagicMock()

        # Must not raise.
        s.job()

        # Audit still got the cycle-error event (the publish
        # failure happens AFTER the audit.append call).
        audit.append.assert_called_once()
        # The event_bus.publish was attempted exactly once
        # (and the scheduler absorbed its failure).
        event_bus.publish.assert_called_once()


if __name__ == "__main__":
    unittest.main()
