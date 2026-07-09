# ExecutionNode

`src/execution/execution_node.py`

## Responsabilidad

Nodo de ejecución **aislado**. Consume `ORDER_APPROVED` del EventBus,
ejecuta al broker real (o paper), publica `ORDER_EXECUTED`.

Inspirado en **NautilusTrader** ExecutionNode / ExecutionClient:

> "ExecutionEngine routes the command to the ExecutionClient for the target
> venue. ExecutionClient submits. The adapter sends the order to the venue
> over REST or WebSocket."

## Lifecycle

`__init__` se suscribe al EventBus:
```python
self.event_bus.subscribe("ORDER_APPROVED", self.on_order_approved)
```

## Modos (config.yaml `execution_mode`)

### `auto`
- Broker conectado → ordena al mercado via `BrokerClient.create_market_order`
- Broker NO conectado → "FILLED (SIMULATED)" status para paper-mode

### `human_in_the_loop`
- Publica `ORDER_PENDING_APPROVAL` para que [[Modules/NotificationAgent]] mande Telegram
- Si hay TTY (interactivo local): `input()` bloqueante (preguntar Y/N)
- **Si NO hay TTY (Docker daemon)**: SKIP seguro con `EOFError` caught
- Audit: `TRADE_REJECTED_HUMAN` o `TRADE_SKIPPED_NO_TTY`

## Bug B003 cerrado (Sprint 0)

Antes: `input()` sin try/except → colgaba el daemon en Docker.
Ahora: try/except `EOFError` → SKIP seguro si no hay stdin.

## Kill Switch integration (Sprint 1)

```python
def on_order_approved(self, data):
    if self.kill_switch and self.kill_switch.is_triggered():
        print("[ExecutionNode] ⛔ Kill switch ARMED — orden NO ejecutada")
        if self.audit:
            self.audit.append("TRADE_BLOCKED_KILLSWITCH", {"asset": data.get("asset")})
        return
```

Doble check del KillSwitch también dentro de `execute_order()` (defensa en profundidad).

## Audit logging

```python
self.audit.append(
    "TRADE_FILLED" if status.startswith("FILLED") else "TRADE_FAILED",
    {"asset": ..., "qty": ..., "entry_price": ..., "status": status}
)
```

## Símbolo mapping

```python
if "-" in symbol:
    symbol = symbol.replace("-", "/")      # BTC-USD → BTC/USD
elif "/" not in symbol:
    symbol = f"{symbol}/USDT"              # BTC → BTC/USDT
```

## Conecta con

- [[Modules/ExecutionAgent]] — source de ORDER_APPROVED (indirecto)
- [[Modules/BrokerClient]] — envío de órdenes reales
- [[Modules/KillSwitch]] — bloquea si armado
- [[Modules/AuditLedger]] — TRADE_FILLED/REJECTED/BLOCKED
- [[Modules/NotificationAgent]] — ORDER_PENDING_APPROVAL → Telegram
- [[Sprints/Sprint_0_Critical_Bug_Fixes]] — fix input() Docker
- [[Sprints/Sprint_1_Safety_Layer]] — Kill Switch
