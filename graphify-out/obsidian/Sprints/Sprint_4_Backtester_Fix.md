# Sprint 4 — Backtester Fix

## Objetivo

Que el backtester reporte **métricas reales** (no las fake de Gemini)
y valide las estrategias con **walk-forward** (anti-curve-fitting).

Inspirado en **freqtrade** (gold standard):

> "Backtesting engine (the reason it's on this list). Replays your strategy
> over past candles across 30+ exchanges. Performance metrics that matter:
> Win rate, Profit factor (gross profit / gross loss — above 1 = net positive),
> Max drawdown (worst peak-to-trough drop), Sharpe/Sortino, Expectancy
> (average $ you'd expect per trade). HOW TO READ RESULTS: a high win rate
> with a huge drawdown is a trap; favor steady equity curves."

## Bugs del backtester viejo

- `win_rate = bars_with_positive_return / total_bars_with_nonzero_return`
  — confundía "días alcistas" con "trades ganadores".
- `num_trades = total_bars_with_nonzero_return` — no era número de trades.
- Anualización hardcoded a 365 días — incorrecto para datos horarios.
- No calculaba **Profit Factor** ni **Expectancy** (las dos métricas
  que el PDF2 dice "separan traders serios de novatos").

## Métricas nuevas (todas vectorizadas con NumPy/Pandas)

- `total_return`: equity final / equity inicial - 1
- `annual_return`: capitalizado con periods_per_year
- `sharpe_ratio`: annual_return / annual_vol (risk-free=0)
- `sortino_ratio`: annual_return / downside_vol
- `calmar_ratio`: annual_return / |max_drawdown|
- `max_drawdown`: min de (equity - cummax) / cummax
- `win_rate`: winning_trades / total_trades (¡por trade, no por barra!)
- `profit_factor`: gross_profit / |gross_loss| (>1 rentable)
- `expectancy`: avg_win · win_rate - |avg_loss| · (1 - win_rate)
- `avg_win`, `avg_loss`, `num_trades` (true trade count)

## Walk-Forward Validation (`walk_forward_validate`)

Inspirado en **intelligent-trading-bot** `predict_rolling` script:

1. Genera splits train/test (expanding o rolling window)
2. Optimiza hiperparámetros en train vía HyperoptManager
3. Evalúa en test (out-of-sample) con los best_params
4. Reporta IS/OOS ratio de la métrica optimizada
5. **Si OOS/IS < 0.5 → marca ⚠️ OVERFIT**

## Trade detection (nuevo)

```python
def _detect_trades(self, signals, prices):
    # Cuando signal != 0 → entry
    # Cuando signal == 0 o fin de serie → exit
    # Cada par entry/exit = 1 trade
```

Ahora `num_trades = 1` cuando hay 1 crucé RSI en 2 años, no 730
(uno por barra como antes).

## Test verificado

```
BTC-USD 2 años con RSI Mean Reversion (25/75):
  In-sample:  Sharpe 2.30, Total +173% (parece excelente)
  Out-of-sample: Sharpe -0.71, Total -19%
  IS/OOS ratio = -0.31 → ⚠️ OVERFIT claro
```

**Conclusión importante**: la estrategia RSI(25/75) NO funciona en
condiciones reales aunque el backtest básico diga que sí. Esto
valida la utilidad OBLIGATORIA del walk-forward antes de ir a producción.

## Commit

`7a0cd26` — feat(sprint 4): métricas correctas + Walk-Forward anti-curve-fit

## Ver también

- [[Sprints_Index]]
- [[Bugs_Index]] — B010 y B011 explicados
