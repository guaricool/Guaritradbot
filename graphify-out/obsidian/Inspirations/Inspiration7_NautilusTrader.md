---
title: "Inspiration #7 — NautilusTrader (institutional-grade architecture)"
tags: [inspiration, inspiration-7, nautilus, institutional, rust, performance]
source: web_ref + memory
sprint_origin: Project_History
---

# Inspiration #7 — NautilusTrader

**Origen**: Web research + documents PDF. NO clonado (proyecto Rust + Python bindings, demasiado pesado para MVP).

## Qué es
Framework trading de grado institucional. El núcleo está escrito en **Rust** (zero-cost abstractions, type system fuerte), con bindings Python. Diseñado para prop firms y desks quant. Documentación extensiva en `nautilustrader.io`.

## Qué aportó a Guaritradbot (mental model)

### Tipos de mensaje con dominio
- `Order`, `Trade`, `Position`, `Instrument`, `Venue` — todos son tipos estrictos con validaciones en compile-time.
- En Guaritradbot → Dict[str, Any] por ahora. **Futuro**: Pydantic models en `src/core/models.py` (Sprint 8 propuesto).

### Máquina de estados explícita por entidad
- `Position` tiene estados: `OPENING`, `OPEN`, `CLOSING`, `CLOSED`.
- `Order` tiene: `INITIALIZED`, `SUBMITTED`, `ACCEPTED`, `WORKING`, `FILLED`, `CANCELED`, `EXPIRED`.
- En Guaritradbot → `[[Component_State_Machine]]` (Sprint 6) para agentes. **PENDIENTE**: FSM por entidad (Position, Order) en Sprint 8.

### Backtester = Adapter de datos
- Nautilus separa **datos** de **lógica**. Cualquier `DataClient` (live, CSV, Parquet, Binance) alimenta el mismo motor.
- En Guaritradbot → `DataClient` interface en `src/data/` (parcial). Sprint 8 puede formalizarlo.

### Portfolio construction
- El "portfolio" de Nautilus recalcula exposure por símbolo, sector, currency en cada fill. NO solo saldo.
- En Guaritradbot → `[[MandateGate]]` tiene `max_total_exposure_usd` por símbolo. **Limitado**: no hay sector/currency exposure tracking aún.

### Strategies como clases con handlers
- `on_bar(ctx, bar)`, `on_event(ctx, event)`. Repr descontínuo, eventos asíncronos.
- En Guaritradbot → [[StrategyAgent]] con método `decide()` síncrono. **Diferencia**: Guaritradbot ejecuta 1 ciclo, Nautilus espera eventos.

### Riesgo con pre-trade checks
- Cada `Order` pasa por `RiskEngine` antes de enviar al venue. Rechaza si excede limits.
- En Guaritradbot → [[RiskManagerAgent]] hace esto. **Diferencia**: Nautilus corre checks en paralelo, Guaritradbot secuencial.

## Por qué NO se migró
- El **core Rust** requiere compilación cruzada. Carlos ya tiene Python-first stack en VPS.
- Tiempo de onboarding para Nautilus: 2-4 semanas. Para $100 capital, ROI no cierra.
- **PERO** los patrones (FSM por entidad, data adapter, RiskEngine como filtro) se migraron **uno a uno** a Guaritradbot en sprints futuros.

## Lo que tomó Guaritradbot
- ✅ Concepto de FSM para componentes (Sprint 6)
- ✅ Separación data/lógica (parcial)
- ✅ Pre-trade risk checks
- ❌ Rust core
- ❌ Portfolio construction institucional
- ❌ Tipos estrictos end-to-end

## Roadmap (futuras inspirations)
Ver [[Research_Evolution_Roadmap]] — Sprint 8+ debería apuntar a:
- FSM por Position/Order (Nautilus-style)
- DataClient interface formal
- Portfolio exposure multi-nivel (symbol, sector, currency)
- Pydantic models para todos los mensajes

## Patrones reusables (cross-project)
> **Las máquinas de estado explícitas por entidad previenen el 80% de bugs de trading.** Sin FSM, un PATCH sobre un PARTIAL_FILL genera estados imposibles (closed+open, filled+pending). El costo de dibujar la FSM upfront es trivial comparado al costo de un incidente.

Ver: [[Component_State_Machine]], [[RiskManagerAgent]], [[Research_Evolution_Roadmap]]
