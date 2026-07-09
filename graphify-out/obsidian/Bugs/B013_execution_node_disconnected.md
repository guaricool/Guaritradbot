# B013 — ExecutionNode desconectado del workflow

**Severidad**: 🔴 crítico (capa de ejecución no se ejecutaba)

## Síntomas

`ExecutionNode` estaba implementado con buena lógica (kill switch,
modo human-in-the-loop, etc.) pero **nunca era invocado**. El
`EventBus.subscribe("ORDER_APPROVED", ...)` registrado en
`__init__` quedaba sin publisher.

## Causa

El workflow YAML ejecutaba `ExecutionAgent.simulate_execution` que
solo imprimía las trades — no emitía `ORDER_APPROVED`.

El `ExecutionNode` se construía en `main.py` pero su `on_order_approved`
nunca era llamado.

## Fix (Sprint 0 + 1, commit `10d144c`)

`ExecutionAgent.simulate_execution` ahora publica:

```python
for trade in approved:
    self.event_bus.publish("ORDER_APPROVED", trade)
```

Y `ExecutionNode` consume correctamente.

## Detección

Lo descubrimos cuando audité el repo: `ExecutionNode` tenía
subscripción pero nadie era publisher. Era una capa muerta.

## Ver también

- [[Modules/ExecutionNode]]
- [[Modules/ExecutionAgent]]
- [[Architecture]] — cómo encajan
- [[Bugs_Index]]
