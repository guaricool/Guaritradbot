# B009 — generate_vectorized_signals nunca flat

**Severidad**: 🟠 medio (backtest siempre invertido)

## Síntomas

El backtester mostraba win_rate ~50% pero el bot siempre estaba
comprado O vendido. Sin estado "cash". Eso significa que el
backtest no era realista.

## Causa

```python
def generate_vectorized_signals(df, strategy_type="RSI", **params):
    signals = pd.Series(0.0, index=df.index)
    if strategy_type == "RSI":
        # ...
        signals = np.where(df['RSI'] < oversold, 1, np.where(df['RSI'] > overbought, -1, 0))
    return pd.Series(signals, index=df.index)
```

El default del np.where era `-1`, no 0. Cada barra estaba o long o short.

## Fix (Sprint 0, commit `10d144c`)

```python
signals = pd.Series(0.0, index=df.index)  # FLAT por default
cross_below = (rsi.shift(1) >= oversold) & (rsi < oversold)
cross_above = (rsi.shift(1) <= overbought) & (rsi > overbought)
signals[cross_below] = 1.0   # entry long
signals[cross_above] = -1.0  # exit long / entry short

# Forward-fill para mantener posición hasta el próximo cruce
return signals.replace(0, np.nan).ffill().fillna(0)
```

Ahora el backtest tiene cash entre trades, igual que el live.

## Conexión

Este bug enmascaraba B010 y B011 (las métricas mal calculadas). Una
vez fijo el FLAT, las métricas reales emergieron y motivaron el
Sprint 4 completo.

## Ver también

- [[Sprints/Sprint_4_Backtester_Fix]]
- [[Sprints/Sprint_0_Critical_Bug_Fixes]]
- [[Bugs_Index]]
