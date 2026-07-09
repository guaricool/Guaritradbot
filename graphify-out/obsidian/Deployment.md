---
title: "Deployment Guide"
tags: [deployment, coolify, ssh, telegram, binance, secret-management]
sprint_origin: Sprint_7, Roadmap
---

# Deployment Guide

## Estado actual
- Repo local: `C:\Users\cpier\OneDrive\Desktop\Proyectos\Trading\`
- 8 commits locales ahead of origin (no push aún — esperando OK de Carlos)
- Coolify app configurada pero `exited:unhealthy` (sin env vars, sin código deployado)
- Modo actual: `testnet=true`, `mandate.enabled=false`

## Pre-requisitos antes de ir live

### 1. Secretos requeridos
| Variable            | Dónde se obtiene                                | Notas                                                                 |
| ------------------- | ----------------------------------------------- | --------------------------------------------------------------------- |
| `BINANCE_API_KEY`   | Binance Testnet → API Management                 | **Empezar con TESTNET**, nunca mainnet primero                       |
| `BINANCE_SECRET`    | Binance Testnet                                 | Solo lectura + trading SPOT, NO enable withdrawal                     |
| `TELEGRAM_BOT_TOKEN`| @BotFather                                      | Crear bot nuevo solo para Guaritradbot                                |
| `TELEGRAM_CHAT_ID`  | GetUpdates con `@userinfobot`                   | Whitelist solo chat personal de Carlos                                |
| `MANDATE_ENABLED`   | config.yaml                                     | Mantener `false` hasta validar 30 días de paper                        |

### 2. Modos de ejecución
- `--mode once`: corre 1 ciclo y termina. Para validar.
- `--mode loop`: corre indefinidamente. Para producción.
- `--mode backtest`: corre el [[Component_State_Machine|backtester]] vectorizado. **Importante**: NO publica eventos al bus live.

### 3. Modos de aprobación
- `human_in_the_loop: true`: cada trade propuesto va a Telegram con botones Aprobar/Rechazar. **Recomendado para live.**
- `human_in_the_loop: false`: ejecuciones automáticas. **Solo después de 30+ días de paper consistentemente positivo.**

## Coolify setup detallado

### Agregar env vars (dentro de Coolify, NO por API directo)
```
1. https://coolify.13.140.181.29.nip.io
2. Apps → guaritrading → Environment Variables
3. Copiar las de .env.example y rellenar
4. Save
```

### Trigger deploy vía API
```bash
docker exec -it coolify sh
# Dentro del container:
curl -sS -X POST 'http://localhost:8000/api/v1/deploy' \
  -H "Authorization: Bearer ${COOLIFY_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"uuid":"wyn2ah6rflg6ufwzpvzk436f","force":true}'
```

### Poll status (CRÍTICO — no asumir éxito)
```bash
# Ver MEMORY.md "Deploy status: reportar FAIL HISTORY"
docker exec coolify curl -sS \
  -H "Authorization: Bearer ${COOLIFY_API_TOKEN}" \
  'http://localhost:8000/api/v1/applications/wyn2ah6rflg6ufwzpvzk436f/deployments' \
  | jq '.[] | {commit: .commit, status: .status, created_at: .created_at}'
```

> **Regla absoluta de Carlos**: reportar FAIL HISTORY, no solo el último estado. Si 6 deploys fallaron y el 7° pasó, mencionar los 6 anteriores.

## Cambiar entre Testnet y Mainnet

```bash
# Testnet (default actual)
BINANCE_API_KEY=<testnet_key>

# Mainnet (solo cuando paper sea estable)
BINANCE_API_KEY=<mainnet_key>
BINANCE_BASE_URL=https://api.binance.com  # actualizar en config.yaml
```

## Monitoreo post-deploy

### Logs
```bash
docker logs -f <container> --since 30m | grep -E "(ERROR|TRADE_)"
```

### Healthcheck custom
- Endpoint `/health` no implementado aún. **Sprint 8 sugerido.**
- Mientras tanto, `docker ps` muestra `STATUS=Up X minutes` = OK.

### Alertas
- Telegram bot debe alertar si:
  - `audit_ledger.jsonl` no se escribió en 1 hora (bot colgado).
  - Kill switch activado.
  - `daily_loss_usd > 80% of limit` → warning temprano.
  - 3 rechazados del MandateGate consecutivos → revisar config.

## Rollback plan
1. `git revert <último_commit_prometedor>` local
2. Push al origin
3. Coolify auto-redeploya
4. O手动: `curl POST /api/v1/deploy con force=true` con el commit viejo como `HEAD`

## Lo que NO se debe hacer
- ❌ Deployar con `mandate.enabled=true` antes de 30 días de paper
- ❌ Cambiar testnet→mainnet sin que Carlos firme OK explícito
- ❌ Activar `--mode loop` directo desde testnet sin verificar `audit_ledger` después del primer ciclo
- ❌ Correr el bot en VPS sin antes testear localmente con `--mode once`
- ❌ Confiar en el exit code del deploy. **Siempre** leer los logs.

## Próximos pasos sugeridos (Sprint 8+)
1. `/health` endpoint
2. Position-level FSM (Nautilus-style)
3. DataClient interface formal
4. Telegram approval botones inline (sí/no inline_keyboard)
5. Daily report a Carlos con PnL, trades, exposure

Ver: [[Sprint_1_Safety_Layer]], [[ExecutionNode]], [[KillSwitch]], [[MandateGate]]
