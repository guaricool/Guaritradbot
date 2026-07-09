# B001 — emit() vs publish()

**Severidad**: 🔴 crítico (rompía runtime en cada fill de trade)

## Síntomas

`python main.py --once` corría y llenaba logs con:

```
[ERROR] Error during workflow execution: 'EventBus' object has no attribute 'emit'
```

## Causa

`src/agents/execution_agent.py:19`:
```python
self.event_bus.emit("TRADES_EXECUTED", {"trades": executed})
```

Pero `src/core/event_bus.py` solo definía:
```python
def publish(self, event_type, data): ...
# (sin emit)
```

Python genera `AttributeError` al llamar `emit()`. En modo daemon
de Coolify, esto dejó el contenedor `exited:unhealthy`.

## Fix (Sprint 0, commit `10d144c`)

```python
# emit → publish
self.event_bus.publish("TRADES_EXECUTED", {"trades": executed})
```

## Lección

En sistemas pub/sub, **el publisher y el subscriber deben acordar el
mismo protocolo**. Si el subscriber solo tiene `publish()`, ningún
publisher debería llamar `emit`.

## Ver también

- [[Modules/EventBus]] — solo expone `publish`
- [[Modules/ExecutionAgent]] — publisher
- [[Bugs_Index]]
