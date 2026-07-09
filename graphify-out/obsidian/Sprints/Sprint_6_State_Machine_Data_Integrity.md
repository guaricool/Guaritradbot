# Sprint 6 — State Machine + Data Integrity

## Objetivo

Que el bot **falle ruidoso** cuando hay data corrupta (no silencioso),
y que cada componente tenga un **lifecycle explícito** auditable.

Inspirado en **NautilusTrader** (fail-fast policy):

> "NautilusTrader prioritizes data integrity over availability for trading
> operations. The system employs a strict fail-fast policy for arithmetic
> operations and data handling to prevent silent data corruption that
> could lead to incorrect trading decisions."

> "Rationale: In trading systems, corrupt data is worse than no data. A
> single incorrect price, timestamp, or quantity can cascade through the
> system, resulting in incorrect position sizing or risk calculations,
> orders placed at wrong prices, backtests producing misleading results,
> silent financial losses."

## Módulos nuevos

### [[Modules/Component_State_Machine]] (Sprint 6)
Lifecycle explícito:
```
PRE_INITIALIZED → READY → STARTING → RUNNING
                       ↓
            DEGRADED (recoverable)
                       ↓
            FAULTED (no recoverable)
                       ↓
            STOPPING → STOPPED
```

Cada transición se loguea + emite `COMPONENT_STATE_X` al audit ledger.

### [[Modules/Data_Validator]] (Sprint 6)
- `validate_price()`: rechaza NaN/Inf/negativos/non-numbers
- `validate_quantity()`: idem para cantidades
- `validate_ohlcv_row()`: valida vela (H>=L, V>=0)
- `validate_dataframe()`: chequea todas las columnas + high<low global

## Integración

`MarketAnalystAgent` ahora hereda de `Component`. Cada vela que
descarga se valida antes de calcular indicadores. Si yfinance devuelve
NaN/Inf, el componente se degrada a `DEGRADED` (o `FAULTED` si
todos los feeds fallan).

## Test verificado

```
=== TEST 1: validate_price rejects NaN/Inf/negativo ===
  ✅ rejected: price=nan: NaN
  ✅ rejected: price=inf: Infinity
  ✅ rejected: price=-0.5: negative (-0.5)
  ✅ rejected: price=abc: not a number ('abc')

=== TEST 2: validate_dataframe rejects corrupt OHLCV ===
  ✅ rejected: column Open: 1 NaN values
  ✅ rejected: high<low en 1 vela(s)
  ✅ aceptó DataFrame válido

=== TEST 3: Component State Machine ===
  Estado inicial: PRE_INITIALIZED
  Después de ready(): READY
  Después de start(): RUNNING
  Después de degrade(): DEGRADED
  Después de recover(): RUNNING
  Después de fault(): FAULTED

✅ Sprint 6: validator + state machine funcionan
```

## Por qué importa para Guaritradbot

Un bot que opera con $100 reales puede ser **destruido por UN solo
tick** con precio 0 o NaN (qty × NaN = NaN = posición corrupta).
Antes esto pasaba silenciosamente. Ahora crash explícito, no pérdidas
silenciosas.

## Commit

`b3904ad` — feat(sprint 6): Component State Machine + fail-fast data integrity

## Ver también

- [[Sprints_Index]]
- [[Architecture]]
