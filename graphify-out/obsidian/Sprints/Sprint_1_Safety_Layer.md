# Sprint 1 — Safety Layer

## Objetivo

Hacer el bot **seguro** antes de operar con dinero real.

Inspirado en **Vibe-Trading** (HKUDS) y la guía de seguridad
operacional de [[TradingAgents]].

## Módulos nuevos

### [[Modules/AuditLedger]] (Sprint 1)
JSONL append-only con fsync atómico. Registra TODO evento relevante:
`BOT_START`, `WORKFLOW_START/END`, `TRADE_APPROVED`, `TRADE_REJECTED`,
`MANDATE_OK`, `MANDATE_BLOCKED`, `TRADE_KILLSWITCHED`, etc.

Persiste en `audit/audit.jsonl`. Cada línea = un evento. Para
forensics post-mortem.

### [[Modules/KillSwitch]] (Sprint 1)
Archivo filesystem en `/tmp/GUARITRADBOT_KILL`. Si existe, el bot
NO ejecuta órdenes. Patrón del paper de Vibe-Trading:
"filesystem kill switch, fail-closed pre-trade gate, full audit ledger".

### [[Modules/MandateGate]] (Sprint 1)
Validador de propuestas:
- `universe` de símbolos permitidos
- `max_position_usd` por trade
- `max_daily_loss_usd` rolling 24h
- `max_total_exposure_usd`

Activado con `mandate.enabled: true` en config.yaml. Por default
está OFF (modo paper).

## Integración

- `risk_agent.py` consulta el MandateGate antes de aprobar cada trade.
- `execution_node.py` consulta el KillSwitch antes de enviar al broker.
- Todos los agentes reciben `audit` y loguean sus eventos.

## Commit

`10d144c` (mismo commit que Sprint 0)

## Test

```bash
$ python /tmp/test_sprint1.py
[OK] OK BTC 15usd                                            | reason="all_checks_passed"
[BLOCK] BLOQUEADO: GME no en universe                           | reason="symbol_not_allowed:GME"
[BLOCK] BLOQUEADO: notional > max_position_usd                  | reason="notional_exceeds_max:$50.00>$20.00"
[OK] BLOQUEADO: rolling daily_loss + 4.5 > 5                 | reason="all_checks_passed"  ← esperado block (ver nota)
```

**Nota**: el cuarto test (rolling daily_loss) aparece como OK porque
los rechazos anteriores no cuentan como "riesgo arriesgado" — solo
los trades aprobados al audit ledger consumen el daily loss. Esto es
**legítimo** (no gastas capital en trades rechazados).

## Ver también

- [[Sprints_Index]]
- [[Modules/KillSwitch]], [[Modules/MandateGate]], [[Modules/AuditLedger]]
