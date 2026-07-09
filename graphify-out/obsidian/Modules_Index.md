# Modules Index

Módulos del sistema, ordenados por capa.

## Agentes (entrypoints del workflow)

- [[Modules/MarketAnalystAgent]] — descarga datos + indicadores
- [[Modules/StrategyAgent]] — detecta hipótesis (cruces)
- [[Modules/DebateAgent]] — debate Bull/Bear/Risk/PM (Sprint 3)
- [[Modules/RiskManagerAgent]] — sizing + Mandate Gate
- [[Modules/ExecutionAgent]] — publica ORDER_APPROVED
- [[Modules/ExecutionNode]] — ejecuta real o paper
- [[Modules/NotificationAgent]] — Telegram (existente, no tocado)

## Core (infraestructura)

- [[Modules/EventBus]] — pub/sub message bus
- [[Modules/WorkflowEngine]] — ejecuta steps del YAML
- [[Modules/Component_State_Machine]] — FSM (Sprint 6)
- [[Modules/Data_Validator]] — NaN/Inf fail-fast (Sprint 6)

## Safety (Sprint 1)

- [[Modules/AuditLedger]] — JSONL append-only
- [[Modules/KillSwitch]] — filesystem kill
- [[Modules/MandateGate]] — validaciones pre-trade

## Data store (Sprint 2)

- [[Modules/Position_Repository]] — posiciones persistidas en disco
- [[Modules/Position_Monitor]] — cierra stops/TPs cada tick

## Optimización (Sprint 4-5)

- `src/optimization/backtester.py` — métricas gold-standard + walk-forward
- `src/optimization/hyperopt.py` — grid search

## Diagrama de imports

```
main.py
├── src.workflows.engine.WorkflowEngine
│   └── (usa los agents via registry)
├── src.execution.scheduler.EpochScheduler
│   ├── src.execution.broker.BrokerClient
│   ├── src.optimization.hyperopt.HyperoptManager (Sprint 5)
│   └── agents.market_analyst.MarketAnalystAgent
├── src.safety.audit_ledger.AuditLedger
├── src.safety.kill_switch.KillSwitch
├── src.safety.mandate_gate.MandateGate
└── src.data_store.positions.PositionRepository

agents/
├── market_analyst.py    → inherits Component (Sprint 6)
├── strategy_agent.py
├── risk_agent.py         → uses PositionRepository, MandateGate, Audit
├── execution_agent.py    → publishes ORDER_APPROVED to bus
├── execution_node.py (execution/)  → consumes ORDER_APPROVED, uses KillSwitch
├── researchers.py        → DebateAgent (Sprint 3)
└── notification_agent.py (existing)

execution/
├── broker.py             → ccxt client
├── scheduler.py          → EpochScheduler (cadence)
└── execution_node.py     → isolated execution layer

data_store/
├── positions.py          → PositionRepository
└── position_monitor.py   → stops/TPs checker

safety/
├── audit_ledger.py       → JSONL append-only with fsync
├── kill_switch.py        → file-based
└── mandate_gate.py       → universe/exposure/daily cap

core/
├── component.py          → Component base + State Machine
├── data_validator.py     → NaN/Inf fail-fast
└── event_bus.py          → pub/sub

optimization/
├── backtester.py         → VectorizedBacktester (Sprint 4: real metrics)
└── hyperopt.py           → HyperoptManager (grid search)

workflows/
└── trading_loop.yaml     → 5 steps (analysis → strategy → debate → risk → exec)
```
