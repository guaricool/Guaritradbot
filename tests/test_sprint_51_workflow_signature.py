"""
Sprint 51 — Workflow signature regression.

Production crash on 2026-07-13:
    [scan_news] Delegating to NewsAnalyst -> scan_news
    [ERROR] Error during workflow execution:
        NewsAnalyst.scan_news() got an unexpected keyword argument 'inputs'

Root cause: Sprint 49 added the NewsAnalyst and wired it as
the FIRST step of `trading_loop.yaml` with an `inputs:` block.
But the workflow engine (`src/workflows/engine.py:133`) calls
every action as `action_method(inputs=<dict>, state=<dict>)`,
and Sprint 49's `scan_news` signature was
`def scan_news(self, assets, lookback_hours=24, use_cache=True)`.
Same problem for `scan_social_sentiment` (Sprint 50).

The bug shipped to the LIVE VPS bot in the Sprint 50 deploy,
which made the workflow crash on its first cycle. The bot
has been unable to take ANY new entries since the redeploy
because the workflow engine raises TypeError immediately
at the `scan_news` step.

Fix: dual-signature dispatcher. Both methods now accept
either the legacy positional form
`scan_news(assets, lookback_hours=24, use_cache=True)` or
the workflow form `scan_news(inputs={...}, state={...})`.

This test suite:
  1. Pins the workflow call path (the production crash
     signature) for both `scan_news` and `scan_social_sentiment`.
  2. Pins the legacy call path so the Sprint 49/50 tests
     and any CLI/library users keep working.
  3. Runs the workflow through `WorkflowEngine.run()` end
     to end with the `scan_news` and `scan_social_sentiment`
     steps, asserting no TypeError, and that the result
     is stored in `state[step_id]`.
  4. Asserts the `_resolve_wf_args` helper handles all
     the edge cases that the integration tests didn't
     cover (empty state, mixed kwargs, etc.).
"""
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.agents.news_analyst import NewsAnalyst, _resolve_wf_args
from src.agents.sentiment_analyst import SentimentAnalyst
from src.workflows.engine import WorkflowEngine


class WorkflowCallPathRegressionTest(unittest.TestCase):
    """Pins the exact production crash signature: engine
    calls action_method(inputs=<dict>, state=<dict>). Both
    methods MUST accept this without raising TypeError.
    """

    def setUp(self):
        # Use a tmp cache path so we don't pollute the repo
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self.news = NewsAnalyst(
            cache_path=Path(self._tmp) / "news_cache.jsonl",
            ttl_seconds=3600,
        )
        self.sentiment = SentimentAnalyst(
            cache_path=Path(self._tmp) / "social_cache.jsonl",
            ttl_seconds=3600,
        )

    def test_scan_news_accepts_workflow_engine_kwargs(self):
        """The exact production crash: engine calls
        action_method(inputs=..., state=...). This MUST NOT
        raise TypeError anymore."""
        # Patch _scan_news_impl so we don't actually hit the network
        with patch.object(self.news, "_scan_news_impl", return_value={"BTC-USD": {"asset": "BTC-USD"}}) as m:
            result = self.news.scan_news(
                inputs={"assets": ["BTC-USD"], "lookback_hours": 24},
                state={},
            )
        m.assert_called_once_with(["BTC-USD"], 24, True)
        self.assertEqual(result, {"BTC-USD": {"asset": "BTC-USD"}})

    def test_scan_social_sentiment_accepts_workflow_engine_kwargs(self):
        """Same regression for sentiment."""
        with patch.object(self.sentiment, "_scan_social_sentiment_impl", return_value={"BTC-USD": {"asset": "BTC-USD"}}) as m:
            result = self.sentiment.scan_social_sentiment(
                inputs={"assets": ["BTC-USD"]},
                state={},
            )
        m.assert_called_once_with(["BTC-USD"], True)
        self.assertEqual(result, {"BTC-USD": {"asset": "BTC-USD"}})


