# Sprint 24 — Dashboard Equity Widget + Persistence

**Fecha**: 2026-07-09
**Status**: ✅ Cerrado (12/12 tests nuevos passing)
**Score delta**: +0.3 (UX + crash-only design)

## Resumen

Sprint 23 creó el `EquityTracker` en logs. Sprint 24 lo trae al **dashboard** con un widget prominent + persistencia en disco (crash-only design).

## Componentes

### 1. Persistencia crash-only

`persist_tracker(tracker, path)` y `load_tracker(path)` en `src/safety/equity_tracker.py`:

```python
# Save (atómico, Sprint 2 pattern)
persist_tracker(equity_tracker, "data_store/equity_state.json")

# Load (al startup)
tracker = load_tracker(
    "data_store/equity_state.json",
    position_repo=position_repo,
    audit=audit,
)
```

Atomic write: temp + replace → si el bot crashea durante el save, el archivo previo queda intacto.

Restaurado al cargar:
- `starting_balance`
- `precision`
- `max_equity` (peak para drawdown calc)
- `history` (todos los snapshots)

### 2. Wire-up en main.py

```python
# Init: try to load from disk, fallback to broker balance
if os.path.exists("data_store/equity_state.json"):
    equity_tracker = load_tracker(_equity_state_path, position_repo, audit)
else:
    equity_tracker = EquityTracker(starting_balance=broker_balance, ...)

# After every update(): persist
snap = equity_tracker.update(prices)
persist_tracker(equity_tracker, _equity_state_path)
```

### 3. Dashboard widget

Nuevo bloque prominent en el dashboard, justo después del KPI "Equity" principal:

```
┌─────────────────────────────────────────────┐
│ 💰 Live Equity Tracker (Sprint 24)         │
├──────────────────┬──────────────────────────┤
│ 🟢               │   [sparkline chart]      │
│ $10.0123         │   $10.0 ──── $10.0123     │
│ +$0.0123 (+0.12%)│                          │
│ Realized: +$0.005│                          │
│ Unrealized:+$0.007│                         │
│ Drawdown: -0.50% │                          │
└──────────────────┴──────────────────────────┘
📊 23 snapshots persisted | Last update: 2026-07-09T20:45:23
```

Componentes visuales:
- **Card grande** con emoji 🟢/🔴, número con gradient text, delta formateado
- **Sparkline** Plotly con fill (verde/rojo según dirección)
- **Stats secundarios**: realized, unrealized, drawdown
- **Caption**: count de snapshots + timestamp del último

## CSS custom para el widget

```css
.equity-card {
  background: linear-gradient(135deg, #141937, #1a1f3a);
  border: 1px solid #2a3050;
  border-radius: 12px;
  padding: 16px 20px;
}
.equity-card.equity-positive { border-left: 4px solid #06d6a0; }
.equity-card.equity-negative { border-left: 4px solid #f72585; }
.equity-total {
  font-size: 2.2rem;
  font-weight: 800;
  background: linear-gradient(135deg, #4cc9f0, #06d6a0);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}
```

## Tests (12 nuevos)

```
tests/test_equity_tracker.py (persist tests)
├── test_persist_creates_file                          ✓
├── test_persist_then_load_roundtrip                   ✓
├── test_persist_includes_audit_state                  ✓
├── test_load_missing_file_raises                      ✓
├── test_load_corrupt_file_raises                      ✓
├── test_atomic_write_does_not_corrupt_on_failure      ✓
└── test_load_restores_max_equity_for_drawdown         ✓

tests/test_equity_widget.py (dashboard data shape)
├── test_persisted_state_has_required_fields           ✓
├── test_equity_series_parsing                         ✓
├── test_latest_snapshot_extraction                    ✓
├── test_positive_when_delta_positive                  ✓
└── test_negative_when_delta_negative                  ✓

TOTAL: 83/83 tests passing
```

## Por qué persistencia en disco (crash-only)

Si el bot corre 24/7 y se reinicia por update / crash / deploy:
- **Antes (Sprint 23)**: equity history se perdía. Empezabas de nuevo en `$10.0000`.
- **Ahora (Sprint 24)**: el tracker carga su history desde disco. Continúas donde quedaste.

Pattern consistente con:
- `PositionRepository` (Sprint 2) — posiciones persisten en disco
- `AuditLedger` (Sprint 1) — eventos persisten en JSONL
- `EquityTracker` (Sprint 24) — history persiste en JSON

## Uso en el dashboard

1. El bot corre, actualiza el tracker cada ciclo
2. Persiste a disco
3. Al abrir el dashboard, lee `equity_state.json` y renderiza el widget
4. El sparkline muestra los últimos 50 equity values (~16 horas si corre cada 5min)

## Score de capacidad actualizado

| Capacidad | Antes | Después |
|---|---|---|
| Real-time P&L display | ✅ (logs) | ✅ (dashboard widget prominente) |
| Equity curve history | ✅ (memoria) | ✅ (disco, crash-only) |
| UX | ⚠️ (logs only) | ✅ (visual + sparkline) |
| Forense audit | ✅ | ✅ |

**Score global sube a ~83%** con este sprint.

## Próximos pasos

- **Sprint 25**: Telegram equity updates (notificación periódica al celular).
- **Sprint 26**: Multi-timeframe equity (separar curves 1h/4h/1d).

Ver [[../Sprints/Sprint_23_Live_Equity_Tracker]] para el sprint anterior.