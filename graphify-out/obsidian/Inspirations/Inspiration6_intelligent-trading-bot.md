---
title: "Inspiration #6 — intelligent-trading-bot (event-driven + audit log)"
tags: [inspiration, inspiration-6, event-driven, audit-log, python]
source: external_repos/intelligent-trading-bot/
sprint_origin: Sprint_1
---

# Inspiration #6 — intelligent-trading-bot

**Origen**: `external_repos/intelligent-trading-bot/` (clonado el 2026-07-08).

## Qué es
Bot Python con arquitectura event-driven simple. Cada evento (señal, orden, fill, error) se loguea y se propaga por un bus. Diseñado para enseñar el patrón.

## Qué aportó a Guaritradbot

### Bus de eventos
- `intelligent-trading-bot` tiene un `EventBus` con `publish(topic, payload)` + suscriptores tipados.
- En Guaritradbot → `[[EventBus]]` en `src/core/event_bus.py`, pero con un solo método `publish(topic, payload)` (no `emit`).
- **Lección**: no mezclar nombres. Confundir `emit` con `publish` fue el [[B001_emit_vs_publish|bug B001]].

### Audit log append-only
- Cada trade va a un JSONL `audit_ledger.jsonl` con timestamp + decisión + razón.
- Inspiración directa para `[[AuditLedger]]` (Sprint 1).
- Trail completo para debugging post-mortem: si el bot falla a las 3 AM, abrir el audit y reconstruir.

### Separación Análisis / Risk / Execution
- 3 pasos discretos en el loop, no monolito.
- En Guaritradbot → `trading_loop.yaml` con 5 pasos: analyze → generate → debate → risk → execute.

### Confidence score
- Cada señal lleva score 0-1; el risk agent filtra < threshold.
- En Guaritradbot → `confidence` en hypothesis + `min_confidence = 0.6` en `config.yaml`.

## Lo que tomó Guaritradbot
- ✅ `[[EventBus]]` con `publish` único
- ✅ `[[AuditLedger]]` JSONL append-only
- ✅ 5 pasos discretos en YAML
- ✅ Confidence score por señal
- ❌ Múltiples subscriber types (Guaritradbot usa callable único)
- ❌ Replay de eventos (futuro Sprint)

## Patrones reusables (cross-project)
> **Audit log append-only en JSONL es el patrón universal de debugging post-mortem.** No importa el dominio: trading, e-commerce, IoT. Cada evento relevante a disco, una línea por evento, timestamp ISO 8601.

Ver: [[Sprint_1_Safety_Layer]], [[AuditLedger]], [[EventBus]], [[WorkflowEngine]], [[B001_emit_vs_publish]]
