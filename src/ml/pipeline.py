"""
Sprint 19 — ML Feature Pipeline.

Pipeline: OHLCV → Alpha Zoo (Sprint 21) → Feature Matrix → ML Model → Predict

Three components:
1. `FeatureExtractor` — turn OHLCV bars into feature vectors using the alpha zoo
2. `ModelTrainer` — train a baseline classifier (LogisticRegression by default)
3. `Predictor` — load a trained model and predict probabilities on new bars

The label we predict is a binary classification: did the forward N-bar return
exceed a threshold? (default: positive return in next 5 bars).

Design principles:
- Deterministic seed for reproducibility
- Pure sklearn — no xgboost dependency (smaller install footprint)
- NaN-safe (drops leading NaN, fills with median)
- Train/predict API is split so the model can be trained offline and
  served at inference time without retraining.
"""
from __future__ import annotations
import os
import pickle
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from src.features.alpha_zoo import compute_alpha_features, list_alpha_features


@dataclass
class LabelConfig:
    """Configuration for how we generate labels from forward returns."""
    forward_bars: int = 5         # how many bars ahead to look
    threshold_pct: float = 0.0    # 0.0 = "did price go up at all?"


def make_labels(df: pd.DataFrame, cfg: LabelConfig) -> pd.Series:
    """
    Generate binary labels based on forward return.

    Label = 1 if `df['Close'].shift(-cfg.forward_bars) > df['Close'] * (1 + threshold/100)`
            0 otherwise.

    Returns a Series aligned to df.index. The last `forward_bars` rows will be NaN
    (we don't have future data for them).
    """
    forward = df["Close"].shift(-cfg.forward_bars)
    label = (forward > df["Close"] * (1.0 + cfg.threshold_pct / 100.0)).astype(float)
    # Last forward_bars rows are NaN (no future data)
    label.iloc[-cfg.forward_bars:] = np.nan
    return label


class FeatureExtractor:
    """
    Convert raw OHLCV bars into a clean feature matrix for ML.

    Pipeline: df → compute_alpha_features → dropna → standardize → X
    """

    def __init__(self, dropna_threshold: float = 0.95):
        self.dropna_threshold = dropna_threshold

    def transform(self, df: pd.DataFrame) -> tuple:
        """
        Returns:
            (X_df, feature_names): cleaned feature matrix + list of column names
        """
        if df is None or len(df) < 50:
            raise ValueError(f"Need at least 50 bars, got {len(df) if df is not None else 0}")

        # Step 1: add alpha features
        enriched = compute_alpha_features(df)
        feature_cols = list_alpha_features(enriched)

        if not feature_cols:
            raise ValueError("No alpha features computed — check input OHLCV columns")

        # Step 2: select feature columns
        X = enriched[feature_cols].copy()

        # Step 3: drop columns that are mostly NaN
        nan_ratio = X.isna().mean()
        good_cols = nan_ratio[nan_ratio < self.dropna_threshold].index.tolist()
        X = X[good_cols]

        # Step 4: forward-fill then drop remaining leading NaN rows
        X = X.ffill().dropna()

        return X, good_cols


