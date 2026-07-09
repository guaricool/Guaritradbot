# B005 — MACD comparaba estado vs cruce

**Severidad**: 🟠 medio (estrategia ineffectiva)

## Síntomas

BTC-USD estaba **siempre long** durante una tendencia alcista. El
bot generaba la misma hipótesis (long con MACD>0) durante 3+ meses.

## Causa

Versión vieja:
```python
if last_macd > last_signal:
    hypotheses.append({direction: long, ...})
```

Eso es "el momentum está alcista AHORA" — siempre true durante
tendencias alcistas. No es una **señal de entrada**, es un estado.

## Fix (Sprint 0, commit `10d144c`)

Detectar **cruce explícito**:
```python
macd_bull_cross = (
    df['MACD'].iloc[-1] > df['MACD_Signal'].iloc[-1]
    and df['MACD'].iloc[-2] <= df['MACD_Signal'].iloc[-2]
)
if macd_bull_cross:
    hypotheses.append({direction: long, strategy: "MACD_BullCross", ...})
```

Mismo fix para RSI (oversold/overbought **cross**) y EMA (golden/
death cross).

## Resultado

- Antes: 1 trade / año (rotación mala)
- Ahora: trades solo cuando hay cruce genuino

## Ver también

- [[Modules/StrategyAgent]]
- [[Bugs_Index]]
