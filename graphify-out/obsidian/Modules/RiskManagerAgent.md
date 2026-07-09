# RiskManagerAgent

`src/agents/risk_agent.py`

## Responsabilidad

**Validar + dimensionar** cada hipótesis antes de aprobar.

## Lógica de sizing (Sprint 0)

```
stop_distance = max(atr × k_stop, price × 0.005)  # k_stop = 2.0 default
if direction == long: stop_loss = entry - stop_distance
if direction == short: stop_loss = entry + stop_distance

risk_amount_usd = account_balance × (risk_per_trade_pct / 100)
quantity = risk_amount_usd / stop_distance
notional = quantity × entry_price

# Cap por max_capital_per_trade_pct
if notional > max_notional:
    quantity = max_notional / entry_price
```

Reglas:
- `risk_per_trade_pct = 1.0` (1% del balance por trade)
- `atr_stop_multiplier = 2.0` (2x ATR como distancia)
- `max_capital_per_trade_pct = 10` (cap absoluto por trade)
- `min_order_usd = 10` (Binance spot mínimo)

**Cerrado B006**: stop_loss ya no es `5.0` hardcoded sino ATR-based.

## Mandate Gate integration (Sprint 1)

Si el [[Modules/MandateGate]] está activo:

```python
verdict = self.mandate.validate(trade)
if not verdict.ok:
    rejected.append(...)
    audit.append("MANDATE_BLOCKED", ...)
    continue
```

Validaciones: universe, max_position_usd, daily_loss_usd rolling 24h,
total_exposure_usd.

## Position tracking (Sprint 2)

Tras aprobar una trade:

```python
pos = Position(
    asset=trade["asset"],
    direction=direction,
    entry_price=entry_price,
    stop_loss=stop_loss,
    take_profit=take_profit,  # ATR-based, 4x ATR → 1:2 R:R
    qty=quantity,
    risk_usd=risk_amount_usd_eff,
    entry_ts=time.time(),
    strategy=hypothesis["strategy"],
)
self.position_repo.add_open(pos)
```

Take profit default = `entry ± (atr × 4)` → R:R = 1:2.

## max_open_trades (Sprint 2)

Lee `self.position_repo.count_open()` y rechaza nuevas si ya hay
`max_open_trades` slots llenos (default 5).

## Consume debate (Sprint 3)

```python
if "debate_hypotheses" in state:
    hypotheses = state["debate_hypotheses"]["approved_hypotheses"]
else:
    hypotheses = state["generate_hypotheses"]["hypotheses"]  # retrocompat
```

## Output del step

```python
{
    "approved_trades": [...],
    "rejected_trades": [...],
    "account_balance": float,
    "balance_source": str,  # "live" | "testnet_sim" | "no_broker_sim"
    "open_positions_after": int,
}
```

## Conecta con

- [[Modules/MarketAnalystAgent]] — ATR para sizing
- [[Modules/StrategyAgent]] / [[Modules/DebateAgent]] — consume hipótesis
- [[Modules/MandateGate]] — bloquea si no cumple
- [[Modules/AuditLedger]] — TRADE_APPROVED / TRADE_REJECTED / MANDATE_*
- [[Modules/Position_Repository]] — persiste nuevas posiciones
- [[Sprints/Sprint_0_Critical_Bug_Fixes]] — fix stop hardcoded
- [[Sprints/Sprint_1_Safety_Layer]] — Mandate integration
- [[Sprints/Sprint_2_Position_Tracking]] — Position persistence
- [[Sprints/Sprint_3_Multi_Agent_Debate]] — consume debate
