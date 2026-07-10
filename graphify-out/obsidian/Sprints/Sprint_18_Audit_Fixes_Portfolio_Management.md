# Sprint 18 — Audit Fixes + Portfolio Management

**Fecha**: 2026-07-09
**Status**: ✅ Cerrado (18/18 tests passing)
**Branch**: `main`
**Inspiración**: Carlos pidió "¿el bot reemplaza peor posición por mejor?" y "¿el bot cierra profit antes de reversión?". El audit team entregó 3 bugs adicionales.

## Resumen

Sprint 18 cierra dos frentes en paralelo:

1. **Audit fixes** (3 bugs matemáticos que rompían el bot en cuentas pequeñas o después de varios trades round-trip).
2. **Portfolio management inteligente** (2 features que llevan el bot de "ejecutor de señales" a "gestor de portafolio").

## Cambios

### Bug A — Micro-Account Death Loop (RiskAgent)

**Archivo**: `src/agents/risk_agent.py`

**Problema**:
- Cuenta de $20, `risk_per_trade_pct=1%`, stop ATR = 4% del precio.
- `quantity = risk_amount / stop_distance = $0.20 / (4% × $5000) = 0.001 BTC`.
- `notional = 0.001 × $5000 = $5` ❌ (binance.us min = $10).
- Auto-adjust anterior solo disparaba cuando `max_notional < min_order`. Pero aquí `max_notional = $10` (50% de $20), así que NO disparaba.
- Resultado: trade rechazado en cuentas pequeñas.

**Fix**: Auto-adjust ahora dispara cuando `notional < min_order_usd`, sin importar la causa (cap de config OR risk/distance demasiado pequeño). Se loguea la razón exacta (`max_cap_below_min_order` o `risk_below_min_order`) para que el usuario sepa qué tunable de config arreglar.

### Bug B — Phantom Exposure Lockup (MandateGate)

**Archivo**: `src/safety/mandate_gate.py`

**Problema**:
- `_open_exposure_usd()` iteraba sobre TODOS los eventos del audit ledger.
- Sumaba `qty × fill_price` cada vez que veía `TRADE_FILLED`.
- NUNCA restaba cuando veía `TRADE_CLOSED`.
- Después de 5 round-trip trades de $20, exposición calculada = $100 = `max_total_exposure_usd` → Mandate Gate bloquea TODOS los trades futuros permanentemente.

**Fix**: `_open_exposure_usd()` ahora consulta `PositionRepository.total_exposure_usd()` (suma de notional de posiciones realmente abiertas). El audit-ledger fallback fue corregido para que `TRADE_CLOSED` REMUEVA la posición del map.

### Bug C — Punished for Trying (MandateGate)

**Archivo**: `src/safety/mandate_gate.py`

**Problema**:
- `_daily_loss_usd()` sumaba `risk_usd` de eventos `TRADE_APPROVED` (teórico, NO realizado).
- 5 trades aprobados con $1 risk cada uno → `daily_loss = $5` → kill switch 24h, AUNQUE TODOS HUBIERAN SIDO WINNERS.

**Fix**: Suma `realized_pnl` real de posiciones cerradas (de `PositionRepository`) o de eventos `TRADE_CLOSED` (audit fallback). Solo cuenta pérdidas REALIZADAS. Si todas son ganadoras, `daily_loss = $0` y el bot sigue operando.

### Feature 1 — Position Replacement (RiskAgent)

**Archivo**: `src/agents/risk_agent.py`

**Comportamiento**: Cuando `len(open_positions) >= max_open_trades` y aparece una nueva hipótesis:

1. Calcular `score_new` de la hipótesis nueva.
2. Calcular `score_open` de cada posición abierta.
3. Ordenar por score ascendente → encontrar la peor.
4. Si `score_new > score_worst + replacement_score_threshold` (default 0.20):
   - Cerrar la peor posición a precio de mercado.
   - Loggear `POSITION_REPLACED` con score nuevo, score viejo, delta, threshold.
   - Aprobar el nuevo trade.
5. Si no, rechazar normalmente con `max_open_trades_reached`.

**Scoring**:

Posición abierta (más bajo = peor candidato a reemplazar):
- `unrealized_pnl_pct` (-1 a +1)
- `dist_to_sl` (más cerca del SL = peor)
- `age_h` (más viejo sin progreso = peor, penalización a partir de 24h, fuerte a las 72h)
- `remaining_r:r` (más bajo = peor)

Hipótesis nueva (más alto = mejor candidato a entrar):
- `expected_move_pct × 5` (cap +0.6)
- `R:R × 0.2` (cap +0.4)
- ATR quality: < 1% bonus +0.1, > 5% penalty -0.2

### Feature 2 — Smart Profit Take (PositionMonitor)

**Archivo**: `src/data_store/position_monitor.py`

**Comportamiento**: Nuevo método `check_with_signals(prices, signals, min_strength)`:

