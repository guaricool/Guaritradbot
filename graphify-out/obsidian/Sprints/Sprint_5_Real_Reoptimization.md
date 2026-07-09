# Sprint 5 — Real Re-Optimization

## Objetivo

Cerrar el **placeholder** `run_reoptimization()` que solo logueaba
"Re-optimization complete". Ahora corre de verdad al cierre de cada
**epoch** (default: 7 días).

Inspirado en **intelligent-trading-bot** (asavinov):

> "Functions for backtesting and measuring trade performance on historic
> data which is more difficult because requires periodic re-train of the
> used ML models."

## B012 cerrado

El placeholder (`log.info("complete")`) se reemplazó con la lógica real.

## Lógica nueva

`EpochScheduler.run_reoptimization()` (corre cuando han pasado N días):

1. Descarga 2 años de datos del primer asset (BTC-USD por default)
2. Calcula indicadores (RSI Wilder, EMA)
3. `HyperoptManager.optimize()` prueba 9 combinaciones de RSI
4. Si los best_params mejoran, los inyecta al `StrategyAgent.params`
5. Emite `REOPT_START` y `REOPT_NEW_PARAMS` al audit ledger con old/new

## Cambios en `EpochScheduler`

```python
def __init__(self, engine, workflow_data, config_path,
             market_analyst=None,   # NUEVO Sprint 5
             strategy_agent=None,   # NUEVO Sprint 5
             hyperopt=None,         # NUEVO Sprint 5
             audit=None,            # NUEVO Sprint 5
             assets=("BTC-USD", "SPY")):
    # Default: si no se inyectan, hace skip silencioso.
```

`main.py` los inyecta explícitamente:

```python
hyperopt = HyperoptManager()
scheduler = EpochScheduler(
    engine, workflow_data, config_path,
    market_analyst=registry["MarketAnalystAgent"],
    strategy_agent=registry["StrategyAgent"],
    hyperopt=hyperopt, audit=audit,
    assets=("BTC-USD", "SPY", "GLD", "QQQ", "USO"),
)
```

## Cadencia

- **Una vez por epoch** (cada 7 días). NO cada tick.
- Mantiene la estabilidad — los params no bailan entre ciclos.
- Si hyperopt falla, mantiene los params actuales (no degrada).

## Test verificado

```
[HyperoptManager] Optimizando epoch_1783575000...
[HyperoptManager] Probando 9 combinaciones...
[HyperoptManager] -> Mejores parámetros (sharpe_ratio: 0.80):
                    {'rsi_oversold': 25, 'rsi_overbought': 75}
Params ANTES:  {'rsi_oversold': 30, 'rsi_overbought': 70}
Params DESPUÉS: {'rsi_oversold': 25, 'rsi_overbought': 75}
✅ Params cambiaron — re-opt funciona
```

## Limitación

Optimiza **solo RSI** sobre **un asset**. Una versión más ambiciosa
optimizaría cada estrategia sobre su asset:
- RSI para SPY/QQQ (mean reversion)
- MACD-cross para BTC
- EMA-cross para GLD/USO

Eso sería Sprint 8+ con `multi-strategy hyperopt`.

## Commit

`b2e8fb8` — feat(sprint 5): epoch re-optimization real

## Ver también

- [[Sprints_Index]]
- [[Bugs_Index]] — B012 cerrado
