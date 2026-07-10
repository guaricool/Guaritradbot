# AlphaZoo (Sprint 21)

**Archivo**: `src/features/alpha_zoo.py`
**Fecha**: 2026-07-09
**Score delta**: +0.5 (de ⚠️ → ✅✅)

## Resumen

Módulo que añade 48+ indicadores técnicos (alpha features) a un dataframe OHLCV en una sola llamada. Reemplaza los 9 indicadores custom que teníamos con una library battle-tested (`ta`).

## API

```python
from src.features.alpha_zoo import compute_alpha_features, list_alpha_features

# df tiene columnas: Open, High, Low, Close, Volume (todas o algunas)
out = compute_alpha_features(df)

# out tiene las columnas originales + 48 columnas alpha_*
features = list_alpha_features(out)  # ['alpha_rsi_14', 'alpha_macd', ...]
```

## Features disponibles (48)

### Momentum (16)
- **RSI** (14, 7): Relative Strength Index
- **Stochastic** (K, signal): Stochastic Oscillator
- **Williams %R** (14): Williams Percent Range
- **ROC** (10, 20): Rate of Change
- **CCI** (20): Commodity Channel Index (manual impl)
- **MFI** (14): Money Flow Index (manual impl, ta no lo incluye)
- **AO**: Awesome Oscillator
- **UO**: Ultimate Oscillator
- **TSI**: True Strength Index
- **PPO** (line, signal, hist): Percentage Price Oscillator

### Trend (16)
- **MACD** (line, signal, diff): Moving Average Convergence Divergence
- **EMA** (9, 21, 50, 200): Exponential Moving Averages
- **SMA** (20, 50): Simple Moving Averages
- **ADX** (line, +DI, -DI): Average Directional Index
- **Aroon** (up, down): Aroon Indicator
- **PSAR**: Parabolic SAR
- **Ichimoku** (a, b): Ichimoku Cloud conversion + base line
- **KAMA**: Kaufman Adaptive Moving Average

### Volatility (11)
- **Bollinger Bands** (high, low, width, %b): 20-period, 2σ
- **Keltner Channel** (high, low): 20-period
- **Donchian Channel** (high, low): 20-period
- **ATR** (14): Average True Range
- **ATR Ratio**: ATR / Close (normalized)
- **Ulcer Index**: Drawdown-based volatility measure

### Volume (7)
- **OBV**: On Balance Volume
- **CMF** (20): Chaikin Money Flow
- **EOM** (14): Ease of Movement
- **Force Index** (13): Elder's Force Index
- **VPT**: Volume Price Trend
- **NVI**: Negative Volume Index
- **VWAP** (20): Volume Weighted Average Price

## Selección de features

Puedes elegir qué categorías incluir:

```python
out = compute_alpha_features(
    df,
    include_momentum=True,
    include_trend=True,
    include_volatility=True,
    include_volume=True,
)
```

Si tu data no tiene `Volume`, los indicadores de volumen se saltan gracefully.

## Por qué `ta` y no TA-Lib

| Library | Indicators | Windows install | Veredicto |
|---|---|---|---|
| `ta-lib` | 200+ | ❌ Requiere compilación C | Descartado |
| `pandas_ta` | 130+ | ❌ Requiere Python 3.12+ | Descartado |
| `ta` (pure-Python fork) | 130+ | ✅ `pip install ta` | **Elegido** |

`ta` cubre todo lo que necesitamos para trading medio-frecuencia, sin los dolores de cabeza de TA-Lib en Windows + Linux VPS mixtos.

## Tests

8/8 passing en `tests/test_alpha_zoo.py`:
- Cantidad correcta de features (>=50 según catálogo)
- Columnas OHLCV preservadas
- Convención de nombres (`alpha_*`)
- Sin NaN en última fila (warmup manejado)
- Rangos válidos (RSI 0-100, BB brackets 95% of bars)
- Funciona sin Volume
- Skip selectivo por categoría

## Consumidores

- **`src/ml/pipeline.py`** (Sprint 19): `FeatureExtractor` consume `compute_alpha_features` y limpia para ML.
- **`src/agents/strategy_agent.py`**: aún no consume directamente (las hipótesis técnicas siguen usando los indicadores custom). Futuras versiones podrían migrar a alpha zoo.

## Lecciones aprendidas durante implementación

1. **`ta.momentum.CCIIndicator` no existe** → CCI implementado a mano (fórmula estándar).
2. **`ta.trend.AroonIndicator` requiere (high, low)** no (close) como sugiere la doc.
3. **`ta.trend.MFIIndicator` no existe** → MFI implementado a mano.
4. **Warning de PSAR (`__setitem__`)** es interno de la librería `ta`, no bug nuestro.

Ver [[../Bugs/B017_micro_account_death_loop]] para el contexto de por qué empezamos a necesitar más features.