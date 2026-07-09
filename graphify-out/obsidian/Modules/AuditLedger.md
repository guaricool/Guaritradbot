# AuditLedger

`src/safety/audit_ledger.py`

## Responsabilidad

Log **append-only** en formato JSONL. Source of truth para forensics
post-mortem ("¿por qué el bot hizo esto el martes a las 3 AM?").

Inspirado en Vibe-Trading's "full audit ledger" pattern.

## Formato

Una línea JSON por evento:
```json
{"ts": 1783574437.16, "iso": "2026-07-09T00:00:37", "event_type": "TRADE_FILLED", "asset": "BTC-USD", "qty": 0.001, "entry_price": 62000, "status": "FILLED (SIMULATED)"}
```

## Escritura atómica

```python
with open(self.path, "a", encoding="utf-8") as f:
    f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    f.flush()
    os.fsync(f.fileno())
```

Garantiza que cada evento está **persistido en disco** antes de retornar.
Si el proceso muere, no perdemos eventos.

## Tipos de eventos registrados

| Evento | Origen |
|--------|--------|
| `BOT_START` | main.py |
| `BOT_START_BLOCKED_KILLSWITCH` | main.py (cuando KS armado) |
| `WORKFLOW_START` / `WORKFLOW_END` | main.py |
| `MARKET_DATA_READY` | MarketAnalystAgent |
| `HYPOTHESIS_GENERATED` | (Sprint 8+ pendiente) |
| `MANDATE_OK` / `MANDATE_BLOCKED` | RiskManager |
| `TRADE_APPROVED` / `TRADE_REJECTED` | RiskManager |
| `POSITION_OPENED` | RiskManager (Sprint 2) |
| `TRADE_FILLED` / `TRADE_FAILED` | ExecutionNode |
| `TRADE_BLOCKED_KILLSWITCH` / `TRADE_SKIPPED_NO_TTY` / `TRADE_REJECTED_HUMAN` | ExecutionNode |
| `TRADE_CLOSED` | PositionMonitor (Sprint 2) |
| `DEBATE_APPROVED` / `DEBATE_REJECTED` | DebateAgent (Sprint 3) |
| `REOPT_START` / `REOPT_NEW_PARAMS` / `REOPT_ERROR` | EpochScheduler (Sprint 5) |
| `COMPONENT_STATE_xxx` | Component (Sprint 6) |
| `ORDER_PENDING_APPROVAL` / `ORDER_EXECUTED` | ExecutionNode / Notification |
| `SYSTEM_ERROR` | NotificationAgent |

## API

```python
audit = AuditLedger("audit/audit.jsonl")

# Append (atómico + fsync)
event = audit.append("TRADE_FILLED", {"asset": "BTC-USD", "qty": 0.001})

# Lectura
all = audit.read_all()             # todos los eventos como list[dict]
since = audit.read_since(ts)        # eventos con ts >= ts
typed = audit.read_by_type("X")     # filtrar por event_type

# Stats
summary = audit.summary()
# {"total_events": 16, "by_type": {"BOT_START": 1, "TRADE_FILLED": 3, ...}}
```

## Comandos útiles

```bash
# Ver últimos N eventos
tail -50 audit/audit.jsonl | jq -r '.iso + " " + .event_type + " " + (.asset // "-")'

# Contar por tipo
cat audit/audit.jsonl | jq -r '.event_type' | sort | uniq -c | sort -rn

# Filtrar solo fills
cat audit/audit.jsonl | jq 'select(.event_type == "TRADE_FILLED")'
```

## Inmutabilidad

El ledger es **append-only por convención**. No hay método de delete o
update. Si se necesita "corregir" algo, se agrega un evento nuevo
(`CORRECTION_OF_xxx`).

## Configurable path

`config.yaml`:
```yaml
mandate:
  audit_log_dir: "audit"
```

Por default persiste en `audit/audit.jsonl`. El directorio `.gitignore` lo excluye
del repo (es data, no código).

## Conecta con

- **TODOS** los agents / modules — es la fuente universal de audit
- [[Sprints/Sprint_1_Safety_Layer]]
