---
title: "Inspiration #4 — freqtrade (risk management + persistence)"
tags: [inspiration, inspiration-4, risk-management, persistence, freqtrade]
source: external_repos/freqtrade/
sprint_origin: Sprint_1, Sprint_2
---

# Inspiration #4 — freqtrade

**Origen**: `external_repos/freqtrade/` (clonado el 2026-07-08).

## Qué es
Bot open-source más maduro del ecosistema Python (15k+ stars). Tiene TODO: exchanges múltiples, backtesting, hyperopt, FreqAI (ML), Telegram bot, dry-run. Es el estándar de facto.

## Qué aportó a Guaritradbot

### Risk management (Sprint 1)
- `stake_amount = capital * stake_pct` → traducido a `qty = capital * risk_per_trade_pct / stop_distance`.
- `max_open_trades` global limit → MandelGate.
- `stop_loss` desde config + `roi` (return on investment) take profit → ATR-based en Guaritradbot.
- **`stake_pct = 1%` por defecto** (no 10% como sugería el PDF). Más conservador.

### Persistencia (Sprint 2)
- `trades.json` como log append-only → inspirado en `[[AuditLedger]]`.
- **Conversión trade DB → orden DB**: cada orden ejecutada se asocia a un `trade_id` persistente. En Guaritradbot → `position_id` (UUID) en `[[Position_Repository]]`.
- **Realizar ganancia/pérdida solo en venta**, no en compra. En Guaritradbot → `close_position()` calcula PnL con ATR + precio de cierre.

### Walk-forward + hyperopt (Sprint 4, 5)
- `freqtrade hyperopt` usa `epoch`, `space` (buy/sell/roi/stoploss), `loss` function. Inspiración directa para `HyperoptManager` en `src/optimization/`.
- **Detección de overfit** con IS/OOS split → `walk_forward_validate` en [[Component_State_Machine|backtester]].

## Por qué NO se copió literal
- freqtrade tiene **10 años de deuda técnica** + plugins legacy.
- Configurar freqtrade productivamente toma semanas.
- Está escrito para ejecutarse como **servidor** (RabbitMQ + worker), no como una lib.

## Lo que tomó Guaritradbot
- ✅ `risk_per_trade_pct = 1%` default
- ✅ `max_open_trades` global limit
- ✅ Persistencia de posiciones con `position_id` UUID
- ✅ PnL al cerrar, no en cada tick
- ✅ Walk-forward validation
- ❌ Multi-exchange (Binance solo)
- ❌ FreqAI / ML
- ❌ Dry-run mode humano (usar `testnet` Binance en su lugar)

## Patrones reusables (cross-project)
> **Posiciones deben tener un ID persistente antes de cualquier estado mutable.** Sin eso, los PnL son incontables. freqtrade lo aprendió en 2018; Guaritradbot no debería repetir el error.

Ver: [[Sprint_1_Safety_Layer]], [[Sprint_2_Position_Tracking]], [[RiskManagerAgent]], [[Position_Repository]]
