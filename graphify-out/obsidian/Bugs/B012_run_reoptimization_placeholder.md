# B012 — run_reoptimization placeholder

**Severidad**: 🟠 medio (funcionalidad crítica no implementada)

## Síntomas

Cada 7 días (epoch), el log mostraba:
```
[EpochScheduler] Re-optimization complete. New parameters injected.
```

Pero no había ninguna re-optimización, ni nuevos params. Era un
placeholder dejado por Gemini que **mentía**.

## Causa

```python
def run_reoptimization(self):
    logger.info("[EpochScheduler] Starting Hyperopt Re-optimization phase...")
    # Por ahora, simulamos una recalibración exitosa.
    logger.info("[EpochScheduler] Re-optimization complete. New parameters injected.")
```

## Fix (Sprint 5, commit `b2e8fb8`)

Reemplazo completo. Ahora corre de verdad:

```python
def run_reoptimization(self):
    asset = self.assets[0]
    df = self.market_analyst.fetch_one(asset, interval="1d", period="2y")
    # ... pre-popular con EMA/RSI ...
    param_space = {"rsi_oversold": [25, 30, 35], "rsi_overbought": [65, 70, 75]}
    best_params = self.hyperopt.optimize(
        f"epoch_{int(time.time())}",
        df, param_space, rsi_sig,
        metric="sharpe_ratio",
    )
    if best_params:
        old = dict(self.strategy_agent.params)
        new_params.update(best_params)
        self.strategy_agent.params = new_params
        self.audit.append("REOPT_NEW_PARAMS", {"old": old, "new": new_params})
```

## Test verificado

```
[HyperoptManager] Optimizando epoch_1783575000...
[HyperoptManager] -> Mejores parámetros (sharpe_ratio: 0.80):
                    {'rsi_oversold': 25, 'rsi_overbought': 75}
Params ANTES:  {'rsi_oversold': 30, 'rsi_overbought': 70}
Params DESPUÉS: {'rsi_oversold': 25, 'rsi_overbought': 75}
✅ Params cambiaron
```

## Limitación

Optimiza **solo RSI** sobre **un asset**. Una versión más ambiciosa
optimizaría cada estrategia sobre su asset. Sprint 8+.

## Ver también

- [[Sprints/Sprint_5_Real_Reoptimization]]
- [[Bugs_Index]]
