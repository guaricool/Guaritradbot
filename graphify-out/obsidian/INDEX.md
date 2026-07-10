# Guaritradbot — Memoria del Proyecto

> Bot de trading algorítmico multi-agente. Documentación viva.
> Construido entre 2026-05/06 (Gemini AI) y refactorizado Sprint 0-7 (Mavis, 2026-07-08/09).

## 🌟 Punto de entrada

- **¿Nuevo en el proyecto?** Lee [[Architecture]] → [[Project_History]]
- **¿Bug a investigar?** → [[Bugs_Index]]
- **¿Qué hace cada sprint?** → [[Sprints_Index]]
- **¿Cómo corre un módulo?** → [[Modules_Index]]
- **¿Por qué X se hizo así?** → [[Inspirations]]
- **¿Cómo desplegar en producción?** → [[Deployment]]

## 📂 Estructura del vault

```
Guaritradbot_Vault/
├── INDEX.md                    ← estás aquí
├── Architecture.md              ← diagrama del sistema
├── Project_History.md          ← origen y evolución
├── Sprints_Index.md            ← índice de los 7 sprints
├── Sprints/
│   ├── Sprint_0_Critical_Bug_Fixes.md
│   ├── Sprint_1_Safety_Layer.md
│   ├── Sprint_2_Position_Tracking.md
│   ├── Sprint_3_Multi_Agent_Debate.md
│   ├── Sprint_4_Backtester_Fix.md
│   ├── Sprint_5_Real_Reoptimization.md
│   ├── Sprint_6_State_Machine_Data_Integrity.md
│   ├── Sprint_7_PDF_Indicators.md
│   ├── Sprint_18_Audit_Fixes_Portfolio_Management.md
│   ├── Sprint_19_ML_Pipeline.md                  ← Sprint 19 (2026-07-09)
│   └── Sprint_21_Alpha_Zoo.md                    ← Sprint 21 (2026-07-09)
├── Modules_Index.md             ← índice de los módulos
├── Modules/
│   ├── MarketAnalystAgent.md
│   ├── StrategyAgent.md
│   ├── RiskManagerAgent.md
│   ├── ExecutionAgent.md
│   ├── ExecutionNode.md
│   ├── DebateAgent.md           ← Sprint 3
│   ├── PositionMonitor.md       ← Sprint 2 (+ Sprint 18: smart profit-take)
│   ├── AuditLedger.md           ← Sprint 1
│   ├── KillSwitch.md            ← Sprint 1
│   ├── MandateGate.md           ← Sprint 1 (+ Sprint 18: source-of-truth fix)
│   ├── Component_State_Machine.md  ← Sprint 6
│   ├── Data_Validator.md        ← Sprint 6
│   ├── EventBus.md
│   ├── PositionRepository.md    ← Sprint 2
│   ├── WorkflowEngine.md
│   ├── AlphaZoo.md              ← Sprint 21 (48 features)
│   └── MLPipeline.md            ← Sprint 19 (FeatureExtractor + ModelTrainer + Predictor)
├── Bugs_Index.md               ← lista maestra de bugs (23 total)
├── Bugs/
│   ├── B001_emit_vs_publish.md … B016_pos_id_uuid_collision.md
│   ├── B017_micro_account_death_loop.md      ← Sprint 18
│   ├── B018_phantom_exposure_lockup.md       ← Sprint 18
│   ├── B019_punished_for_trying.md           ← Sprint 18
│   ├── B020_replacement_loop.md              ← Sprint 18 patch
│   ├── B021_phantom_pnl_replacement.md       ← Sprint 18 patch
│   ├── B022_smart_take_dead_code.md          ← Sprint 18 patch
│   └── B023_dashboard_filter_button_flash.md  ← Sprint 18 patch
├── Inspirations.md              ← 5 repos + NautilusTrader, de dónde viene cada idea
└── Deployment.md                ← cómo subir a Coolify VPS
```

## 🔗 Conexiones rápidas (wikilinks)

El bot tiene **3 capas principales** ([ver arquitectura](Architecture.md)):

```
🛡️ SAFETY (Sprint 1)
├── MandateGate → valida universe, exposure, daily cap
├── KillSwitch → archivo /tmp/GUARITRADBOT_KILL
└── AuditLedger → JSONL append-only

📊 STRATEGY (Sprints 0,3,7)
├── MarketAnalyst → fetch datos + indicadores + state machine (S6+S7)
├── StrategyAgent → genera hipótesis (cruces RSI/MACD/EMA)
└── DebateAgent → Bull/Bear/Risk/PortfolioManager (S3)

🛡️ EXECUTION (Sprints 0,2)
├── RiskManager → sizing ATR, mandate gate (S1), repo (S2)
├── ExecutionAgent → publica ORDER_APPROVED al bus
├── ExecutionNode → consume ORDER_APPROVED, broker real
└── PositionMonitor → cierra stops/TPs cada ciclo (S2)
```

## ⚡ Reglas duras

1. **Backtest OBLIGATORIO** antes de cualquier trade (Sprint 4 walk-forward)
2. **1% del balance por trade** (regla #1 del playbook) — o más alto en cuentas < $50
3. **ATR(14) × 2 = stop loss** (no $5 hardcoded, no inventar)
4. **Risk:Reward mínimo 1:2** (TP = 2× stop distance)
5. **5 trades máximo abiertos** simultáneos
6. **Audit ledger NUNCA se borra** (forensics post-mortem)
7. **Sprint 18 — Exposure = PositionRepository** (NO suma de TRADE_FILLED sin restar)
8. **Sprint 18 — Daily loss = realized PnL** (NO risk_usd teórico)
9. **Sprint 18 — Notional < min_order → auto-adjust** (no rechazar)

## 🔍 Comandos rápidos

```bash
# Test completo (paper mode)
python main.py --once

# Modo daemon (24/7 en VPS)
python main.py

# Ver últimas trades / estado
cat latest_state.json

# Auditoría forense
cat audit/audit.jsonl | jq '.'

# Armar / desarmar kill switch
python -c "from src.safety.kill_switch import KillSwitch; ks=KillSwitch('/tmp/GUARITRADBOT_KILL'); ks.arm()"
python -c "from src.safety.kill_switch import KillSwitch; ks=KillSwitch('/tmp/GUARITRADBOT_KILL'); ks.disarm()"

# Activar mandate gate (cambiar config.yaml)
# mandate.enabled: true
```

## 📊 Métricas del proyecto

| | |
|--|--|
| Sprints | **9 cerrados** (0-7 + 18) |
| Commits locales | **9** (en `main`, sin push) |
| Archivos Python | **~26** (sin contar los external_repos, incluyendo tests/) |
| Líneas de código añadidas | ~4,400 (incluyendo Sprint 18) |
| Bugs encontrados | **19** (todos corregidos y testeados) |
| Tests pasando | **18 unit tests Sprint 18** + scripts legacy /tmp/test_sprintN.py |
| Inspiraciones externas | 6 (5 repos + NautilusTrader) |
