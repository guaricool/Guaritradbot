# EventBus

`src/core/event_bus.py`

## Responsabilidad

**Pub/sub message bus** entre los componentes. Inspirado en
**NautilusTrader** MessageBus:

> "The backbone of inter-component communication, implementing:
> - Publish/Subscribe patterns: For broadcasting events and data to
>   multiple consumers.
> - Request/Response communication: For operations requiring acknowledgment.
> - Command/Event messaging: For triggering actions and notifying state changes.
> - Optional state persistence: Using Redis for durability and restart capabilities."

## API

```python
bus = EventBus()

# Suscribirse
def handler(event_data):
    print(f"Recibí: {event_data}")
bus.subscribe("TRADE_APPROVED", handler)

# Publicar
bus.publish("TRADE_APPROVED", {"asset": "BTC-USD", "qty": 0.001})
```

## Events emitidos en Guaritradbot

| Evento | Publisher | Subscribers |
|--------|-----------|-------------|
| `MARKET_DATA_READY` | MarketAnalyst | (debug/log) |
| `ORDER_APPROVED` | ExecutionAgent ([[Modules/ExecutionAgent]]) | ExecutionNode |
| `ORDER_PENDING_APPROVAL` | ExecutionNode | NotificationAgent → Telegram |
| `ORDER_EXECUTED` | ExecutionNode | (audit log) |
| `TRADE_CLOSED` | PositionMonitor | NotificationAgent |
| `SYSTEM_ERROR` | (cualquiera) | NotificationAgent |

## Limitación actual

Es un bus **in-memory** (no persistente). Si el proceso muere,
eventos en vuelo se pierden. Para crash-only real, debería ser
persistido en disco (Nautilus lo hace con Redis/Parquet).

## Bug B001 cerrado (Sprint 0)

Antes el código llamaba `self.event_bus.emit(...)` en
`execution_agent.py`, pero `EventBus` solo tenía `publish()`.
Cada trade llenaba logs con `AttributeError`. Fix: cambiar `emit` → `publish`.

## Conecta con

- [[Architecture]] — backbone del flujo de eventos
- [[Modules/ExecutionAgent]], [[Modules/ExecutionNode]] — flujo ORDER_APPROVED
- [[Modules/NotificationAgent]] — consume y manda Telegram
- [[Sprints/Sprint_0_Critical_Bug_Fixes]] — fix emit/publish
