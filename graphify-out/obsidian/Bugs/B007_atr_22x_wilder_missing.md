# B007 — ATR no se calculaba (era $5 fijo por B006)

**Severidad**: 🟠 medio (pariente de B006)

## Síntomas

No había forma de calcular stop loss "inteligente" porque no existía
ATR(14). El botón de fixing B006 requería ATR disponible.

## Fix (Sprint 0, commit `10d144c`)

Nueva función `_atr(df, period=14)` en `market_analyst.py`:

```python
def _atr(df, period=14):
    high, low, close = df['High'], df['Low'], df['Close']
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()
```

Wilder smoothing (EMA con `α=1/14`), igual que RSI.

Aplicada en:
- `fetch_and_analyze()` (workflow principal) → columna `ATR_14`
- `fetch_one()` (helper, usado por PositionMonitor)
- Consumida por `RiskManagerAgent` para sizing

## Usos downstream

- Sprint 0: stop loss (cierra B006)
- Sprint 2: take profit (default 4x ATR para R:R 1:2)
- Sprint 2: volatility gating en [[Modules/DebateAgent]]
- Sprint 5: hyperopt respeta ATR en backtest

## Ver también

- [[Bugs/B006_stop_loss_hardcoded_5]] — bug principal
- [[Modules/MarketAnalystAgent]]
- [[Bugs_Index]]
