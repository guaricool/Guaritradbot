# PositionRepository

`src/data_store/positions.py`

## Responsabilidad

Persiste **posiciones abiertas** en disco. Sobrevive crashes.

Inspirado en **NautilusTrader** crash-only design:

> "Unified recovery path - Startup and crash recovery share the same
> code path, ensuring it is well-tested. Externalized state - Critical
> state is meant to be persisted externally when configured, reducing
> data-loss risk."

## Storage

Archivo: `data_store/positions.json`. Escritura **atómica**:
```python
tmp = self.path.with_suffix(".tmp")
tmp.write_text(json.dumps(data, indent=2, ...))
tmp.replace(self.path)
```

Eso evita archivos corruptos en mitad de escritura.

## Position dataclass

```python
@dataclass
class Position:
    asset: str
    direction: str                # "long" | "short"
    entry_price: float
    stop_loss: float
    take_profit: float
    qty: float
    risk_usd: float
    entry_ts: float
    strategy: str
    position_id: str               # pos_<ts_ms>_<uuid8>
    
    closed_ts: Optional[float] = None
    closed_price: Optional[float] = None
    close_reason: Optional[str] = None
    realized_pnl: Optional[float] = None
```

## Bug B016 cerrado

`position_id = f"pos_{int(time.time()*1000)}"` colisionaba si dos
posiciones se creaban en el mismo milisegundo. Fix:
`f"pos_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"`.

## API

```python
repo = PositionRepository("data_store/positions.json")

# Abrir
pos = Position(asset="BTC-USD", direction="long", entry_price=60000,
               stop_loss=58000, take_profit=70000, qty=0.001,
               risk_usd=2.0, entry_ts=time.time(), strategy="MACD_BullCross")
repo.add_open(pos)

# Queries
repo.all()                           # todas (open + closed)
repo.open()                          # solo abiertas
repo.open_for_asset("BTC-USD")      # abiertas por asset
repo.count_open()                    # int
repo.total_exposure_usd()            # suma de notionals abiertos
repo.total_realized_pnl_usd()        # suma de PnL cerradas

# Cerrar (por position_id)
closed = repo.close_position(pos.position_id, price=current_price, reason="STOP_HIT")

# should_close_at
hit, reason = pos.should_close_at(current_price)  # ("STOP_HIT" | "TP_HIT" | "")
```

## Conecta con

- [[Modules/RiskManagerAgent]] — `add_open()` tras aprobar cada trade
- [[Modules/Position_Monitor]] — `close_position()` cuando stops/TPs se tocan
- [[Sprints/Sprint_2_Position_Tracking]] — sprint principal
