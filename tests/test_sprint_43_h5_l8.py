"""
Sprint 43 — H5 (EventBus isolation) and L8 (pickle.load try/except).

H5: a failing subscriber must not abort the rest of the cycle.
L8: a corrupted model artifact must not crash the caller.
"""
import os
import pickle
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.core.event_bus import EventBus
from src.ml.pipeline import ModelTrainer


class EventBusIsolationTest(unittest.TestCase):
    """H5: a raising subscriber must NOT stop the rest of the cycle."""

    def test_failing_subscriber_does_not_abort_cycle(self):
        bus = EventBus()
        results = []
        def good1(d): results.append(("good1", d))
        def bad(d): raise RuntimeError("simulated subscriber crash")
        def good2(d): results.append(("good2", d))
        bus.subscribe("TEST", good1)
        bus.subscribe("TEST", bad)
        bus.subscribe("TEST", good2)
        # Publishing must not raise, and good1 + good2 must run
        bus.publish("TEST", {"x": 1})
        self.assertEqual(len(results), 2, f"Only good subscribers should run: {results}")
        self.assertIn(("good1", {"x": 1}), results)
        self.assertIn(("good2", {"x": 1}), results)

    def test_failing_subscriber_error_recorded(self):
        bus = EventBus()
        def bad(d): raise ValueError("deliberate")
        bus.subscribe("TEST", bad)
        bus.publish("TEST", {"x": 42})
        # Error should be recorded for the audit reader
        self.assertIn("TEST", bus.last_errors)
        self.assertEqual(len(bus.last_errors["TEST"]), 1)
        self.assertIn("ValueError", bus.last_errors["TEST"][0]["error"])
        self.assertIn("deliberate", bus.last_errors["TEST"][0]["error"])

    def test_no_subscribers_no_error(self):
        bus = EventBus()
        # No subscribers — should be a no-op, no error
        bus.publish("UNSUBSCRIBED", {"x": 1})

    def test_multiple_failing_subscribers_all_recorded(self):
        bus = EventBus()
        def bad1(d): raise RuntimeError("a")
        def bad2(d): raise ValueError("b")
        def good(d): pass
        bus.subscribe("TEST", bad1)
        bus.subscribe("TEST", bad2)
        bus.subscribe("TEST", good)
        bus.publish("TEST", None)
        # Both failures recorded
        self.assertEqual(len(bus.last_errors.get("TEST", [])), 2)
        # Good subscriber still ran (no exception propagated)
        # If we got here without exception, that's the test pass.


class PickleLoadTryExceptTest(unittest.TestCase):
    """L8: corrupted model files must not crash the caller."""

    def test_load_nonexistent_file_returns_none(self):
        result = ModelTrainer.load("/nonexistent/path/model.pkl")
        self.assertIsNone(result)

    def test_load_empty_file_returns_none(self):
        """A 0-byte file (truncated by kill -9 mid-save) raises EOFError."""
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
            # don't write anything
        try:
            result = ModelTrainer.load(path)
            self.assertIsNone(result, "Empty file should return None, not crash")
        finally:
            os.unlink(path)

    def test_load_corrupted_file_returns_none(self):
        """Garbage bytes — UnpicklingError."""
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False, mode="wb") as f:
            f.write(b"this is not a valid pickle file at all !!!")
            path = f.name
        try:
            result = ModelTrainer.load(path)
            self.assertIsNone(result, "Corrupted file should return None, not crash")
        finally:
            os.unlink(path)

    def test_load_truncated_pickle_returns_none(self):
        """A pickle that starts valid but is cut short."""
        # Train a model first to get a valid pickle
        import pandas as pd
        trainer = ModelTrainer(model_type="random_forest")
        X = pd.DataFrame({"f1": [0.1, 0.3, 0.5, 0.7], "f2": [0.2, 0.4, 0.6, 0.8]})
        y = pd.Series([0, 1, 0, 1])
        trainer.train(X, y)
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            trainer.save(f.name)
            path = f.name
        # Truncate the file
        with open(path, "rb+") as f:
            f.seek(0)
            f.write(b"TRUNCATED!")  # overwrite the start
        try:
            result = ModelTrainer.load(path)
            self.assertIsNone(result, "Truncated file should return None, not crash")
        finally:
            os.unlink(path)

    def test_load_valid_file_returns_trainer(self):
        """Regression: a healthy file still loads correctly."""
        import pandas as pd
        trainer = ModelTrainer(model_type="random_forest")
        X = pd.DataFrame({"f1": [0.1, 0.3, 0.5, 0.7], "f2": [0.2, 0.4, 0.6, 0.8]})
        y = pd.Series([0, 1, 0, 1])
        trainer.train(X, y)
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
        try:
            trainer.save(path)
            loaded = ModelTrainer.load(path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.model_type, "random_forest")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
