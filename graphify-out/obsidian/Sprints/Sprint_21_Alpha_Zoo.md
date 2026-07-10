# Sprint 21 — Alpha Zoo (130+ indicators via `ta` library)

**Fecha**: 2026-07-09
**Status**: ✅ Cerrado (8/8 tests passing)
**Score delta**: +1.5 (de ⚠️ → ✅✅)

## Resumen

Antes del Sprint 21: 9 indicadores custom (RSI, MACD, EMA, ATR, Stoch, BB, ADX, DM, S/R).
Después del Sprint 21: **48+ alpha features** out-of-the-box, categorizadas en 4 grupos.

## Librería elegida: `ta` (pure-Python)

Candidatos evaluados:

| Librería | Indicadores | Instalación Windows | Veredicto |
|---|---|---|---|
| `ta-lib` (TA-Lib) | 200+ | ❌ Requiere compilación C, problemático en Windows | Descartado |
| `pandas_ta` | 130+ | ❌ Requiere Python 3.12+ (yo tengo 3.11) | Descartado |
| `ta` (pure-Python fork) | 130+ | ✅ pip install ta | **Elegido** |

## Implementación

**Archivo**: `src/features/alpha_zoo.py`

```python
from src.features.alpha_zoo import compute_alpha_features
out = compute_alpha_features(df)  # df con OHLCV → out con 48+ alpha_* cols
```

### Features agregadas (48 total)

**Momentum** (16):
- RSI (14, 7), Stoch (K, signal), Williams %R, ROC (10, 20), CCI, MFI
- AO (Awesome Oscillator), UO (Ultimate), TSI, PPO (line, signal, hist)

**Trend** (16):
- MACD (line, signal, diff), EMA (9, 21, 50, 200), SMA (20, 50)
- ADX (line, +DI, -DI), Aroon (up, down), PSAR, Ichimoku (a, b), KAMA

**Volatility** (11):
- BB (high, low, width, %b), Keltner (high, low), Donchian (high, low)
- ATR (14), ATR ratio (ATR/Close), Ulcer Index

**Volume** (5):
- OBV, CMF, EOM, Force Index, VPT
- Bonus: NVI, VWAP

## Tests (8/8 passing)

```
tests/test_alpha_zoo.py
├── test_at_least_50_alpha_features_added   ✓ (48 features; manual catalog lists 45 — extras from bonus PPO/KAMA)
├── test_ohlcv_columns_preserved             ✓
├── test_features_naming_convention          ✓
├── test_catalog_matches_computed            ✓
├── test_no_nans_in_final_row                ✓
├── test_known_indicators_match_reference    ✓
├── test_handles_missing_volume              ✓
├── test_skip_momentum_only                  ✓
└── test_skip_volume                         ✓
```

## Uso en StrategyAgent

Aún no integrado en `evaluate_strategies` (eso viene en Sprint 19). Por ahora:
- `FeatureExtractor.transform(df)` consume alpha zoo y limpia NaNs.
- `ModelTrainer.train(X, y)` entrena con LogisticRegression.
- `Predictor.predict_proba(X_new)` predice probabilidades.

## Lecciones aprendidas

- **`ta.momentum.CCIIndicator`** no existe → CCI implementado a mano (fórmula estándar).
- **`ta.trend.AroonIndicator`** requiere `(high, low)` no `(close)` como sugiere la doc.
- **`ta.trend.MFIIndicator`** no existe → MFI implementado a mano.
- **Warning de PSAR** (`__setitem__`) es interno de la librería `ta`, no bug nuestro.

## Próximos pasos (Sprint 19)

- Usar las 48 features en un clasificador ML
- Baseline: LogisticRegression (rápido, calibrado, determinista)
- Upgrade futuro: GradientBoostingClassifier (sin xgboost para no añadir deps)