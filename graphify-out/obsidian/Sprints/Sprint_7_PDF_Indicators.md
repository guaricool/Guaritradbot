# Sprint 7 — PDF Indicators

## Objetivo

Aplicar los indicadores técnicos que recomienda el "Manual del Buen
Trader Algorítmico" (PDF2) — no todos estaban en Guaritradbot.

El PDF2 recomienda **7 indicadores**: DM, SMA/EMA, RSI, Estocástico,
MACD, S/R, Bollinger. Antes solo teníamos 3/7.

## Indicadores añadidos en `market_analyst.py`

| Indicador | Función | Uso |
|-----------|---------|-----|
| **DM / ADX** | `_dm()` | Fuerza de tendencia. ADX<20 = sin tendencia, ADX>25 = clara |
| **Estocástico %K/%D** | `_stochastic()` | Momentum. %K>80 sobrecompra, %K<20 sobreventa |
| **Bollinger Bands** | `_bollinger()` | S/R dinámico + volatilidad |
| **Soporte/Resistencia 50** | `_support_resistance()` | Niveles clave del rolling window |

Los indicadores ya existentes (RSI Wilder, MACD, EMA, ATR) **se
mantienen** — son los del PDF2 también.

## Output ejemplo

Cada vela descargada ahora muestra:

```
✅ SPY@15m: 780 velas | close=$745.29 | RSI=53.0 | ADX=18.2 | StochK=76.8 | ATR=1.2086
```

Interpretación de la última vela:
- **RSI 53**: neutral, sin edge
- **ADX 18**: sin tendencia clara (choppy market)
- **StochK 76.8**: cerca de sobrecompra
- **ATR 1.21**: volatilidad ~0.16% del precio

## Estrategia pendiente

Estos indicadores están **disponibles** en el state pero
StrategyAgent todavía no los usa. Próximo: extender StrategyAgent
para detectar señales con ADX (filtrar "sin tendencia"), Bollinger
(mean reversion a la banda), Estocástico (confirmación de RSI).

## Bug pequeño B016-bis

`fillna(method='bfill')` está deprecado en pandas 2.2+. Fix:
usar `.bfill()` directamente.

## Commit

`51a3db4` — feat(pdf2 indicators): DM/ADX + Estocástico + Bollinger + S/R

## Inspiración

Manual del Buen Trader Algorítmico (PDF2). El PDF recomienda
**NinjaTrader** como plataforma; nosotros ya teníamos nuestro bot
Python así que solo aplicamos los principios.

## Ver también

- [[Sprints_Index]]
- [[Inspiration2_Manual]]
