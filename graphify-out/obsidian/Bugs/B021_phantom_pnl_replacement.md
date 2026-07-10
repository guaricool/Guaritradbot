# B021 — Phantom P&L on Replacement (Missing Price Fallback)

**Severidad**: 🔴 Crítica (corrupte audit + daily_loss)
**Sprint**: 18 (encontrado durante revisión post-sprint)
**Componente**: `RiskManagerAgent._try_replace_position()` en `src/agents/risk_agent.py`

## Síntomas

- PositionMonitor nunca refresca `current_prices` para un asset específico (e.g., falla el fetch de Yahoo Finance).
- Llega una nueva hipótesis al RiskAgent.
- `_try_replace_position` toma la peor posición abierta (e.g., LONG ETH @ $3000).
- La posición tiene unrealized_pnl = +$5 (precio actual $3500), pero el código no tiene ese precio.
- Se usa `worst_pos.entry_price` (=$3000) como fallback para `close_price`.
- La posición se cierra con `realized_pnl = 0`.
- El audit log registra una posición cerrada en breakeven — miente sobre el P&L real.
- `_daily_loss_usd()` calcula el daily_loss basándose en este P&L falso.

## Causa raíz

```python
close_price = self.current_prices.get(worst_pos.asset) or worst_pos.entry_price
```

El operador `or` es truthy: si el precio es None, usa entry_price. Pero `entry_price` no es un sustituto válido del precio actual — es el precio histórico al que se abrió.

Peor: si la posición realmente tiene +$5 profit y el código la cierra a entry_price (sin profit), el bot está DEJANDO DINERO SOBRE LA MESA. Y el audit log no muestra el error porque parece que se cerró a breakeven.

## Fix (Sprint 18 patch)

```python
close_price = self.current_prices.get(worst_pos.asset)
if close_price is None or close_price <= 0:
    if self.audit:
        self.audit.append("REPLACEMENT_SKIPPED", {
            ...
            "reason": "no_current_price",
        })
    print(
        f"  ⏸️  REPLACEMENT_ABORTED {worst_pos.asset:8} — "
        f"no current price available; will retry next cycle"
    )
    return False
```

**Mejor comportamiento**: si no tenemos precio fresco, NO reemplazar. El `PositionMonitor` cerrará la posición cuando se ejecute el siguiente ciclo con precios reales (ya sea por SL, TP, o signal reversal).

## Por qué esto es importante para auditoría forense

El audit ledger es append-only y se usa para:
1. Post-mortem analysis (¿por qué cerramos esta posición?)
2. Cálculo de daily_loss (¿cuánto perdimos realmente en 24h?)
3. Cálculo de realized_pnl histórico (métricas de performance)

Si los precios de cierre son falsos, TODO el downstream está corrupto. Es mejor dejar la posición abierta y perder una oportunidad de reemplazo que cerrar con datos mentirosos.

## Tests que cubren este bug

- `tests/test_risk_agent_sprint18.py::B021NoPriceAbortTest::test_no_current_price_aborts_replacement`

## Lección de diseño

NUNCA uses un fallback histórico cuando la operación es financieramente material. Si el dato fresco no está disponible:
- Opción A: Abortar la operación y reintentar (preferida para cierres).
- Opción B: Marcar la operación como "tentative" en el audit y reconciliar después.

`entry_price` como fallback era un "shotcut" que silenciaba el error pero introducía un bug más sutil y peor que el original.