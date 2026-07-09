# Data Validator

`src/core/data_validator.py`

## Responsabilidad

**Fail-fast** sobre data corrupta. Crash explícito cuando yfinance
devuelve NaN/Inf/precio negativo/high<low.

Inspirado en **NautilusTrader** data integrity policy:

> "In trading systems, corrupt data is worse than no data. A single
> incorrect price, timestamp, or quantity can cascade through the system,
> resulting in incorrect position sizing or risk calculations, orders
> placed at wrong prices, backtests producing misleading results, silent
> financial losses."

> "By crashing immediately on invalid data, NautilusTrader aims to provide:
> 1. No silent corruption - The fail-fast policy is intended to prevent
> invalid data from propagating.
> 2. Immediate feedback - Issues are discovered during development and
> testing, not in production.
> 3. Audit trail - Crash logs clearly identify the source of invalid data.
> 4. Deterministic behavior."

## 4 funciones

```python
# 1. Un solo precio
validate_price(price, label="entry_price")
# Raises DataIntegrityError si NaN/Inf/negativo/non-number

# 2. Una sola cantidad
validate_quantity(qty, label="position_size")
# Raises DataIntegrityError si NaN/Inf/negativo/non-number

# 3. Una vela completa
validate_ohlcv_row((open, high, low, close, volume))
# Raises si H<L o V<0

# 4. DataFrame completo
validate_dataframe(df)
# Raises si faltan columnas, NaN en OHLC, Inf, negativos, o high<low en cualquier fila
```

## Test verificado (Sprint 6)

```
✅ rejected: price=nan: NaN
✅ rejected: price=inf: Infinity
✅ rejected: price=-inf: Infinity
✅ rejected: price=-0.5: negative (-0.5)
✅ rejected: price=abc: not a number ('abc')
✅ rejected: price=None: not a number (None)

✅ rejected: column Open: 1 NaN values
✅ rejected: high<low en 1 vela(s)
✅ aceptó DataFrame válido
```

## Aplicado en

`MarketAnalystAgent._validate_or_fault(df, asset_tf)` corre
`validate_dataframe(df)` después de descargar cada vela y antes
de calcular indicadores. Si falla → el componente se degrada a
`DEGRADED`.

## Por qué importa

Un bot que opera con $100 reales puede ser **destruido por UN solo
tick** con precio 0 o NaN. Sin este validator, el silencio lo
consume.

## Conecta con

- [[Modules/MarketAnalystAgent]] — primer usuario
- [[Modules/Component_State_Machine]] — DEGRADED/FAULTED
- [[Sprints/Sprint_6_State_Machine_Data_Integrity]]
