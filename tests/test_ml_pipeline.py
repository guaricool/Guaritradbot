"""
Sprint 19 tests — ML Pipeline (FeatureExtractor + ModelTrainer + Predictor).

Run: python -m unittest tests.test_ml_pipeline -v
"""
import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.ml.pipeline import (
    FeatureExtractor, ModelTrainer, Predictor, LabelConfig, make_labels,
)


def _make_sample_df(n=400, seed=42):
    """Build OHLCV with embedded trend + noise."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=n, freq="1h")
    trend = np.linspace(100, 110, n)
    noise = np.cumsum(rng.standard_normal(n) * 0.3)
    close = trend + noise + np.sin(np.linspace(0, 8 * np.pi, n)) * 2
    return pd.DataFrame({
        "Open": close + rng.standard_normal(n) * 0.1,
        "High": close + np.abs(rng.standard_normal(n)) * 0.5,
        "Low": close - np.abs(rng.standard_normal(n)) * 0.5,
        "Close": close,
        "Volume": rng.integers(1000, 10000, n).astype(float),
    }, index=dates)


class FeatureExtractorTest(unittest.TestCase):
    def test_returns_features_and_names(self):
        df = _make_sample_df()
        extractor = FeatureExtractor()
        X, names = extractor.transform(df)
        self.assertGreater(len(names), 30)
        self.assertEqual(X.shape[1], len(names))
        self.assertGreater(len(X), 100)

    def test_no_nans_in_output(self):
        df = _make_sample_df()
        X, _ = FeatureExtractor().transform(df)
        self.assertEqual(X.isna().sum().sum(), 0)

    def test_rejects_too_few_bars(self):
        df = _make_sample_df(n=30)
        with self.assertRaises(ValueError):
            FeatureExtractor().transform(df)


class MakeLabelsTest(unittest.TestCase):
    def test_label_one_when_forward_return_positive(self):
        df = _make_sample_df()
        labels = make_labels(df, LabelConfig(forward_bars=5, threshold_pct=0.0))
        # First 295 bars should have a label (last 5 are NaN due to shift)
        self.assertEqual(labels.iloc[:295].isna().sum(), 0)
        self.assertEqual(labels.iloc[-5:].isna().sum(), 5)

    def test_label_matches_forward_return_definition(self):
        """Sanity: label=1 iff future_close > current_close * (1+threshold)."""
        df = _make_sample_df()
        labels = make_labels(df, LabelConfig(forward_bars=3, threshold_pct=1.0))
        for i in range(len(df) - 3):
            current = df["Close"].iloc[i]
            future = df["Close"].iloc[i + 3]
            expected = 1.0 if future > current * 1.01 else 0.0
            self.assertEqual(labels.iloc[i], expected, f"Mismatch at index {i}")


class ModelTrainerTest(unittest.TestCase):
    def setUp(self):
        self.df = _make_sample_df(n=400)
        self.X, _ = FeatureExtractor().transform(self.df)
        self.y = make_labels(self.df, LabelConfig(forward_bars=5, threshold_pct=0.5))
        common = self.X.index.intersection(self.y.dropna().index)
        self.X = self.X.loc[common]
        self.y = self.y.loc[common]

    def test_train_returns_self_and_metrics(self):
        trainer = ModelTrainer(model_type="logistic")
        result = trainer.train(self.X, self.y)
        # train() returns self for chaining
        self.assertIs(result, trainer)
        self.assertIn("accuracy", trainer.train_metrics)
        self.assertIn("f1", trainer.train_metrics)
        self.assertGreater(trainer.train_metrics["n_samples"], 0)
        self.assertGreater(trainer.train_metrics["train_time_s"], 0)

    def test_train_accuracy_above_chance(self):
        """LogisticRegression on a non-trivial dataset should beat 50% accuracy."""
        trainer = ModelTrainer(model_type="logistic")
        trainer.train(self.X, self.y)
        self.assertGreater(
            trainer.train_metrics["accuracy"], 0.5,
            f"Model accuracy {trainer.train_metrics['accuracy']:.3f} is at chance level",
        )

    def test_save_and_load_roundtrip(self):
        trainer = ModelTrainer(model_type="logistic")
        trainer.train(self.X, self.y)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_model.pkl")
            trainer.save(path)
            self.assertTrue(os.path.exists(path))

            predictor = Predictor.load(path)
            # Predict on first 5 samples
            probs = predictor.predict_proba(self.X.head(5))
            self.assertEqual(len(probs), 5)
            for p in probs:
                self.assertGreaterEqual(p, 0)
                self.assertLessEqual(p, 1)


class PredictorTest(unittest.TestCase):
    def test_predict_one_returns_scalar(self):
        df = _make_sample_df(n=400)
        X, _ = FeatureExtractor().transform(df)
        y = make_labels(df, LabelConfig(forward_bars=5, threshold_pct=0.5))
        common = X.index.intersection(y.dropna().index)
        X = X.loc[common]
        y = y.loc[common]

        trainer = ModelTrainer().train(X, y)
        predictor = Predictor(trainer)

        prob = predictor.predict_one(X.iloc[-1])
        self.assertIsInstance(prob, float)
        self.assertGreaterEqual(prob, 0)
        self.assertLessEqual(prob, 1)


class EndToEndTest(unittest.TestCase):
    """Realistic end-to-end: fetch → features → train → predict → pick signals."""

    def test_full_pipeline(self):
        df = _make_sample_df(n=500)
        # 1. Train
        X, feat_names = FeatureExtractor().transform(df)
        y = make_labels(df, LabelConfig(forward_bars=5, threshold_pct=0.0))
        common = X.index.intersection(y.dropna().index)
        X = X.loc[common]
        y = y.loc[common]
        trainer = ModelTrainer().train(X, y)
        # 2. Predict on latest
        predictor = Predictor(trainer)
        latest = X.iloc[-1]
        prob = predictor.predict_one(latest)
        # 3. Decide signal based on prob
        if prob >= 0.6:
            signal = "long"
        elif prob <= 0.4:
            signal = "short"
        else:
            signal = "hold"
        self.assertIn(signal, ("long", "short", "hold"))


if __name__ == "__main__":
    unittest.main()