Para cada posición abierta:
1. Calcular `unrealized_pnl` al precio actual.
2. Si `unrealized_pnl >= min_profit_to_protect` (default 0, cualquier profit):
   - Buscar en `signals` una entrada con `asset == pos.asset`, `direction` OPUESTA, `strength >= min_strength` (default 0.6).
   - Si la encuentra → cerrar a precio de mercado con `reason="SMART_PROFIT_TAKE:{opposite}_signal_strength_X.XX"`.
3. Loggear `TRADE_CLOSED` con `realized_pnl_usd` (positivo).

**Wiring en main.py**: `job_with_monitor()` ahora:
- Después del check mecánico SL/TP, llama `check_with_signals()` con las hipótesis de la última hora (audit `read_since(time-3600)` filtrado por `event_type=HYPOTHESIS_GENERATED`).
- Refresca `risk_agent.current_prices` con precios en vivo para que el scoring de position replacement use datos reales.

## Tests (18/18 passing)

```
tests/test_risk_agent_sprint18.py
├── test_risk_below_min_order_triggers_auto_adjust        (Bug A)
├── test_max_cap_below_min_order_also_triggers            (regresión Sprint 12)
├── test_replace_worst_when_new_score_much_higher        (Feature 1: replace)
├── test_no_replacement_when_new_score_not_better_enough  (Feature 1: skip)
└── test_losing_position_scores_lower_than_winning       (scoring sanity)

tests/test_mandate_gate_sprint18.py
├── test_exposure_zero_after_round_trip_trades            (Bug B)
├── test_exposure_with_open_positions                     (Bug B)
├── test_legacy_audit_only_path_correctly_subtracts       (Bug B fallback)
├── test_daily_loss_zero_after_winning_trades            (Bug C)
├── test_daily_loss_sums_realized_losses                  (Bug C)
├── test_old_behavior_would_have_triggered_kill_switch    (regresión Bug C)
└── test_winning_trades_pass_daily_loss_check             (integración)

tests/test_position_monitor_sprint18.py
├── test_profitable_long_closed_on_strong_short_signal    (Feature 2)
├── test_profitable_long_NOT_closed_on_weak_signal        (Feature 2)
├── test_losing_position_NOT_closed_even_with_strong_signal (Feature 2 guard)
├── test_no_reversal_signal_keeps_profitable_long        (Feature 2 guard)
├── test_mechanical_sl_tp_still_works                     (regresión Sprint 2)
└── test_below_threshold_does_not_trigger                 (Feature 2 threshold)
```

## Archivos tocados

| Archivo | Líneas | Cambio |
|---|---|---|
| `src/agents/risk_agent.py` | +240/-65 | Bug A fix + position replacement + scoring |
| `src/safety/mandate_gate.py` | +85/-24 | Bug B + C fixes |
| `src/data_store/position_monitor.py` | +115/-16 | Smart profit-take |
| `main.py` | +40/-7 | Wire-up position_repo + current_prices + signals |
| `config.yaml` | +7 | 3 nuevos parámetros |
| `tests/test_risk_agent_sprint18.py` | +250 | New |
| `tests/test_mandate_gate_sprint18.py` | +180 | New |
| `tests/test_position_monitor_sprint18.py` | +165 | New |

Total: ~520 insertions, ~110 deletions en código de producción; +595 en tests.

## Configuración nueva (config.yaml)

```yaml
trading:
  enable_position_replacement: true        # default true; false = reject all al llegar a max_open
  replacement_score_threshold: 0.20        # new must score +20% over worst open
  min_profit_to_protect: 0.0               # USD; 0 = protect any unrealized profit on reversal signal
```

## Audit events nuevos

| Event | Cuándo | Payload |
|---|---|---|
| `POSITION_REPLACED` | Feature 1 dispara un reemplazo | closed_position_id, closed_asset, closed_pnl_usd, closed_score, new_asset, new_score, delta_score, threshold |
| `REPLACEMENT_SKIPPED` | Feature 1 evaluó pero no reemplazó | new_asset, new_score, worst_asset, worst_score, delta, threshold, reason |
| `SMART_PROFIT_TAKE` | Feature 2 cierra preventivamente | (via TRADE_CLOSED con reason prefix) |
| `CAP_AUTO_ADJUSTED` | Bug A dispara (con reason nuevo: `risk_below_min_order` además de `max_cap_below_min_order`) | reason, config_pct, config_notional, raw_risk_notional, min_order_usd, adjusted_to_pct, adjusted_to_notional |

## Próximos pasos

1. ✅ Push a GitHub.
2. Smoke test en paper mode: `python main.py --once`.
3. Monitorear primeros ciclos con `mandate.enabled=true` (no recomendado aún; paper primero).
4. Sprint 19 candidates (no comprometido):
   - Live trading en binance.us (requiere API keys).
   - LLM-powered debate (reemplazar debate determinístico con Claude/GPT).
   - Dashboard con display de replacement + smart profit-take events.