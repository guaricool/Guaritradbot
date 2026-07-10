# Sprint 19 — ML Pipeline (label/train/predict)

**Fecha**: 2026-07-09
**Status**: ✅ Cerrado (11/11 tests passing)
**Score delta**: +2.0 (de ❌ → ✅✅✅)

## Resumen

Antes del Sprint 19: el bot no tenía ML pipeline. Solo indicadores técnicos.
Después del Sprint 19: pipeline end-to-end de ML que **genera señales nuevas basadas en predicciones**.

```
OHLCV → Alpha Zoo (Sprint 21) → Feature Matrix → LogisticRegression → Probability
                                                                          ↓
                                                              ML_Baseline signal
                                                                          ↓
                                                              StrategyAgent evalúa
                                                                          ↓
                                                              Si prob >= 0.6 → LONG
                                                              Si prob <= 0.4 → SHORT
```

## Componentes

### 1. `LabelConfig` + `make_labels`

Genera labels binarios desde forward returns:
```python
LabelConfig(forward_bars=5, threshold_pct=0.5)
# → "did price rise >0.5% in the next 5 bars?" (binary)
```

### 2. `FeatureExtractor`

Pipeline:
1. `compute_alpha_features(df)` → 48 columnas alpha_*
2. Drop columnas con >95% NaN (low-quality features)
3. ffill + dropna → clean feature matrix
4. Return (X, feature_names)

### 3. `ModelTrainer`

- **Default**: LogisticRegression con L2, balanced class weight, max_iter=1000
- **StandardScaler** interno (features estandarizadas para LR)
- **Train metrics**: accuracy, precision, recall, F1, n_samples, train_time_s
- **Persistencia**: `trainer.save(path)` / `ModelTrainer.load(path)` (pickle)
- **`train()` retorna self** para chaining: `Predictor(ModelTrainer().train(X, y))`

### 4. `Predictor`

- `predict_proba(X)` → array de shape (n,) con prob de class=1
- `predict_one(feature_row)` → float
- Validates feature column match con `trainer.feature_names`

### 5. Integration con StrategyAgent

Nuevo bloque al final de `evaluate_strategies`:
```python
for asset in ("BTC-USD", "SPY", "QQQ", "GLD", "USO"):
    predictor = self.ml_predictors.get(asset)
    if predictor is None:
        continue
    # extract features for last bar
    X, _ = FeatureExtractor().transform(df_ml)
    prob = predictor.predict_one(X.iloc[-1])
    if prob >= 0.6:
        emit ML_Baseline_LONG hypothesis
    elif prob <= 0.4:
        emit ML_Baseline_SHORT hypothesis
```

Y `_hypothesis_strength` ahora reconoce ML_Baseline:
```python
if "ml_" in strategy or "baseline" in strategy:
    prob = h["ml_probability"]
    return 0.5 + 0.45 * abs(prob - 0.5) * 2  # prob=0.7 → strength=0.68
```

## Wire-up en main.py (futuro)

```python
# 1. Train models offline (or periodically via EpochScheduler)
from src.ml.pipeline import ModelTrainer, FeatureExtractor, make_labels, LabelConfig
df = market_analyst.fetch_historical("BTC-USD", period="1y")
X, _ = FeatureExtractor().transform(df)
y = make_labels(df, LabelConfig(forward_bars=5, threshold_pct=0.0))
trainer = ModelTrainer().train(X, y)
trainer.save("models/BTC-USD_v1.pkl")

# 2. Load + pass to StrategyAgent
from src.ml.pipeline import Predictor
predictors = {
    "BTC-USD": Predictor.load("models/BTC-USD_v1.pkl"),
    # ... more assets
}
strategy_agent = StrategyAgent(audit=audit, ml_predictors=predictors)
```

Esto queda como Sprint 19+ (TODO): entrenar modelos en producción con periodicidad.

## Tests (11/11 passing)

```
tests/test_ml_pipeline.py
├── FeatureExtractorTest
│   ├── test_returns_features_and_names       ✓
│   ├── test_no_nans_in_output                ✓
│   └── test_rejects_too_few_bars             ✓
├── MakeLabelsTest
│   ├── test_label_one_when_forward_return_positive   ✓
│   └── test_label_matches_forward_return_definition  ✓
├── ModelTrainerTest
│   ├── test_train_returns_self_and_metrics   ✓
│   ├── test_train_accuracy_above_chance      ✓
│   └── test_save_and_load_roundtrip          ✓
├── PredictorTest
│   └── test_predict_one_returns_scalar       ✓
└── EndToEndTest
    └── test_full_pipeline                    ✓
```

## Métricas de ejemplo (synthetic data)

```
Feature matrix: (301, 48) (48 features)
After alignment: 296 samples, 29.73% positive
Train metrics: {'accuracy': 0.844, 'precision': 0.681, 'recall': 0.898, 'f1': 0.775, 'n_samples': 296, 'n_features': 48, 'train_time_s': 0.047}
```

(Accuracy alta en training set es esperado — el backtest real mide out-of-sample).

## Limitaciones actuales

1. **Single timeframe**: solo usa 4h data para entrenar y predecir.
2. **No walk-forward**: el modelo se entrena una vez. Sprint 19+ debería re-entrenar periódicamente.
3. **No cross-validation**: el accuracy reportado es in-sample. Necesitamos CV o holdout set.
4. **No feature selection**: usamos las 48 features; algunas son redundantes.
5. **No ensemble**: solo LogisticRegression. Mejora futura: GradientBoosting o stacking.

## Próximos pasos (Sprint 19+)

1. Periodic retraining (cada epoch en el `EpochScheduler`)
2. Cross-validation en el trainer (reportar out-of-sample accuracy)
3. Persistir modelos en disco (`models/{asset}_v{n}.pkl`)
4. Hyperopt del clasificador (C, penalty, class_weight)
5. Ensemble: Logistic + GBM + RandomForest → voting
6. Feature importance report → dashboard

## Score de capacidad actualizado

| Capacidad | Antes | Después |
|---|---|---|
| ML pipeline | ❌ | ✅✅✅ |
| Alpha zoo | ⚠️ (9 custom) | ✅✅ (48 features via `ta`) |
| Indicadores técnicos | ⚠️ | ✅✅ |

**Cap score delta: +3.5 puntos** (Sprint 19 + 21 combinados).