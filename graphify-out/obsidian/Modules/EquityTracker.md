# EquityTracker (Sprint 23)

**Archivo**: `src/safety/equity_tracker.py`
**Fecha**: 2026-07-09
**Score delta**: +0.5

## Resumen

Tracker que calcula el equity del bot en tiempo real con precisión de 4 decimales. Inspirado en la pregunta de Carlos: "¿no hay manera de ver centavos o dólares ganando/perdiendo en vivo?"

## API

```python
from src.safety.equity_tracker import EquityTracker, format_equity_line

tracker = EquityTracker(
    starting_balance=10.00,        # $10 default
    position_repo=position_repo,   # source of truth
    audit=audit,
    history_size=200,
)
snap = tracker.update(current_prices={"BTC-USD": 50100.0})
print(format_equity_line(snap, precision=4))
# → 🟢 Equity: $10.1000 | ΔP&L: +$0.1000 (+1.00%) | Open: 1 | Drawdown: 0.00%
```

## EquitySnapshot

Dataclass con todos los campos:
- `timestamp`, `iso` — cuándo se tomó la foto
- `starting_balance` — baseline (constante)
- `realized_pnl` — suma de P&L de posiciones cerradas
- `unrealized_pnl` — mark-to-market de posiciones abiertas
- `total_equity` = starting + realized + unrealized
- `delta_usd`, `delta_pct` — vs baseline
- `open_positions`, `closed_positions` — counts
- `drawdown_usd`, `drawdown_pct` — vs peak histórico

## EquityTracker methods

| Method | Retorna | Uso |
|---|---|---|
| `update(current_prices)` | `EquitySnapshot` | Calcula + appenda al history + emite audit |
| `latest()` | `EquitySnapshot` | El snapshot más reciente |
| `equity_series()` | `List[float]` | Para sparklines |
| `delta_series()` | `List[float]` | Solo los deltas |
| `summary()` | `dict` | Compacto para el dashboard |

## Wire-up en main.py

```python
# Initialize (Sprint 23): use real broker balance or $10 fallback
_initial_balance = broker_client.get_usdt_balance() if broker_client else 10.0
equity_tracker = EquityTracker(starting_balance=_initial_balance, ...)

# In job_with_monitor() after fetching prices:
snap = equity_tracker.update(prices)
print(f"  [Equity] {format_equity_line(snap, precision=4)}")
```

## Audit event: `EQUITY_UPDATE`

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

Ver `tests/test_equity_tracker.py`:
- Sub-dollar precision ($10.0123)
- Profit/loss updates correctly
- Realized PnL added after close
- History capped at maxlen (ring buffer)
- Drawdown tracks peak
- Audit events emitted
- Format helper produces correct strings

## Uso

### Logs del bot

```
[EquityTracker] initialized with $10.0000
[Equity] 🟢 Equity: $10.0123 | ΔP&L: +$0.0123 (+0.12%) | Open: 0 | Drawdown: 0.00%
[Equity] 🟢 Equity: $10.1245 | ΔP&L: +$0.1245 (+1.24%) | Open: 1 | Drawdown: 0.00%
[Equity] 🔴 Equity: $9.9876  | ΔP&L: $-0.0124 (-0.12%) | Open: 1 | Drawdown: -1.07%
```

### Telegram (futuro)

```
🤖 Bot Update
💰 Equity: $10.0123 (+0.12%)
📊 Open: 1 | Realized: $0.005 | Unrealized: $0.007
📉 Drawdown: -0.50% from peak
```

### Dashboard (futuro Sprint 24)

```
💰 $10.0123  ▲ +$0.0123 (+0.12%)  [green]
📈 sparkline with last 200 equity values
```

## Diseño: ¿por qué 4 decimales?

Para cuenta de $10:
- 1 centavo = 0.1% del balance
- $0.0001 = 0.001% (ruido mínimo)

4 decimales dan **granularidad accionable** sin excesivo ruido. Más decimales serían falsos positivos (BTC qty min = 0.00001 → variaciones de $0.0001 son aleatorias).

Para cuenta de $1000:
- 4 decimales siguen siendo útiles para P&L fractional
- Drawdown tracking con esa precisión ayuda a detectar drift temprano

## Limitaciones actuales

- **Precision fija en 4 decimales**: configurable pero no expuesto al usuario.
- **History size fija (200 snapshots)**: suficiente para ~16 horas a 1 snapshot/5min.
- **No persiste history**: si el bot muere, el history se pierde (solo el último snapshot queda en audit).
- **Drawdown se calcula en runtime**: no se persiste el peak máximo histórico cross-session.

## Próximos pasos

- **Sprint 24**: Dashboard widget que muestra el equity tracker en vivo (sparkline + números grandes).
- **Sprint 25**: Telegram notifications con equity update cada N ciclos.
- **Sprint 26**: Persistir history en disco (crash-only design para el tracker también).

## Lección de diseño

**Cualquier sistema que opera con dinero necesita mostrar el delta exacto, no aproximado.**

- "Made money" no es accionable.
- "+$0.0123 (+0.12%)" sí lo es.

La diferencia entre `print(realized_pnl)` y `format_equity_line(snap)` es la diferencia entre un bot que solo logea y un bot que **comunica**.

## Por qué no usar libraries externas

- `empyrical` (Quantopian): overkill, solo para backtests
- `pyfolio`: depende de很多东西, no vale la pena para un caso simple
- Custom 100 líneas: hace exactamente lo que necesitamos, nada más

Ver [[../Sprints/Sprint_22_Paper_Live_Transition]] para el sprint anterior.