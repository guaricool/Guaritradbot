# B019 — Punished for Trying (Daily Loss Theoretical)

**Severidad**: 🔴 Crítico
**Sprint**: 18
**Reportado por**: Audit Team (Finance & Risk)
**Componente**: `MandateGate._daily_loss_usd()` en `src/safety/mandate_gate.py`

## Síntomas

- Bot abre 5 trades en un día, todos WINNERS (cada uno +$5 profit).
- Después del 5to trade, bot entra en kill switch de 24h con `daily_loss_cap:$5.00>$5.00`.
- LITERALMENTE castiga al bot por hacer trades buenos.

## Causa raíz

El método original sumaba `risk_usd` (teórico) de eventos `TRADE_APPROVED` en vez de P&L realizado de eventos `TRADE_CLOSED`:

```python
def _daily_loss_usd(self, now_ts=None) -> float:
    rows = self.audit.read_since(cutoff)
    return sum(
        float(r.get("risk_usd", 0.0))
        for r in rows
        if r.get("event_type") == "TRADE_APPROVED"   # ← BUG: teórico, no realizado
    )
```

`risk_usd` es el tamaño del stop loss (cuánto estás ARRIESGANDO), NO cuánto perdiste realmente. Es la misma cantidad que el trade sea winner o loser.

Trace:
1. Trade 1 LONG $1 risk → TRADE_APPROVED → daily_loss += $1
2. Trade 1 cierra TP +$5 → TRADE_CLOSED (no se cuenta aquí)
3. Trade 2 LONG $1 risk → TRADE_APPROVED → daily_loss += $1
4. ...
5. Trade 5 → daily_loss = $5 = `max_daily_loss_usd`
6. Trade 6 → Mandate Gate: `$5 + $1 > $5` → BLOQUEADO 24h

Aun cuando trades 1-5 fueron +$5 cada uno = +$25 net profit. Ridículo.

## Fix (Sprint 18)

Opción A (preferida): usar `PositionRepository` y filtrar solo pérdidas realizadas en las últimas 24h:

```python
def _daily_loss_usd(self, now_ts=None) -> float:
    now = now_ts or time.time()
    cutoff = now - 24 * 3600

    if self.position_repo is not None:
        loss = 0.0
        for p in self.position_repo.all():
            if (
                p.is_open is False
                and p.closed_ts is not None
                and p.closed_ts >= cutoff
                and p.realized_pnl is not None
                and p.realized_pnl < 0     # ← solo pérdidas
            ):
                loss += abs(p.realized_pnl)
        return loss
    # ... audit fallback (TRADE_CLOSED.realized_pnl_usd < 0)
```

Opción B (fallback): leer audit ledger, filtrar `TRADE_CLOSED` con `realized_pnl_usd < 0`.

## Tests que cubren este bug

- `tests/test_mandate_gate_sprint18.py::PunishedForTryingTest::test_daily_loss_zero_after_winning_trades`
- `tests/test_mandate_gate_sprint18.py::PunishedForTryingTest::test_daily_loss_sums_realized_losses`
- `tests/test_mandate_gate_sprint18.py::PunishedForTryingTest::test_old_behavior_would_have_triggered_kill_switch` (regresión)
- `tests/test_mandate_gate_sprint18.py::MandateIntegrationTest::test_winning_trades_pass_daily_loss_check`

## Lección de diseño

**Nunca** uses el `risk_usd` (tamaño del stop) como sustituto del realized P&L. Son cosas fundamentalmente diferentes:

| Concepto | Significado |
|---|---|
| `risk_usd` | Cuánto estás ARRIESGANDO si el trade toca el SL (teórico, antes del trade) |
| `realized_pnl` | Cuánto GANASTE o PERDISTE realmente cuando el trade cerró (después del trade) |

Para kill switches / daily loss limits, solo el realized P&L tiene sentido. Si quieres protección pre-trade, usa `risk_usd` PERO como un budget pre-trade (no como un sumador post-trade).