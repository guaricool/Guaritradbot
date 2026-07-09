# B008 — tf_map "4h" silenciaba resample

**Severidad**: 🟠 medio (las estrategias "4h" recibían datos de 1h)

## Síntomas

GLD y USO se configuraron con `tf_map["4h"]`. Las estrategias
esperaban velas diarias (cada 4 horas = 6 velas/día). Pero el bot les
estaba dando velas de 1h, idénticas a las que ya recibían para el tf
"1h".

## Causa

```python
tf_map = {
    "15m": "15m",
    "1h":  "60m",
    "4h":  "1h"  # <-- MENTIRA: yfinance no devuelve velas de 4h
}
```

`yfinance` no soporta velas de 4h directamente. La versión vieja
silenciosamente usaba `1h` y lo llamaba `4h`.

## Fix (Sprint 0, commit `10d144c`)

Nueva función `_resample_ohlcv(df, rule)`:
```python
agg = {"Open": "first", "High": "max", "Low": "min",
       "Close": "last", "Volume": "sum"}
return df.resample(rule).agg(agg).dropna(subset=["Close"])
```

Y la lookup table ahora avisa "voy a resamplear":
```python
tf_map = {
    "15m": ("15m", None),
    "1h":  ("60m", None),
    "4h":  ("60m", "4h"),  # descarga 60m, resample a 4h
}
```

## Verificación

```
✅ GLD@4h: 120 velas  (vs antes: 60m que se hacía pasar por 4h)
```

## Lección

Si un proveedor no soporta el timeframe que necesitás, **no finjas**.
Resamplea explícitamente o fallá ruidoso.

## Ver también

- [[Modules/MarketAnalystAgent]] — usa el mapeo + resample
- [[Bugs_Index]]