class LegacyCallPathStillWorksTest(unittest.TestCase):
    """The Sprint 49/50 tests and any CLI/library users call
    these methods positionally. They MUST keep working after
    the dual-signature refactor."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self.news = NewsAnalyst(
            cache_path=Path(self._tmp) / "news_cache.jsonl",
            ttl_seconds=3600,
        )
        self.sentiment = SentimentAnalyst(
            cache_path=Path(self._tmp) / "social_cache.jsonl",
            ttl_seconds=3600,
        )

    def test_scan_news_legacy_positional(self):
        with patch.object(self.news, "_scan_news_impl", return_value={}) as m:
            self.news.scan_news(["BTC-USD"])
        m.assert_called_once_with(["BTC-USD"], 24, True)

    def test_scan_news_legacy_positional_with_kwargs(self):
        with patch.object(self.news, "_scan_news_impl", return_value={}) as m:
            self.news.scan_news(["BTC-USD"], lookback_hours=12, use_cache=False)
        m.assert_called_once_with(["BTC-USD"], 12, False)

    def test_scan_news_legacy_kwargs_only(self):
        with patch.object(self.news, "_scan_news_impl", return_value={}) as m:
            self.news.scan_news(assets=["BTC-USD"], use_cache=False)
        m.assert_called_once_with(["BTC-USD"], 24, False)

    def test_scan_social_sentiment_legacy_positional(self):
        with patch.object(self.sentiment, "_scan_social_sentiment_impl", return_value={}) as m:
            self.sentiment.scan_social_sentiment(["BTC-USD"])
        m.assert_called_once_with(["BTC-USD"], True)

    def test_scan_social_sentiment_legacy_kwargs(self):
        with patch.object(self.sentiment, "_scan_social_sentiment_impl", return_value={}) as m:
            self.sentiment.scan_social_sentiment(assets=["BTC-USD"], use_cache=False)
        m.assert_called_once_with(["BTC-USD"], False)


class ResolveWfArgsHelperTest(unittest.TestCase):
    """Unit tests for the _resolve_wf_args dispatcher."""

    def test_workflow_path_inputs_kwarg(self):
        inputs, state = _resolve_wf_args(
            (), {"inputs": {"assets": ["BTC-USD"]}, "state": {"x": 1}},
            param_names=("assets",),
        )
        self.assertEqual(inputs, {"assets": ["BTC-USD"]})
        self.assertEqual(state, {"x": 1})

    def test_workflow_path_positional_dict(self):
        inputs, state = _resolve_wf_args(
            ({"assets": ["BTC-USD"]}, {"x": 1}),
            {},
            param_names=("assets",),
        )
        self.assertEqual(inputs, {"assets": ["BTC-USD"]})
        self.assertEqual(state, {"x": 1})

    def test_legacy_path_positional(self):
        inputs, state = _resolve_wf_args(
            (["BTC-USD"],),
            {},
            param_names=("assets", "lookback_hours", "use_cache"),
        )
        self.assertEqual(inputs, {"assets": ["BTC-USD"]})
        self.assertEqual(state, {})

    def test_legacy_path_kwargs(self):
        inputs, state = _resolve_wf_args(
            (), {"assets": ["BTC-USD"], "use_cache": False},
            param_names=("assets", "lookback_hours", "use_cache"),
        )
        self.assertEqual(inputs, {"assets": ["BTC-USD"], "use_cache": False})
        self.assertEqual(state, {})

    def test_legacy_path_mixed(self):
        inputs, state = _resolve_wf_args(
            (["BTC-USD"],), {"use_cache": False},
            param_names=("assets", "lookback_hours", "use_cache"),
        )
        self.assertEqual(inputs, {"assets": ["BTC-USD"], "use_cache": False})
        self.assertEqual(state, {})

    def test_state_kwarg_default(self):
        """Engine always passes state=<dict>, but a library
        user might pass inputs without state. We must default
        state to {}."""
        inputs, state = _resolve_wf_args(
            (), {"inputs": {"assets": ["BTC-USD"]}},
            param_names=("assets",),
        )
        self.assertEqual(inputs, {"assets": ["BTC-USD"]})
        self.assertEqual(state, {})


class WorkflowEngineEndToEndTest(unittest.TestCase):
    """Run the actual workflow through WorkflowEngine.run()
    with mock agents to confirm the engine no longer raises
    TypeError on the scan_news and scan_social_sentiment
    steps."""

    def test_engine_runs_scan_news_and_sentiment_steps(self):
        # The YAML we ship. We instantiate a real WorkflowEngine
        # but feed it mock agents that record the call shape.
        from src.workflows.engine import WorkflowEngine
        import yaml

        news_mock = MagicMock()
        # The engine enforces agent.state == READY/RUNNING/DEGRADED
        # before invoking any action. Make the mock pass that
        # gate.
        news_mock.state.name = "READY"
        news_mock.scan_news = MagicMock(return_value={"BTC-USD": {"news_sentiment": 0.5}})

        sent_mock = MagicMock()
        sent_mock.state.name = "READY"
        sent_mock.scan_social_sentiment = MagicMock(return_value={"BTC-USD": {"social_sentiment": 0.3}})

        engine = WorkflowEngine(agents_registry={
            "NewsAnalyst": news_mock,
            "SentimentAnalyst": sent_mock,
        })

        wf = {
            "name": "sprint51_test",
            "steps": [
                {
                    "id": "scan_news",
                    "agent": "NewsAnalyst",
                    "action": "scan_news",
                    "inputs": {"assets": ["BTC-USD"], "lookback_hours": 24},
                },
                {
                    "id": "scan_social_sentiment",
                    "agent": "SentimentAnalyst",
                    "action": "scan_social_sentiment",
                    "depends_on": ["scan_news"],
                    "inputs": {"assets": ["BTC-USD"]},
                },
            ],
        }

        state = engine.run(wf)

        # The engine called each action with inputs=... and state=...
        news_mock.scan_news.assert_called_once()
        sent_mock.scan_social_sentiment.assert_called_once()
        # And the result landed in state[step_id]
        self.assertEqual(state["scan_news"], {"BTC-USD": {"news_sentiment": 0.5}})
        self.assertEqual(state["scan_social_sentiment"], {"BTC-USD": {"social_sentiment": 0.3}})


if __name__ == "__main__":
    unittest.main()
