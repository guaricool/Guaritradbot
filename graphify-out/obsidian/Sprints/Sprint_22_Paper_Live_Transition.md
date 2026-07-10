# Sprint 22 — Paper→Live Transition Safe Mode

**Fecha**: 2026-07-09
**Status**: ✅ Cerrado (10/10 tests passing)
**Score delta**: +0.3 (seguridad operacional)
**Inspiración**: Carlos preguntó "¿qué pasa cuando paso de paper a live?" — la respuesta reveló un riesgo operacional serio.

## Resumen

Antes del Sprint 22, el bot hacía transición paper → live sin ninguna validación. Eso era peligroso:

1. **Ghost positions**: posiciones paper en `data_store/positions.json` no existen en el exchange real → el bot cree que las tiene.
2. **SL/TP en ghosts**: PositionMonitor intenta cerrar al SL/TP → orden de venta de un asset que NO tienes en live.
3. **Exposure incorrecta**: MandateGate calcula exposure sobre ghosts → puede rechazar señales legítimas o permitir overexposure.
4. **Daily loss incorrecto**: calculado sobre P&L paper, no corresponde con el exchange real.

Sprint 22 añade un **pre-flight checklist** que se ejecuta automáticamente al detectar transición paper → live.

## Componentes

### `src/safety/paper_to_live.py`

Tres clases:
- `TransitionDecision` — dataclass con el resultado del checklist
- `PaperToLiveChecklist` — la lógica del checklist
- `run_preflight()` — función de conveniencia para llamar desde main.py

### El checklist ejecuta 4 pasos

1. **Verifica conectividad del broker live** (`get_usdt_balance()`)
2. **Cuenta posiciones paper abiertas** (`position_repo.count_open()`)
3. **Maneja las posiciones paper** (3 opciones):
   - **close**: las cierra en repo a entry_price (simulated P&L 0)
   - **ignore**: las deja pero loggea `LIVE_TRANSITION_PAPER_IGNORED` warning
   - **abort**: rechaza proceder (default)
4. **Dry-run validation**: envía una orden mínima (BTC qty=0.00001) al broker live para verificar end-to-end

### Audit events nuevos

| Event | Cuándo | Payload |
|---|---|---|
| `LIVE_TRANSITION_CHECK` | Inicio del checklist | open_paper_positions, broker_balance_usd, interactive, auto_action |
| `LIVE_TRANSITION_BLOCKED` | Si el checklist rechaza y forzamos paper mode | reason, forced_back_to_paper |
| `LIVE_TRANSITION_APPROVED` | Si pasa todos los checks | broker_balance, paper_positions_closed, dry_run_validated |
| `LIVE_TRANSITION_PAPER_IGNORED` | Si eligió ignore | open_paper_positions, warning |
| `PAPER_POSITION_CLOSED_PRE_LIVE` | Una paper position cerrada | position_id, asset, entry_price, reason |
| `DRY_RUN_OK` / `DRY_RUN_FAILED` / `DRY_RUN_EXCEPTION` | Validación dry-run | symbol, qty, order_id, error |

### Wire-up en main.py

```python
mandate_being_enabled = bool(config.get("mandate", {}).get("enabled", False))
exchange_use_testnet = bool(config.get("exchange", {}).get("use_testnet", True))
is_live_attempt = mandate_being_enabled and not exchange_use_testnet

if is_live_attempt:
    checklist = PaperToLiveChecklist(...)
    decision = checklist.run(dry_run=True)
    if not decision.proceed:
        config["mandate"]["enabled"] = False  # force back to paper
        audit.append("LIVE_TRANSITION_BLOCKED", {...})
```

### Configuración nueva (config.yaml)

```yaml
live_transition:
  auto_action: "abort"   # default safe; for daemon mode
  dry_run_qty: 0.00001   # BTC qty for validation (~50¢ at $50k)
```

## Tests (10/10 passing)

```
tests/test_paper_to_live.py
├── PaperToLiveHappyPathTest
│   └── test_no_paper_positions_proceeds_to_live       ✓
├── PaperPositionsHandlingTest
│   ├── test_auto_action_close_removes_paper_positions        ✓
│   ├── test_auto_action_ignore_keeps_paper_positions_warn    ✓
│   └── test_auto_action_abort_blocks_live                    ✓
├── BrokerConnectivityTest
│   ├── test_broker_connection_failure_blocks_live            ✓
│   ├── test_broker_zero_balance_blocks_live                  ✓
│   └── test_no_broker_blocks_live                            ✓
├── DryRunValidationTest
│   ├── test_dry_run_failure_blocks_live                      ✓
│   └── test_dry_run_skipped_when_disabled                    ✓
└── RunPreflightConvenienceTest
    └── test_run_preflight_with_minimal_config                ✓
```

## Uso

### Interactive mode (TTY)
```bash
# Cuando config.yaml tiene mandate.enabled=true y exchange.use_testnet=false:
python main.py

# Output:
# ⚠️  LIVE TRANSITION CHECKLIST
#    3 paper positions detected in repo.
#    These DO NOT exist on the live exchange.
#
# What should we do with these positions?
#   [C]lose all (mark as closed in repo, simulated P&L)
#   [I]gnore (proceed with live; bot will track them but they don't exist on exchange)
#   [A]bort (do NOT proceed to live)
#
# Choice (C/I/A): C
# [Checklist] Closed 3 paper positions.
# [Checklist] ✅ Broker connected. Balance: $20.00
# [Checklist] Dry-run: placing test order on BTC/USDT qty=1e-05
# [Checklist] ✅ Dry-run succeeded: order DRY_TEST_123
# ✅ Pre-flight passed. Live mode is GO.
```

### Daemon mode (no TTY)
```yaml
# config.yaml
live_transition:
  auto_action: "abort"   # safest default for non-interactive
```
```bash
# El bot usa auto_action. Si hay paper positions y auto_action=abort, NO procede.
```

## Por qué dry-run

Una orden real de 0.00001 BTC cuesta ~$0.50. Eso valida:
- ✅ API authentication
- ✅ Routing a Binance US (no binance.com bloqueado desde USA)
- ✅ Order matching engine funcionando
- ✅ Balance fetching funciona

Si todo eso pasa con $0.50, sabemos que el sistema puede operar con $20.

## Lecciones aprendidas

1. **Nunca confíes en state local para reconciliar con exchange real**: el estado paper debe estar completamente separado del live.
2. **Dry-runs con qty mínima** son la forma estándar de validar un exchange nuevo (paper de Binance, testnet de Binance, etc.).
3. **Default safe = abort**: cuando no puedes preguntar al usuario, NO asumas que quiere proceder.
4. **Audit forense para transiciones**: cada cambio de modo debe quedar registrado con timestamp, razón, y outcome.

## Próximos pasos

- Sprint 23: GradientBoostingClassifier para ML pipeline (Sprint 19 upgrade)
- Sprint 24: Multi-broker via ccxt (último gap importante)
- Sprint 25: Optuna para hyperopt del modelo ML

## Score de capacidad actualizado

| Capacidad | Antes | Después |
|---|---|---|
| Operational safety | ⚠️ | ✅ (dry-run + checklist) |
| Transition safety | ❌ | ✅ |
| Forense audit | ✅ | ✅ (+5 event types) |

**Score global sube a ~80%** con este sprint (de 78%).