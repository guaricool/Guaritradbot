# WorkflowEngine

`src/workflows/engine.py`

## Responsabilidad

Ejecuta los steps del `trading_loop.yaml` **secuencialmente**,
manteniendo un `state` compartido.

## YAML driver

`src/workflows/trading_loop.yaml`:
```yaml
name: Daily Multi-Asset Trading Loop
description: ...
steps:
  - id: analyze_market
    agent: MarketAnalystAgent
    action: fetch_and_analyze
    inputs: ...
  - id: generate_hypotheses
    agent: StrategyAgent
    action: evaluate_strategies
    depends_on: ["analyze_market"]
  ...
```

## Ejecución

```python
def run(self, workflow_data):
    state = {}
    for step in workflow_data["steps"]:
        agent_name = step["agent"]
        action_name = step["action"]
        agent = self.agents[agent_name]
        result = action_method(inputs=step["inputs"], state=state)
        state[step["id"]] = result
    return state
```

Cada step recibe `state` (acumulado) y devuelve `result` que se
guarda en `state[step_id]`.

## Steps actuales (después de Sprint 3)

1. `analyze_market` → `MarketAnalystAgent.fetch_and_analyze`
   → `state["analyze_market"]["market_data"]`
2. `generate_hypotheses` → `StrategyAgent.evaluate_strategies`
   → `state["generate_hypotheses"]["hypotheses"]`
3. `debate_hypotheses` → `DebateAgent.run_debate` (Sprint 3)
   → `state["debate_hypotheses"]["approved_hypotheses"]`
4. `risk_evaluation` → `RiskManagerAgent.validate_and_size`
   → `state["risk_evaluation"]["approved_trades"]`
5. `execute_trades` → `ExecutionAgent.simulate_execution`
   → `state["execute_trades"]["executed_trades"]`

## Customización (Sprint 2)

En `main.py`, el `scheduler.job()` está monkey-patched para correr
`PositionMonitor.check()` ANTES del workflow normal (cierra stops/TPs
antes de evaluar nuevas señales):

```python
def job_with_monitor():
    # 1. PositionMonitor
    ...
    # 2. Workflow normal
    original_job()

scheduler.job = job_with_monitor
```

## Bug "depend_on" actualmente ignorado

YAML define `depends_on` pero el engine **no valida** el orden.
Siempre ejecuta en el orden del YAML. Para Sprint 8+ se podría
añadir validación.

## Conecta con

- [[Architecture]] — cómo encaja
- [[Modules/DebateAgent]] — paso añadido en Sprint 3
- [[Modules/Position_Monitor]] — hookeado antes del engine
- [[Project_History]] — pre-existente al refactor Mavis
