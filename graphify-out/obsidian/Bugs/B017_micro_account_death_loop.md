# B017 — Micro-Account Death Loop

**Severidad**: 🔴 Crítico
**Sprint**: 18
**Reportado por**: Audit Team (Finance & Risk)
**Componente**: `RiskManagerAgent.validate_and_size()` en `src/agents/risk_agent.py`

## Síntomas

- Cuenta de $20 (o cualquier cuenta pequeña donde `risk_per_trade × stop_distance < min_order_usd`)
- Bot rechaza prácticamente todos los trades con `min_order_<X>`
- El bot nunca opera efectivamente

## Causa raíz

El bloque de auto-adjust original (Sprint 12) solo disparaba cuando:

```python
if max_notional < self.min_order_usd:
    # bump to min_order
```

Pero ese caso es RARO en cuentas pequeñas. El caso común es:

```
max_notional = $10  (50% de $20)  → ✅ OK, NO < $10
notional = $5       (risk $0.20 / stop distance 4% × price)
                                     → ❌ < $10, NO auto-adjusted
                                     → rejected by min_order check
```

Numéricamente:
- `risk_per_trade_pct = 1%` → `risk_amount = $0.20`
- `stop_distance = 4% × entry_price = $200` (BTC @ $5000)
- `quantity = $0.20 / $200 = 0.001 BTC`
- `notional = 0.001 × $5000 = $5` ❌ (< min_order $10)
- `max_notional = 50% × $20 = $10` (no dispara el if original)
- min_order check final: `$5 < $10` → `TRADE_REJECTED`

## Fix (Sprint 18)

Cambiar la condición para disparar auto-adjust cuando el `notional` realmente calculado está por debajo del mínimo, **independientemente** de la causa:

```python
# Step 1: Cap by max_capital_per_trade_pct (lo mismo de siempre)
if notional > max_notional:
    quantity = max_notional / entry_price
    notional = quantity * entry_price

# Step 2: Auto-adjust if notional < min_order_usd (NUEVO)
auto_adjust_reason = None
if notional < self.min_order_usd:
    if max_notional < self.min_order_usd:
        auto_adjust_reason = "max_cap_below_min_order"
    else:
        auto_adjust_reason = "risk_below_min_order"   # ← caso del bug
    # bump quantity to min_order, log reason
```

El log de audit `CAP_AUTO_ADJUSTED` ahora incluye `reason` (uno de los dos valores) para que el usuario sepa qué tunable de config arreglar:

- `max_cap_below_min_order` → subir `max_capital_per_trade_pct` o bajar `min_order_usd`
- `risk_below_min_order` → subir `risk_per_trade_pct` o usar stops más ajustados (ATR más bajo)

## Test que cubre este bug

- `tests/test_risk_agent_sprint18.py::RiskAgentBugAFixTest::test_risk_below_min_order_triggers_auto_adjust`
- `tests/test_risk_agent_sprint18.py::RiskAgentBugAFixTest::test_max_cap_below_min_order_also_triggers` (regresión Sprint 12)

## Nota adicional

Para cuenta de $20 con `risk_per_trade_pct=1%`, **la regla 1% del playbook NO es sostenible** con stops de 4% (que es típico en BTC/alta volatilidad). Carlos debería:

1. Subir `risk_per_trade_pct` a 2-3% en cuenta de $20, o
2. Operar solo activos menos volátiles (GLD, SPY) donde el stop distance típico es menor, o
3. Esperar a tener balance ≥ $50-100 donde 1% × stop distance típico > $10.