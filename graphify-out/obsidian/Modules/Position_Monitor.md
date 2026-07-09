# PositionMonitor

`src/data_store/position_monitor.py`

## Responsabilidad

**Cada tick del scheduler** (antes del workflow engine):

1. Itera posiciones abiertas
2. Compara current_price con stop_loss y take_profit
3. Si hit → ejecuta close via broker + marca cerrada en repo
4. Registra `TRADE_CLOSED` en audit ledger con realized_pnl + duration

## Flujo

```
EpochScheduler.job()
   │
   ├── 1. PositionMonitor.check(current_prices)   ← NUEVO Sprint 2
   │      │
   │      └── Para cada pos abierta:
   │             ├── hit = pos.should_close_at(price)
   │             ├── if hit:
   │             │      ├── broker.create_market_order(opposite side)
   │             │      └── repo.close_position(pos, price, reason)
   │             │
   │             └── audit.append("TRADE_CLOSED", ...)
   │
   ├── 2. engine.run(workflow_data)
   │      ... MarketAnalyst → Strategy → Debate → Risk → Exec
   │
   └── 3. save_state
```

## Update precios

El monitor necesita precios actuales. Usa `MarketAnalyst.fetch_one()`
para cada asset con posición abierta (1d timeframe, 1mo period).

## Helper fetching

En `main.py`:
```python
def job_with_monitor():
    opens = position_repo.open()
    if opens:
        ma = MarketAnalystAgent()
        prices = {}
        for pos in opens:
            df = ma.fetch_one(pos.asset, interval="1d", period="1mo")
            if df is not None and len(df) > 0:
                prices[pos.asset] = float(df["Close"].iloc[-1])
        if prices:
            closed = position_monitor.check(prices)
```

## Symbol mapping

El monitor busca variantes:
```python
price = current_prices.get(asset)  # "BTC-USD"
if price is None:
    alt = asset.replace("/", "-").replace("USDT", "-USD")  # "BTC-USD" → "BTC-USD"
    if "/" in asset:
        # ccxt format "BTC/USDT" → yfinance "BTC-USD"
```

## Conecta con

- [[Modules/Position_Repository]] — `open()` para iterar, `close_position()` para marcar cerrada
- [[Modules/BrokerClient]] — envío de orden opuesta
- [[Modules/AuditLedger]] — TRADE_CLOSED con realized_pnl
- [[Modules/MarketAnalystAgent]] — usa `fetch_one()` para precios actuales
- [[Sprints/Sprint_2_Position_Tracking]] — sprint principal
