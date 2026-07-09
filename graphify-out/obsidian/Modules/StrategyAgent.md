# StrategyAgent

`src/agents/strategy_agent.py`

## Responsabilidad

Generar **hipótesis de trade** basadas en cruces de indicadores.

## Antes (Sprint 0) — qué cambió

| Antes | Ahora |
|-------|-------|
| `last_macd > last_signal` (estado) | `macd_prev ≤ sig_prev AND macd_now > sig_now` (cruce) |
| RSI comparaba valor actual | Detecta `rsi_prev ≥ oversold AND rsi_now < oversold` |
| EMA comparaba valor actual | Detecta golden / death cross |
| `generate_vectorized_signals` siempre 1/-1 | Mantiene FLAT (0) por default, posición forward-filled |

Cerrado **B005** (cruce vs estado), **B009** (FLAT por default).

## Reglas de cruce

Para cada asset:
- **SPY/QQQ**: Mean reversion RSI (oversold/overbought)
- **BTC**: MACD bullish/bearish cross (1h)
- **GLD/USO**: EMA golden/death cross (4h preferentemente, 1h fallback)

## generate_vectorized_signals (Sprint 0 fix)

```python
def generate_vectorized_signals(df, strategy_type="RSI", **params):
    signals = pd.Series(0.0, index=df.index)  # FLAT por default
    if strategy_type == "RSI":
        cross_below = (rsi.shift(1) >= oversold) & (rsi < oversold)
        cross_above = (rsi.shift(1) <= overbought) & (rsi > overbought)
        signals[cross_below] = 1.0  # entry long
        signals[cross_above] = -1.0  # exit long / flat
    elif strategy_type == "MACD":
        ...
    elif strategy_type == "EMA_CROSS":
        ...
    # forward-fill para mantener posición hasta el próximo cruce
    return signals.replace(0, np.nan).ffill().fillna(0)
```

## Bug B014 cerrado

`df = df_4h or df_1h` cuando `df_4h` es DataFrame → Python evalúa
`bool(df)` que es ambiguo para DataFrames en pandas → crash.
Fix: reemplazar con `if df is None or len(df) == 0: df = df_1h`.

## Estrategia usada actualmente

- RSI(30/70) para SPY/QQQ
- MACD para BTC-USD
- EMA-cross para GLD/USO

Los nuevos indicadores DM/ADX/Stoch/BB/S/R (Sprint 7) están
disponibles en el state pero StrategyAgent todavía no los usa. Esto
sería un sprint futuro: lógica de "ADX<20 = no operar" o
"Bollinger reversion".

## Conecta con

- [[Modules/MarketAnalystAgent]] — consume market_data
- [[Modules/DebateAgent]] — entrega hipótesis para debate
- [[Sprints/Sprint_0_Critical_Bug_Fixes]] — fix RSI/Stoch/EMA cruces
- [[Sprints/Sprint_5_Real_Reoptimization]] — recibe nuevos params
