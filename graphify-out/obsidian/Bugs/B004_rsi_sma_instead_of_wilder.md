# B004 — RSI con SMA en vez de Wilder

**Severidad**: 🔴 crítico (indicador técnicamente incorrecto)

## Síntomas

Las señales RSI eran **más lentas** que el estándar TradingView /
TA-Lib / cualquier backtester institucional. El bot generaba menos
señales y con delay.

## Causa

Versión vieja (Gemini):
```python
gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()  # SMA
loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()  # SMA
```

Wilder smoothing usa EMA con `α = 1/period`, NO rolling mean.

## Fórmula correcta

```python
delta = close.diff()
gain = delta.clip(lower=0.0)
loss = (-delta).clip(lower=0.0)

avg_gain = gain.ewm(alpha=1.0 / 14, adjust=False).mean()
avg_loss = loss.ewm(alpha=1.0 / 14, adjust=False).mean()

rs = avg_gain / avg_loss
rsi = 100 - (100 / (1 + rs))
```

## Fix (Sprint 0, commit `10d144c`)

Nueva función `_wilder_rsi(close, period=14)` en
`src/agents/market_analyst.py`. Aplicada en `fetch_and_analyze` Y en
`fetch_one()` (helper público para PositionMonitor, Sprint 2).

## Lección

Los indicadores técnicos tienen **convenciones estrictas**. RSI =
Wilder. MACD = EMA 12/26/9 (no SMA). Bollinger = SMA 20 ± 2σ.
El "rolling mean" en lugar de "Wilder smoothing" cambia las señales.

## Ver también

- [[Modules/MarketAnalystAgent]]
- [[Bugs_Index]]
