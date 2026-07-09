# Inspiration 2 — Manual del Buen Trader Algorítmico (PDF2)

**Fuente**: PDF que Carlos convirtió a texto (`C:\Users\cpier\Downloads\01_LA GUIA DEL (BUEN) TRADER ALGORITMIC.pdf`)

**Usado en**: Sprint 7 (PDF Indicators)

## Lo que nos llevamos

### 7 indicadores técnicos recomendados

El PDF recomienda **estos 7** para un trader algorítmico:

| # | Indicador | Implementación en Guaritradbot |
|---|-----------|-------------------------------|
| 1 | **DM** (Directional Movement / ADX) | `_dm()` en [[Modules/MarketAnalystAgent]] |
| 2 | **SMA / EMA** | EMA_20, EMA_50 (ya estaban en Sprint 0) |
| 3 | **RSI** | Wilder smoothing (Sprint 0 fix B004) |
| 4 | **Estocástico** | `_stochastic()` (Sprint 7) |
| 5 | **MACD** | cruce (Sprint 0 fix B005) |
| 6 | **Máximos / Mínimos (S/R)** | `_support_resistance()` (Sprint 7) |
| 7 | **Bollinger Bands** | `_bollinger()` (Sprint 7) |

### Mentalidad ganadora

El PDF hace énfasis en:

> "El trading No es una máquina de generar dinero infinito.
> El trading es una herramienta MUY BUENA para sacar rentabilidad
> a tus ahorros."
>
> "TIENES QUE CREAR UN ROBOT QUE LO HAGA POR TÍ."

Eso es **exactamente** lo que Guaritradbot hace.

## Lo que NO tomamos

- **NinjaTrader como plataforma** — Carlos ya tenía su propio bot
  Python (Guaritradbot). NinjaTrader es para traders manuales que
  quieren robotizar sin programar; aquí ya éramos Python-first.
- **El robot regalado** del autor — el PDF promete "te regalo uno de
  los míos" pero no comparte el código. Además, ninguno de los
  nuestros robots compartidos es auditado, lo que sería peligroso
  meter en producción sin entenderlo.
- **10% per trade como regla fija** — el PDF sugiere 10% pero el de
  freqtrade sugiere 1-2%. Usamos 1% por trade (el "1% rule").

## Sprint 8+ ideas del PDF

- Activar StrategyAgent para usar los nuevos indicadores (ADX>25 = solo
  tendencia; BB reversión; Stoch confirmation de RSI)
- Trade journal persistente (el PDF habla mucho del journal)

## Ver también

- [[Sprints/Sprint_7_PDF_Indicators]]
- [[Inspirations]]
