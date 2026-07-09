# B002 — env keys mismatch

**Severidad**: 🔴 crítico (las API keys nunca se cargaban)

## Síntomas

Bot reportaba balance `0.00` o caía al fallback `testnet_sim` aunque
las variables de entorno estuviesen configuradas.

## Causa

`src/execution/broker.py`:
```python
api_key = os.getenv("EXCHANGE_API_KEY")
secret = os.getenv("EXCHANGE_SECRET_KEY")
```

`.env.example`:
```
BINANCE_API_KEY="tu_api_key_aqui"
BINANCE_API_SECRET="tu_secret_aqui"
```

Mismatch — el broker leía nombres `EXCHANGE_*` que **nunca existían**.

## Fix (Sprint 0, commit `10d144c`)

```python
# broker.py
api_key = os.getenv("BINANCE_API_KEY")
secret = os.getenv("BINANCE_API_SECRET")
```

## Lección

Cuando un módulo tiene muchos `.env.example` heredados, usar grep
para verificar que cada `os.getenv("X")` tenga su `X=...` declarado.

## Ver también

- [[Bugs_Index]]
- [[Deployment]] — cómo configurar `.env` real
