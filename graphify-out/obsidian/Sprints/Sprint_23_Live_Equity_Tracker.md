# Sprint 23 — Live Equity Tracker (centavos visibles)

**Fecha**: 2026-07-09
**Status**: ✅ Cerrado (16/16 tests passing)
**Inspiración**: Carlos preguntó: "¿no hay manera de que si mete 10 dólares entonces y va arriba así sea muy poquito entonces pueda enseñarte cuántos centavos o dólares vas ganando o perdiendo?"

## Resumen

Antes del Sprint 23, el bot solo mostraba realized P&L agregado en el dashboard. No había equity curve en vivo con precisión sub-dólar.

Ahora: **`EquityTracker`** calcula el equity total en tiempo real con **precisión de 4 decimales** (`$10.0123`), incluyendo realized + unrealized PnL, drawdown, y un historial para sparklines.

## Componentes

### `src/safety/equity_tracker.py`

```python
from src.safety.equity_tracker import EquityTracker, format_equity_line

tracker = EquityTracker(
    starting_balance=10.00,        # $10 default
    position_repo=position_repo,   # source of truth
    audit=audit,
    history_size=200,
)
snapshot = tracker.update(current_prices={"BTC-USD": 50100.0})
print(format_equity_line(snapshot, precision=4))
# → 🟢 Equity: $10.1000 | ΔP&L: +$0.1000 (+1.00%) | Open: 1 | Drawdown: 0.00%
```

### `EquitySnapshot` (dataclass)

Campos:
- `timestamp`, `iso` (cuándo se tomó la foto)
- `starting_balance` (baseline, constante)
- `realized_pnl` (suma de P&L de posiciones cerradas)
- `unrealized_pnl` (mark-to-market de posiciones abiertas)
- `total_equity` (starting + realized + unrealized)
- `delta_usd`, `delta_pct` (vs baseline)
- `open_positions`, `closed_positions` (counts)
- `drawdown_usd`, `drawdown_pct` (vs peak histórico)

### `EquityTracker` methods

- `update(current_prices)` → EquitySnapshot (también appenda al history + audit log)
- `latest()` → most recent snapshot
- `equity_series()` → List[float] para sparklines
- `summary()` → dict para el dashboard
- `format_equity_line()` → string formateado para logs / Telegram

### Wire-up en main.py

```python
# Initialize with broker's actual balance (or $10 fallback)
_initial_balance = broker_client.get_usdt_balance() if broker_client else 10.0
equity_tracker = EquityTracker(
    starting_balance=_initial_balance,
    position_repo=position_repo,
    audit=audit,
)

# In job_with_monitor() after fetching prices:
snap = equity_tracker.update(prices)
print(f"  [Equity] {format_equity_line(snap, precision=4)}")
```

### Audit event nuevo: `EQUITY_UPDATE`

Cada `update()` emite:
```json
{
  "event_type": "EQUITY_UPDATE",
  "total_equity": 10.0123,
  "realized_pnl": 0.0050,
  "unrealized_pnl": 0.0073,
  "delta_usd": 0.0123,
  "delta_pct": 0.123,
  "drawdown_pct": 0.0,
  "open_positions": 1,
  "closed_positions": 2
}
```

## Tests (16/16 passing)

```
tests/test_equity_tracker.py
├── EquitySnapshotTest
│   ├── test_sub_dollar_precision            ✓ ($10.0123 visible)
│   └── test_to_dict_roundtrip              ✓
├── EquityTrackerBasicTest
│   ├── test_initial_state_no_positions      ✓
│   ├── test_profitable_position_increases_equity  ✓
│   ├── test_losing_position_decreases_equity     ✓
│   ├── test_realized_pnl_added_after_close  ✓
│   └── test_combined_realized_and_unrealized       ✓
├── EquityTrackerHistoryTest
│   ├── test_history_accumulates             ✓
│   ├── test_history_capped_at_maxlen        ✓ (ring buffer)
│   └── test_equity_series_for_sparklines    ✓
├── EquityTrackerDrawdownTest
│   └── test_drawdown_tracks_peak            ✓
├── EquityTrackerAuditTest
│   └── test_update_emits_audit_event        ✓
├── FormatEquityLineTest
│   ├── test_format_with_precision           ✓
│   └── test_format_negative_delta           ✓
└── EquityTrackerValidationTest
    ├── test_negative_starting_balance_rejected     ✓
    └── test_zero_starting_balance_rejected         ✓
```

## Uso

### En el dashboard (próximo)

```python
import streamlit as st
from src.safety.equity_tracker import EquityTracker, format_equity_line

# Sprint 24 will add: dashboard widget that shows
# 💰 Equity: $10.0123
# ΔP&L: +$0.0123 (+0.12%)    [green/red]
# 📊 sparkline with last 50 equity values
```

### En logs del bot

```
[EquityTracker] initialized with $10.0000
[Equity] 🟢 Equity: $10.0123 | ΔP&L: +$0.0123 (+0.12%) | Open: 0 | Drawdown: 0.00%
[Equity] 🟢 Equity: $10.1245 | ΔP&L: +$0.1245 (+1.24%) | Open: 1 | Drawdown: 0.00%
[Equity] 🔴 Equity: $9.9876 | ΔP&L: $-0.0124 (-0.12%) | Open: 1 | Drawdown: -1.07%
```

### En Telegram notifications (futuro)

```
🤖 Bot Update
💰 Equity: $10.0123 (+0.12%)
📊 Open: 1 | Realized: $0.005 | Unrealized: $0.007
📉 Drawdown: -0.50% from peak
```

## Diseño: ¿por qué 4 decimales?

Para cuenta de $10:
- 1 centavo = 0.1% del balance
- $0.0001 = 0.001% (ruido mínimo)

4 decimales dan **granularidad útil** sin excesivo ruido. Más decimales serían falsos positivos (BTC qty min = 0.00001 → variaciones de $0.0001 son aleatorias).

Para cuenta de $1000:
- 4 decimales siguen siendo útiles para P&L fractional
- Drawdown tracking con esa precisión ayuda a detectar drift temprano

## Lección de diseño

**Cualquier sistema que opera con dinero necesita mostrar el delta exacto, no aproximado.**

- "Made money" no es accionable.
- "+$0.0123 (+0.12%)" sí lo es.

La diferencia entre `print(realized_pnl)` y `format_equity_line(snap)` es la diferencia entre un bot que solo logea y un bot que **comunica**.

## Próximos pasos

- **Sprint 24**: Dashboard widget que muestra el equity tracker en vivo (sparkline + números grandes).
- **Sprint 25**: Enviar equity update a Telegram cada N ciclos.

## Score de capacidad actualizado

| Capacidad | Antes | Después |
|---|---|---|
| Real-time P&L display | ❌ | ✅ (precision 4 decimals) |
| Equity curve history | ❌ | ✅ (200 snapshots) |
| Drawdown tracking | ❌ | ✅ (vs peak) |
| Forense audit | ✅ | ✅ (+1 event type) |

**Score global sube a ~82%** con este sprint (de 80%).