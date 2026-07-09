# B006 — Stop loss hardcoded a $5

**Severidad**: 🔴 crítico (gestión de riesgo rota)

## Síntomas

Para cada trade, stop loss = `entry ± $5`. Asimétrico entre assets:

| Asset | Entry | Stop | Stop % |
|-------|-------|------|---------|
| BTC-USD | $61995 | $61990 | 0.008% |
| USO | $112 | $107 | 4.5% |

Riesgo ridículamente pequeño en BTC (efectivamente cero protección),
violento en USO.

## Causa

```python
stop_loss_distance = 5.0  # HARDCODED
```

## Fix (Sprint 0, commit `10d144c`)

ATR-based: `stop_distance = max(ATR × k_stop, price × 0.005)`.

- BTC ATR=380, k=2 → stop = $760 ≈ 1.2% del precio ✅
- USO ATR=2.7, k=2 → stop = $5.4 ≈ 4.8% del precio (consistente) ✅

Constante: ahora el stop es **proporcional a la volatilidad**, no al
precio del activo.

## Configurable en `config.yaml`

```yaml
trading:
  atr_stop_multiplier: 2.0  # default; 1.5 más tight, 3.0 más loose
```

## Take profit también (Sprint 2)

Mismo patrón. `take_profit_distance = ATR × atr_take_profit_multiplier`.
Default 4 → R:R = 1:2.

## Ver también

- [[Modules/RiskManagerAgent]]
- [[Bugs_Index]]
