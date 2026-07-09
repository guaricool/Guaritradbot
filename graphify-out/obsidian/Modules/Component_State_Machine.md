# Component State Machine

`src/core/component.py`

## Responsabilidad

Lifecycle explícito para todos los componentes. Inspirado en
**NautilusTrader**:

> "All components follow a finite state machine pattern. The `ComponentState`
> enum defines both stable states and transitional states."
> Stable: PRE_INITIALIZED, READY, RUNNING, DEGRADED, FAULTED, DISPOSED, STOPPED.
> Transitional: STARTING, STOPPING, RESUMING, RESETTING, DISPOSING,
> DEGRADING, FAULTING.

## State enum

```python
class ComponentState(str, Enum):
    PRE_INITIALIZED = "PRE_INITIALIZED"
    READY = "READY"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAULTED = "FAULTED"
    DISPOSED = "DISPOSED"
```

## Component class

```python
class Component:
    def __init__(self, name: str, audit=None):
        self.name = name
        self.state: ComponentState = ComponentState.PRE_INITIALIZED
        self.audit = audit

    def ready(self):     # PRE_INIT → READY
    def start(self):      # READY → STARTING → RUNNING
    def stop(self):       # RUNNING → STOPPING → STOPPED
    def fault(reason):    # * → FAULTED (terminal)
    def degrade(reason):  # RUNNING → DEGRADED (recoverable)
    def recover(self):    # DEGRADED → RUNNING
```

Cada transición se **loguea** y (si hay audit) se **persiste**:
```json
{"event_type": "COMPONENT_STATE_FAULTED",
 "component": "MarketAnalystAgent",
 "from": "RUNNING",
 "reason": "all 15 feeds failed"}
```

## Aplicado en

- **MarketAnalystAgent** (único actualmente que hereda de Component)
- **Futuro Sprint 8+**: ExecutionNode, PositionRepository, StrategyAgent
  podrían también ser Components

## Output run del bot

```
[MarketAnalystAgent] PRE_INITIALIZED → READY (configure())
[MarketAnalystAgent] READY → STARTING ()
[MarketAnalystAgent] STARTING → RUNNING (start())
```

Si todos los feeds fallan:
```
[MarketAnalystAgent] RUNNING → FAULTED (all 15 feeds failed)
```

## Conecta con

- [[Modules/MarketAnalystAgent]] — primer Component del sistema
- [[Modules/AuditLedger]] — persiste cada transición
- [[Modules/Data_Validator]] — fail que dispara DEGRADED
- [[Sprints/Sprint_6_State_Machine_Data_Integrity]]
