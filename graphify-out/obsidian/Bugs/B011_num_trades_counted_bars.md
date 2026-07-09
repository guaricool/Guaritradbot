# B011 — num_trades eran barras, no trades

**Severidad**: 🟠 medio (métrica engañosa)

## Síntomas

Bot reportaba "729 trades en 2 años de BTC-USD daily". Eso es
**imposible** — un trader real no opera a 1 trade por día. Era
un error de unidades.

## Causa

```python
num_trades = int((returns != 0).sum())  # barras con retorno ≠ 0
```

En velas de 1h, cualquier posición abierta cambia la barra → cuenta.

## Fix (Sprint 4, commit `7a0cd26`)

Trade detection real (ver B010) →
`num_trades = len(trades)` después de detect_trades.

## Ejemplo

BTC-USD 2 años daily con RSI(30/70):
```
Antes:  num_trades = 728 (casi cada barra)
Ahora:  num_trades = 1 (BTC no cruzó RSI extremos en 2 años)
```

El número real es mucho más bajo. Eso es **información valiosa**: la
estrategia RSI(30/70) NO produce trades frecuentes en BTC.

## Ver también

- [[Sprints/Sprint_4_Backtester_Fix]]
- [[Bugs/B010_win_rate_misleading]]
- [[Bugs_Index]]
