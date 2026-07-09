# Inspiration 1 — Vibe-Trading (HKUDS)

**Repo**: https://github.com/HKUDS/Vibe-Trading

**Usado en**: Sprint 1 (Safety Layer), patrones generales

## Lo que nos llevamos

### 1. Mandate Gate (citado verbatim)

> "user-committed mandate (symbol universe / order size / exposure /
> leverage / daily cap), filesystem kill switch, fail-closed pre-trade
> gate, and a full audit ledger. The AI can't quietly go rogue with
> your money. Experimental / use at your own risk."

Implementado en [[Modules/MandateGate]] con 4 chequeos:
- universe (set de símbolos permitidos)
- max_position_usd
- max_daily_loss_usd (rolling 24h)
- max_total_exposure_usd

Activado opcionalmente con `mandate.enabled: true` en `config.yaml`.

### 2. Filesystem kill switch

> "filesystem-level instant kill switch, preemptive flatten, mandate
> auto-expiry, a full audit ledger, and a persistent autonomous
> runner."

Implementado en [[Modules/KillSwitch]] — archivo `/tmp/GUARITRADBOT_KILL`,
doble check en main startup + execution_node.

### 3. Audit ledger append-only

> "Session message writes now `flush + fsync` each append so expensive
> AI responses survive a mid-write crash"

Implementado en [[Modules/AuditLedger]] — JSONL append con fsync.

## Lo que NO tomamos

- **Connector-first broker architecture** (10 brokers). Solo Binance.
- **422+ alphas en alpha zoo**. Overkill para 5 activos.
- **IM channels** (16 adapters). Telegram actual es suficiente.
- **Swarm workers** (LangChain/LangGraph). Demasiada complejidad.

## Lo único que cambió vs el original

Vibe-Trading está pensado como **herramienta interactiva** (CLI /
Web UI), con un humano en el loop. Guaritradbot apunta a **autonomía
24/7 en un VPS**. Por eso nuestro [[Modules/KillSwitch]] es la única
interfaz humana — un archivo en disco.

## Ver también

- [[Inspirations]]
- [[Sprints/Sprint_1_Safety_Layer]]
