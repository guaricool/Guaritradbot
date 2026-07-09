# Sprint 2 — Position Tracking

## Objetivo

Rastrear posiciones abiertas **persistentemente** (sobreviven crashes)
y detectar stops/TPs cruzados antes de evaluar nuevas señales.

Inspirado en **NautilusTrader** "crash-only design":

> "Unified recovery path — Startup and crash recovery share the same code path,
> ensuring it is well-tested. Externalized state — Critical state is meant to
> be persisted externally when configured, reducing data-loss risk."

## Módulos nuevos

### [[Modules/Position_Repository]] (Sprint 2)
`Position` dataclass + `PositionRepository` persistido en
`data_store/positions.json` (atomic write via temp + replace).
- `position_id` único con uuid suffix
- Estado: `is_open` / `closed`
- `should_close_at(current_price)` → `(hit, reason)`

### [[Modules/Position_Monitor]] (Sprint 2)
Cada tick (antes del workflow engine):
1. Itera posiciones abiertas
2. Compara current_price con stop_loss y take_profit
3. Si hit → ejecuta close via broker
4. Marca posición como cerrada en repo
5. Registra `TRADE_CLOSED` en audit ledger con realized_pnl + duration

## Mejoras en módulos existentes

- `risk_agent.py`: Take Profit ATR-based (default 4x ATR para 1:2 R:R),
  respeta `max_open_trades`, persiste cada posición en el repo después
  de aprobar.
- `market_analyst.py`: helper público `fetch_one()` para que el
  monitor pueda obtener precios sin refactorizar.
- `main.py`: instancia PositionRepository + PositionMonitor, hookea
  el monitor antes de cada ciclo.

## Bug B016 (encontrado durante testing)

`position_id` con timestamp-only colisionaba si dos posiciones se
abrían en el mismo milisegundo. Fix: añadir uuid suffix.

## Commit

- `a2981bd` — feat(sprint 2): position tracking + TP ATR + PositionMonitor
- `49c36f4` — fix: track src/data_store/* (gitignore pattern was too greedy)

## Test

```
=== TEST PositionMonitor ===
Antes: 3 abiertas
Cerradas este ciclo: 2
  • GLD SHORT @ entry=$400 → close=$410.00 | PnL=$-0.5000 | reason=STOP_HIT
  • USO LONG @ entry=$110 → close=$135.00 | PnL=$+2.5000 | reason=TP_HIT
Después: 1 abiertas
✅ PositionMonitor funciona — riesgo duplicado bloqueado
```

BTC-USD LONG a $65000 (sin hit) queda abierto correctamente.

## Ver también

- [[Sprints_Index]]
- [[Architecture]] — el monitor aparece ANTES del workflow engine
