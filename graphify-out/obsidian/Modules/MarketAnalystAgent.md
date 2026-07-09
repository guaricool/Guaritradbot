# MarketAnalystAgent

`src/agents/market_analyst.py`

## Responsabilidad

Descargar datos OHLCV vía **yfinance** y calcular **14 indicadores
técnicos** por asset × timeframe. Publica `MARKET_DATA_READY` al EventBus.

## Indicadores que calcula

| Indicator | Función | Usado por |
|-----------|---------|-----------|
| EMA_20 / EMA_50 | trend | Strategy (cruce de EMAs) |
| RSI (Wilder) | momentum / mean-reversion | Strategy (cruce de RSI) |
| MACD + MACD_Signal | momentum / trend | Strategy (cruce de MACD) |
| ATR_14 | volatilidad → stop loss | Risk (sizing ATR-based) |
| **DI+/DI-/ADX_14** | fuerza de tendencia | (Sprint 7, no usado todavía) |
| **Stoch_K / Stoch_D** | momentum | (Sprint 7) |
| **BB_Upper/Middle/Lower** | S/R dinámico + volatilidad | (Sprint 7) |
| **Support_50 / Resistance_50** | niveles clave | (Sprint 7) |

Los **4 con negrita** vienen del Manual del Buen Trader (PDF2).

## Lifecycle (Sprint 6)

Heredita de `[[Modules/Component_State_Machine]]`:
- `PRE_INITIALIZED → READY` (en `__init__`)
- `READY → STARTING → RUNNING` (en `fetch_and_analyze`)
- Si todos los feeds fallan → `FAULTED`
- Si algunos fallan → `DEGRADED`

Cada transición se loguea + emite al audit ledger.

## Fail-fast

Antes de procesar cada vela, llama a
`[[Modules/Data_Validator]].validate_dataframe(df)`. Si yfinance
devuelve NaN/Inf/high<low, se lanza `DataIntegrityError`, se loguea,
y el componente se degrada a `DEGRADED`.

## Bug B008 cerrado (Sprint 0)

Antes: `tf_map["4h"] = "1h"` silenciaba el resampleo.
Ahora: descarga 60m y resamplea explícitamente a 4h vía
`_resample_ohlcv()`.

## API pública

```python
agent = MarketAnalystAgent(event_bus=event_bus, audit=audit)

# Método principal (lo llama el workflow)
state["analyze_market"] = agent.fetch_and_analyze(inputs, state)

# Helper público (lo llama PositionMonitor en Sprint 2)
df = agent.fetch_one("BTC-USD", interval="1d", period="1mo")
```

## Tests

```bash
python main.py --once
# Output:
# [MarketAnalystAgent] PRE_INITIALIZED → READY (configure())
# [MarketAnalystAgent] READY → STARTING
# [MarketAnalystAgent] STARTING → RUNNING
# ✅ SPY@15m: 780 velas | close=$745.29 | RSI=53.0 | ADX=18.2 | StochK=76.8 | ATR=1.2086
```

## Conecta con

- [[Sprints/Sprint_0_Critical_Bug_Fixes]] — fix RSI Wilder, fix 4h resample
- [[Sprints/Sprint_6_State_Machine_Data_Integrity]] — heredó Component
- [[Sprints/Sprint_7_PDF_Indicators]] — DM/ADX/Stoch/BB/SR añadidos
- [[Modules/Data_Validator]] — fail-fast
- [[Modules/Component_State_Machine]] — FSM
- [[Modules/StrategyAgent]] — consume market_data
- [[Modules/Position_Monitor]] — usa fetch_one()
