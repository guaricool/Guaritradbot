# B014 — bool(DataFrame) ambiguo

**Severidad**: 🟠 medio (crash en StrategyAgent)

## Síntomas

`python main.py --once` explotaba con:

```
ValueError: The truth value of a DataFrame is ambiguous.
Use a.empty, a.bool(), a.item(), a.any() or a.all().
```

## Causa

`src/agents/strategy_agent.py`:
```python
df = market_data.get(asset, {}).get("4h") or market_data.get(asset, {}).get("1h")
```

Cuando `df` es un DataFrame, Python evalúa `bool(df)` que NO está
definido (es ambiguo — tiene muchos bools). Pandas levanta
`ValueError`.

## Fix (Sprint 0, commit `10d144c`)

```python
df = market_data.get(asset, {}).get("4h")
if df is None or len(df) == 0:
    df = market_data.get(asset, {}).get("1h")
if df is None or len(df) == 0:
    continue
```

`len(df) == 0` es explícito y NO ambiguo. `or` con DataFrame es el
problema real.

## Lección

NUNCA uses `or` con DataFrames. El patrón es:

```python
if df is None:
    df = alternative
if len(df) == 0:
    df = alternative
```

O usar `combine_first()`:
```python
df = df_4h.combine_first(df_1h)
```

## Ver también

- [[Modules/StrategyAgent]]
- [[Sprints/Sprint_0_Critical_Bug_Fixes]]
- [[Bugs_Index]]
