# ML Pipeline (Sprint 19)

**Archivos**:
- `src/ml/pipeline.py` — FeatureExtractor, ModelTrainer, Predictor, LabelConfig
- `src/ml/__init__.py`
**Fecha**: 2026-07-09
**Score delta**: +2.0 (de ❌ → ✅✅✅)

## Resumen

Pipeline end-to-end de Machine Learning para generar señales de trading basadas en predicciones estadísticas. Antes del Sprint 19, el bot no tenía ML pipeline (era el único gap fuerte vs intelligent-trading-bot).

## Componentes

### 1. `LabelConfig`

Configuración de cómo se generan los labels desde forward returns:

```python
LabelConfig(forward_bars=5, threshold_pct=0.0)
# label = 1 si (close.shift(-5) > close * (1 + 0/100)) else 0
#        ↑ "did price go up at all in next 5 bars?"
```

Variantes útiles:
- `threshold_pct=0.5`: "did price rise >0.5% in next 5 bars?"
- `forward_bars=10, threshold_pct=1.0`: "did price rise >1% in next 10 bars?"

### 2. `FeatureExtractor`

```python
from src.ml.pipeline import FeatureExtractor
X, feature_names = FeatureExtractor().transform(df)
# X: pd.DataFrame limpio (rows = samples, cols = features)
# feature_names: list de columnas alpha_* usadas
```

Pipeline:
1. `compute_alpha_features(df)` → 48 columnas alpha_*
2. Drop columnas con >95% NaN (low-quality features)
3. ffill + dropna → clean feature matrix
4. Return (X, feature_names)

### 3. `ModelTrainer`

```python
from src.ml.pipeline import ModelTrainer

trainer = ModelTrainer(model_type="logistic")
metrics = trainer.train(X, y)  # returns self
# metrics ahora vive en trainer.train_metrics

trainer.save("models/BTC-USD_v1.pkl")
```

**Default**: LogisticRegression con L2, balanced class weight, max_iter=1000.
**StandardScaler** interno (features estandarizadas para LR).

**Otros modelos disponibles**:
- `model_type="random_forest"`: RandomForestClassifier (100 trees, max_depth=5)
- `model_type="logistic"`: LogisticRegression (default)

Train metrics: accuracy, precision, recall, f1, n_samples, n_features, train_time_s.

### 4. `Predictor`

```python
from src.ml.pipeline import Predictor

predictor = Predictor.load("models/BTC-USD_v1.pkl")
probs = predictor.predict_proba(X_new)  # array of shape (n,) in [0, 1]
prob_one = predictor.predict_one(X_new.iloc[0])  # float
```

Valida que las features de X_new matchen con las del trainer (column names + count).

### 5. Integration con StrategyAgent

`StrategyAgent` ahora acepta `ml_predictors` dict y emite nuevas hipótesis:

```python
from src.ml.pipeline import Predictor
predictors = {
    "BTC-USD": Predictor.load("models/BTC-USD_v1.pkl"),
    "SPY": Predictor.load("models/SPY_v1.pkl"),
}
agent = StrategyAgent(audit=audit, ml_predictors=predictors,
                      ml_long_threshold=0.6, ml_short_threshold=0.4)
```

Nuevas hipótesis:
- `ML_Baseline` LONG si `prob >= 0.6`
- `ML_Baseline` SHORT si `prob <= 0.4`
- (no emite si 0.4 < prob < 0.6 — zona neutral)

Strength score (0..1) refleja qué tan lejos está la prob de 0.5:
- prob=0.5 → strength=0.5 (neutral)
- prob=0.7 → strength=0.68
- prob=0.9 → strength=0.86

## Tests

11/11 passing en `tests/test_ml_pipeline.py`:
- FeatureExtractor returns correct shape + no NaN
- LabelConfig produces correct labels (matches forward return definition)
- ModelTrainer.train() returns self for chaining
- Model accuracy > 50% on synthetic data
- Save/load roundtrip preserves model
- Predictor returns valid probabilities in [0, 1]
- End-to-end pipeline works

## Métricas ejemplo (datos sintéticos)

```
Feature matrix: (301, 48)
After alignment: 296 samples, 29.73% positive
Train metrics: {
  'accuracy': 0.844,
  'precision': 0.681,
  'recall': 0.898,
  'f1': 0.775,
  'train_time_s': 0.047
}
```

## Limitaciones actuales (TODO Sprint 19+)

1. **Single timeframe**: solo usa 4h data.
2. **No walk-forward**: el modelo se entrena una vez.
3. **No cross-validation**: el accuracy reportado es in-sample.
4. **No feature selection**: usa las 48 features.
5. **No ensemble**: solo LogisticRegression o RandomForest.

## Próximos pasos

- Sprint 19+: Periodic retraining (cada epoch en el `EpochScheduler`)
- Cross-validation reportar out-of-sample accuracy
- Persistir modelos en disco con versionado (`models/{asset}_v{n}.pkl`)
- Hyperopt del clasificador (C, penalty, class_weight)
- Ensemble: Logistic + GBM + RandomForest → voting
- Feature importance report → dashboard

## Por qué scikit-learn y no xgboost/lightgbm

- scikit-learn es pure-Python y ya viene con numpy
- LogisticRegression da probabilidades calibradas (vs tree-based que son over-confident)
- Más rápido de entrenar (no necesita GPU)
- Para datasets pequeños (~400 bars), LR es competitivo con GBM

Si después queremos más accuracy, swap a `GradientBoostingClassifier` (también sklearn) o instalar xgboost.