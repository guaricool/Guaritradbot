# ExecutionAgent

`src/agents/execution_agent.py`

## Responsabilidad

Recibir trades aprobadas del RiskManager, **publicar ORDER_APPROVED**
al EventBus. La ejecución real la hace [[Modules/ExecutionNode]].

## Flujo

```
RiskManagerAgent (publica approved_trades via state)
         │
         ▼
ExecutionAgent.simulate_execution(inputs, state)
         │
         │   1. Logea "route→"
         │   2. EventBus.publish("ORDER_APPROVED", trade)
         │
         ▼
ExecutionNode.on_order_approved(trade)  ← consume
         │
         ▼
broker real o paper
```

## Bug B013 cerrado (Sprint 0 + 1)

Antes, el `trading_loop.yaml` finalizaba con `execute_trades →
ExecutionAgent.simulate_execution`, que solo **imprimía** las trades.
El `[[Modules/ExecutionNode]]` (que se suscribe a `ORDER_APPROVED`)
quedaba **desconectado** del workflow.

Fix: `ExecutionAgent.simulate_execution` ahora publica
`ORDER_APPROVED` por cada trade aprobada. ExecutionNode las consume.

## Modos

| Modo | Comportamiento |
|------|---------------|
| `auto` | Ejecuta directamente, publica `ORDER_EXECUTED` |
| `human_in_the_loop` | Publica `ORDER_PENDING_APPROVAL`, pide confirmación, ejecuta si OK |

## Output del step

```python
{
    "executed_trades": [...],  # lista de dicts de trades
    "rejected_trades": [...],  # trades bloqueadas por mandate
}
```

Esto se persiste en `latest_state.json` para que el [[Project_Architecture|dashboard]]
lo muestre.

## Conecta con

- [[Modules/RiskManagerAgent]] — recibe trades aprobadas
- [[Modules/ExecutionNode]] — consume ORDER_APPROVED via EventBus
- [[Modules/AuditLedger]] — TRADE_FILLED / TRADE_FAILED
- [[Sprints/Sprint_0_Critical_Bug_Fixes]] — fix emit/publish
- [[Sprints/Sprint_1_Safety_Layer]] — fix wiring
