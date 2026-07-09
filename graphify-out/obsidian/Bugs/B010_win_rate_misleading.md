# B010 — win_rate calculado sobre barras

**Severidad**: 🟠 medio (métrica engañosa)

## Síntomas

"Win rate: 100%" cuando la estrategia no había abierto ni un trade.
O "Win rate: 70%" cuando en realidad el 70% de los DÍAS fueron
alcistas (no de los trades).

## Causa

```python
winning_days = (returns > 0).sum()
total_days = (returns != 0).sum()
win_rate = winning_days / total_days  # díasalcistas / díastotales
```

Confundía "días con retorno positivo" con "trades ganadores".

## Fix (Sprint 4, commit `7a0cd26`)

Trade detection real:
```python
def _detect_trades(self, signals, prices):
    trades = []
    in_trade = False
    entry_idx = ...
    for i, (idx, sig) in enumerate(signals.items()):
        if not in_trade and sig != 0:
            in_trade = True
            entry_price = ...
            direction = int(sig)
        elif in_trade and (sig == 0 or i == len(signals) - 1):
            exit_price = ...
            ret = (exit_price - entry_price) / entry_price * direction
            trades.append({entry, exit, return_pct, ...})
            in_trade = False
    return trades
```

Después:
```python
win_rate = len([t for t in trades if t["return_pct"] > 0]) / len(trades)
```

Ahora `win_rate` significa lo que dice.

## Ejemplo BTC-USD 2 años

```
Antes: win_rate = 50% (50% de días alcistas)
Ahora: win_rate = 1.0 (1 trade ganador de 1 trade total, con TF algo mal)
```

## Ver también

- [[Sprints/Sprint_4_Backtester_Fix]]
- [[Bugs_Index]]
- [[Sprints/Sprint_0_Critical_Bug_Fixes]] — relaciones con B009