class ModelTrainer:
    """
    Train a baseline classifier on alpha features.

    Default: LogisticRegression with L2 regularization. Cheap, robust, and
    gives calibrated probabilities out of the box (vs RandomForest which
    tends to be over-confident on small datasets).

    For better accuracy, swap in GradientBoostingClassifier or XGBoost
    later (kept out of the default install to avoid heavy dependencies).
    """

    def __init__(
        self,
        model_type: str = "logistic",
        C: float = 1.0,
        random_state: int = 42,
    ):
        self.model_type = model_type
        self.random_state = random_state
        self.scaler = StandardScaler()
        self.model = self._build_model(C)
        self.feature_names: List[str] = []
        self.trained_at: Optional[float] = None
        self.train_metrics: dict = {}

    def _build_model(self, C: float):
        if self.model_type == "logistic":
            return LogisticRegression(
                C=C, max_iter=1000, random_state=self.random_state,
                class_weight="balanced",  # handle class imbalance
            )
        elif self.model_type == "random_forest":
            from sklearn.ensemble import RandomForestClassifier
            return RandomForestClassifier(
                n_estimators=100, max_depth=5, random_state=self.random_state,
                class_weight="balanced",
            )
        else:
            raise ValueError(f"Unknown model_type: {self.model_type}")

    def train(self, X: pd.DataFrame, y: pd.Series) -> "ModelTrainer":
        """
        Train on (X, y) and return self (so you can chain `Predictor(t.train(X, y))`).

        Args:
            X: feature matrix (rows = samples, cols = features)
            y: binary labels (0 or 1)

        Returns:
            self (with metrics available via .train_metrics).
        """
        if len(X) != len(y):
            raise ValueError(f"X has {len(X)} rows but y has {len(y)}")
        if X.isna().any().any() or y.isna().any():
            raise ValueError("NaN in X or y")

        t0 = time.time()

        # Standardize features (mean=0, std=1) — required for LogisticRegression
        X_scaled = self.scaler.fit_transform(X)
        self.feature_names = list(X.columns)

        # Train
        self.model.fit(X_scaled, y.values)
        self.trained_at = time.time()
        train_time = self.trained_at - t0

        # Quick train metrics
        from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
        y_pred = self.model.predict(X_scaled)
        metrics = {
            "accuracy": float(accuracy_score(y, y_pred)),
            "precision": float(precision_score(y, y_pred, zero_division=0)),
            "recall": float(recall_score(y, y_pred, zero_division=0)),
            "f1": float(f1_score(y, y_pred, zero_division=0)),
            "n_samples": int(len(X)),
            "n_features": int(X.shape[1]),
            "train_time_s": round(train_time, 3),
            "model_type": self.model_type,
        }
        self.train_metrics = metrics
        return self

    def save(self, path: str) -> None:
        """Persist model + scaler + feature_names to disk."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "model": self.model,
                "scaler": self.scaler,
                "feature_names": self.feature_names,
                "trained_at": self.trained_at,
                "train_metrics": self.train_metrics,
                "model_type": self.model_type,
            }, f)

    @staticmethod
    def load(path: str) -> "ModelTrainer":
        """Load a persisted trainer from disk."""
        with open(path, "rb") as f:
            data = pickle.load(f)
        trainer = ModelTrainer(model_type=data["model_type"])
        trainer.model = data["model"]
        trainer.scaler = data["scaler"]
        trainer.feature_names = data["feature_names"]
        trainer.trained_at = data["trained_at"]
        trainer.train_metrics = data["train_metrics"]
        return trainer


class Predictor:
    """
    Serve predictions from a trained ModelTrainer.

    Usage:
        trainer = ModelTrainer().train(X, y)
        trainer.save("models/btc_v1.pkl")
        predictor = Predictor.load("models/btc_v1.pkl")
        prob = predictor.predict_one(feature_row)  # returns float in [0, 1]
    """

    def __init__(self, trainer: ModelTrainer):
        self.trainer = trainer

    @staticmethod
    def load(path: str) -> "Predictor":
        return Predictor(ModelTrainer.load(path))

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """
        Predict probability of class=1 (positive forward return).
        Returns array of shape (n_samples,) with values in [0, 1].
        """
        # Ensure columns match training
        missing = set(self.trainer.feature_names) - set(X.columns)
        if missing:
            raise ValueError(f"Missing features: {missing}")
        X_aligned = X[self.trainer.feature_names]
        X_scaled = self.trainer.scaler.transform(X_aligned)
        return self.trainer.model.predict_proba(X_scaled)[:, 1]

    def predict_one(self, feature_row: pd.Series) -> float:
        """Predict probability for a single feature vector (pd.Series)."""
        df_row = feature_row.to_frame().T
        return float(self.predict_proba(df_row)[0])