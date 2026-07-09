"""
Sprint 0+1 — RiskManagerAgent.

Sprint 0 fixes:
1. Stop loss ahora basado en ATR (2x ATR, configurable) en vez de $5
   hardcoded.
2. Position sizing correcto: qty = risk_amount / (entry - stop) con
   `risk_per_trade_pct` del config.
3. Account balance fallback inteligente.

Sprint 1 añade:
4. Integración con MandateGate (si está habilitado) — el gate valida
   universo, position size, daily loss rolling 24h, y exposure total
   ANTES de aprobar. Trades que fallen el gate se mueven a `rejected`.
5. Audit ledger: cada aprobación y cada rechazo se registra para
   forensics post-mortem.
"""
import os
from typing import Dict, Any, Optional


class RiskManagerAgent:
    def __init__(
        self,
        broker_client=None,
        risk_per_trade_pct: float = 1.0,
        max_capital_per_trade_pct: float = 10.0,
        atr_stop_multiplier: float = 2.0,
        min_order_usd: float = 10.0,
        event_bus=None,
        mandate_gate=None,
        audit=None,
    ):
        self.broker = broker_client
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_capital_per_trade_pct = max_capital_per_trade_pct
        self.atr_stop_multiplier = atr_stop_multiplier
        self.min_order_usd = min_order_usd
        self.event_bus = event_bus
        self.mandate = mandate_gate
        self.audit = audit

    def get_account_balance(self) -> tuple:
        if self.broker is None:
            return (100.0, "no_broker_sim")
        try:
            bal = self.broker.get_usdt_balance()
            source = self.broker.exchange.options.get("sandboxMode", False)
            return (bal, "testnet_sim" if source else "live")
        except Exception as e:
            if os.getenv("GUARICO_ALLOW_SIMULATED_BALANCE", "1") == "1":
                print(f"[RiskManagerAgent] ⚠️ Balance indisponible ({e}). SIMULATED fallback $100.")
                return (100.0, "testnet_sim")
            raise RuntimeError("Balance no disponible y simulación deshabilitada") from e

    def validate_and_size(self, inputs: dict, state: dict) -> Dict[str, Any]:
        hypotheses: list = state.get("generate_hypotheses", {}).get("hypotheses", [])
        account_balance, balance_source = self.get_account_balance()

        print(
            f"[RiskManagerAgent] {len(hypotheses)} hipótesis | "
            f"Balance ${account_balance:.2f} ({balance_source}) | "
            f"Riesgo/trade: {self.risk_per_trade_pct}% | "
            f"Stop: {self.atr_stop_multiplier}x ATR"
        )

        approved = []
        rejected = []
        for h in hypotheses:
            entry_price = float(h.get("price", 0))
            atr = float(h.get("atr_at_signal", 0))
            direction = h.get("direction", "long")

            if entry_price <= 0:
                rejected.append({"hypothesis": h, "reason": "invalid_entry_price"})
                if self.audit:
                    self.audit.append("TRADE_REJECTED", {"asset": h.get("asset"), "reason": "invalid_entry_price"})
                continue

            # ATR-based stop
            stop_distance = max(atr * self.atr_stop_multiplier, entry_price * 0.005)
            if direction == "long":
                stop_loss = entry_price - stop_distance
            else:
                stop_loss = entry_price + stop_distance

            # Position sizing
            risk_amount_usd = account_balance * (self.risk_per_trade_pct / 100.0)
            quantity = risk_amount_usd / stop_distance

            # Cap por max_capital_per_trade_pct
            notional = quantity * entry_price
            max_notional = account_balance * (self.max_capital_per_trade_pct / 100.0)
            if notional > max_notional:
                quantity = max_notional / entry_price
                notional = quantity * entry_price
                risk_amount_usd_eff = quantity * stop_distance
            else:
                risk_amount_usd_eff = risk_amount_usd

            if notional < self.min_order_usd:
                rejected.append(
                    {"hypothesis": h, "reason": f"notional_${notional:.2f} < ${self.min_order_usd}"}
                )
                if self.audit:
                    self.audit.append("TRADE_REJECTED", {"asset": h.get("asset"), "reason": f"min_order_${notional:.2f}"})
                print(f"  ❌ {h['asset']:8} {direction:5} — ${notional:.2f} < min ${self.min_order_usd}")
                continue

            trade = {
                "asset": h["asset"],
                "strategy": h["strategy"],
                "direction": direction,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": entry_price + (stop_distance * 2.0) * (1 if direction == "long" else -1),
                "position_size": round(float(quantity), 8),
                "notional_usd": round(float(notional), 2),
                "risk_usd": round(float(risk_amount_usd_eff), 2),
                "atr_at_signal": atr,
                "balance_source": balance_source,
            }

            # Sprint 1: Mandate gate
            if self.mandate is not None:
                verdict = self.mandate.validate(trade)
                if not verdict.ok:
                    rejected.append({"trade": trade, "reason": verdict.reason})
                    if self.audit:
                        self.audit.append(
                            "MANDATE_BLOCKED",
                            {"asset": trade["asset"], "reason": verdict.reason, "notional": trade["notional_usd"]},
                        )
                    print(f"  🛡️  {trade['asset']:8} {direction:5} — blocked by mandate: {verdict.reason}")
                    continue
                if self.audit:
                    self.audit.append(
                        "MANDATE_OK",
                        {"asset": trade["asset"], "notional": trade["notional_usd"], "risk": trade["risk_usd"]},
                    )

            approved.append(trade)
            if self.audit:
                self.audit.append(
                    "TRADE_APPROVED",
                    {
                        "asset": trade["asset"],
                        "direction": direction,
                        "entry_price": entry_price,
                        "stop_loss": stop_loss,
                        "qty": trade["position_size"],
                        "notional_usd": trade["notional_usd"],
                        "risk_usd": trade["risk_usd"],
                    },
                )
            print(
                f"  ✅ {trade['asset']:8} {direction:5} @ ${entry_price:>9.2f} | "
                f"qty={quantity:>10.6f} | "
                f"stop=${stop_loss:>9.2f} | "
                f"risk=${risk_amount_usd_eff:.2f} | "
                f"notional=${notional:.2f}"
            )

        return {
            "approved_trades": approved,
            "rejected_trades": rejected,
            "account_balance": account_balance,
            "balance_source": balance_source,
        }
