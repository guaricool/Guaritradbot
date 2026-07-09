# Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                      COOLIFY (VPS Coolify 13.140.181.29)          │
│  ┌────────────────────────┐  ┌──────────────────────────────┐   │
│  │ guaritradbot engine    │  │ guaritradbot dashboard        │   │
│  │ (FROM python:3.11-slim│  │ (FROM python:3.11-slim        │   │
│  │  CMD python main.py)   │  │  CMD streamlit dashboard.py)  │   │
│  │ Port: ninguno          │  │ Port: 8501                   │   │
│  └────────────────────────┘  └──────────────────────────────┘   │
│            ↓ events                                ↑ HTTP        │
│  ┌────────────────────────────────────────────────────────┐      │
│  │ data_store/positions.json (persistente)                  │      │
│  │ audit/audit.jsonl (append-only forense)                  │      │
│  │ config.yaml (todas las configs + mandate toggle)          │      │
│  └────────────────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│              BOT CORE (mismo en VPS y local)                       │
└──────────────────────────────────────────────────────────────────┘

  ┌─── entrypoint ───────────────────────────┐
  │  main.py                                  │
  │  • Carga config.yaml                       │
  │  • Inicializa Audit, Mandate, KillSwitch  │
  │  • Carga PositionRepository                │
  │  • Construye agents registry               │
  │  • Hookea PositionMonitor antes de cada job │
  │  • Lanza EpochScheduler                     │
  └──────────────────────────────────────────┘

  ┌─── epoch scheduler ──────────────────────┐
  │  src/execution/scheduler.py               │
  │  scheduler.job()                           │
  │  1. check_epoch → si 7 días pasaron:        │
  │     run_reoptimization (Sprint 5)          │
  │  2. PositionMonitor.check() (Sprint 2)      │
  │     cierra stops/TPs via broker             │
  │  3. engine.run(workflow_data)              │
  └──────────────────────────────────────────┘
            │
            ▼
  ┌─── workflow engine ────────────────────────┐    ┌──────────────────┐
  │  src/workflows/engine.py                    │    │ src/workflows/    │
  │  Carga trading_loop.yaml                   │◄───│ trading_loop.yaml │
  │  Ejecuta steps secuencialmente              │    │ 5 steps            │
  │  state[step_id] = result                   │    └──────────────────┘
  └──────────────────────────────────────────┘
            │
            ▼
  ┌─── 5 steps (YAML) ──────────────────────┐
  │                                          │
  │  analyze_market                           │
  │  └─► MarketAnalystAgent.fetch_and_analyze │
  │      • yfinance (BTC/SPY/QQQ/GLD/USO)    │
  │      • 3 timeframes cada asset             │
  │      • 14 indicadores × asset × TF         │
  │      • Publica MARKET_DATA_READY           │
  │                                          │
  │  generate_hypotheses                      │
  │  └─► StrategyAgent.evaluate_strategies    │
  │      • Detecta cruces (NO estado)          │
  │      • RSI, MACD, EMA-cross                │
  │                                          │
  │  debate_hypotheses                        │
  │  └─► DebateAgent.run_debate               │
  │      • Bull Researcher (evidencia a favor) │
  │      • Bear Researcher (evidencia en contra)│
  │      • Risk Team (duplicación, sector)    │
  │      • Portfolio Manager (síntesis)       │
  │                                          │
  │  risk_evaluation                          │
  │  └─► RiskManagerAgent.validate_and_size   │
  │      • ATR-based stop (Sprint 0)           │
  │      • qty = risk / distance              │
  │      • Mandate Gate check (Sprint 1)       │
  │      • max_open_trades (Sprint 2)         │
  │      • Persiste Position en repo (S2)      │
  │                                          │
  │  execute_trades                           │
  │  └─► ExecutionAgent.simulate_execution    │
  │      • Publica ORDER_APPROVED al bus       │
  │      • (ExecutionNode consume y ejecuta)  │
  └──────────────────────────────────────────┘
            │
            ▼ (event bus pub/sub)
  ┌─── pub/sub ────────────────────────────┐
  │  src/core/event_bus.py                     │
  │                                          │
  │  ORDER_APPROVED                          │
  │  └─► ExecutionNode.on_order_approved      │
  │      • Kill Switch check                  │
  │      • Mode "human_in_the_loop" → Telegram│
  │      • Mode "auto" → broker real          │
  │      • Publica ORDER_EXECUTED            │
  │                                          │
  │  TRADES_EXECUTED                          │
  │  └─► NotificationAgent                    │
  │      • Telegram message                   │
  │                                          │
  │  SYSTEM_ERROR                             │
  │  └─► NotificationAgent                    │
  │      • Telegram alert                     │
  └──────────────────────────────────────────┘

  ┌─── sidecar data ─────────────────────────┐
  │  PositionRepository (Sprint 2)            │
  │  • Persiste en disco (sobrevive crashes)  │
  │  • PositionMonitor consulta cada tick     │
  │  • Max drawdown → cierra stop            │
  │  • TP hit → registra realized PnL          │
  │                                          │
  │  AuditLedger (Sprint 1)                   │
  │  • Append-only JSONL con fsync            │
  │  • Registra TODO evento relevante          │
  └──────────────────────────────────────────┘
```

## Flujo de datos

```
yfinance (gratis) → MarketAnalyst (calcular indicadores)
                 → EventBus (MARKET_DATA_READY)
                 → StrategyAgent (detectar cruces)
                 → DebateAgent (Bull/Bear/Risk/PM)
                 → RiskManager (sizing + Mandate Gate + persist Position)
                 → ExecutionAgent (publica ORDER_APPROVED)
                 → EventBus (ORDER_APPROVED)
                 → ExecutionNode (ejecuta real o paper)
                 → Broker (Binance ccxt) o paper
                 → AuditLedger (registro de todo)
                 → PositionMonitor (cada tick: checa stops/TPs)
                 → Dashboard Streamlit (visualización)
```

## State machines

- [[Modules/Component_State_Machine]] — cada agente transita por
  PRE_INIT → READY → RUNNING, con DEGRADED/FAULTED para fallos.
- [[Modules/Position_Repository]] — posiciones transitan `OPEN → CLOSED`
  con realized PnL registrado al cierre.

## Cross-references

- [[Project_History]] — cómo llegamos aquí
- [[Sprints_Index]] — los 7 sprints que construyeron este sistema
- [[Modules_Index]] — detalle de cada módulo
- [[Bugs_Index]] — bugs encontrados y corregidos
- [[Inspirations]] — de dónde vino cada idea
