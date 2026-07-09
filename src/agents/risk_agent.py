"""
Sprint 0+1+2 — RiskManagerAgent.

Sprint 0 fixes: ATR-based stops, qty = risk/distance, min order check.
Sprint 1: Mandate Gate + audit ledger integration.
Sprint 2: Take profit ATR-based, max_open_trades respetado, persiste
posición abierta en el PositionRepository.
"""
import os
from typing import Dict, Any

from src.data_store.positions import Position


class RiskManagerAgent:
    def __init__(
        self,
        broker_client=None,
        risk_per_trade_pct: float = 1.0,
        max_capital_per_trade_pct: float = 10.0,
        atr_stop_multiplier: float = 2.0,
        atr_take_profit_multiplier: float = 4.0,
        risk_reward_ratio: float = 2.0,
        max_open_trades: int = 5,
        min_order_usd: float = 10.0,
        event_bus=None,
        mandate_gate=None,
        audit=None,
        position_repo=None,
    ):
        self.broker = broker_client
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_capital_per_trade_pct = max_capital_per_trade_pct
        self.atr_stop_multiplier = atr_stop_multiplier
        self.atr_take_profit_multiplier = atr_take_profit_multiplier
        self.risk_reward_ratio = risk_reward_ratio
        self.max_open_trades = max_open_trades
        self.min_order_usd = min_order_usd
        self.event_bus = event_bus
        self.mandate = mandate_gate
        self.audit = audit
        self.position_repo = position_repo

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
        # Sprint 3: si DebateAgent corrió, usa SOLO las hipótesis que
        # pasaron el debate. Si no hay debate, fallback al total
        # (retrocompatible con versiones sin Sprint 3).
        if "debate_hypotheses" in state:
            debate_result = state["debate_hypotheses"]
            approved = debate_result.get("approved_hypotheses", [])
            all_hyps = debate_result.get("hypotheses", [])
            # Sprint 11 fix: solo usar approved si el debate realmente
            # ejecutó (no [] que es fallback falsy). Si approved es []
            # Y hay all_hyps, fue "ninguna aprobada" → usar [] también,
            # no todas (antes hacía fallback a todas).
            if approved:
                hypotheses = approved
            elif all_hyps and "verdicts" in debate_result and debate_result["verdicts"]:
                # Debate corrió y rechazó todas → respeta la decisión
                hypotheses = []
            else:
                # No hay debate (caso legacy) → usa todas
                hypotheses = all_hyps
            print(f"[RiskManagerAgent] usando {len(hypotheses)} hipótesis post-debate")
        else:
            hypotheses = state.get("generate_hypotheses", {}).get("hypotheses", [])
        account_balance, balance_source = self.get_account_balance()

        # Sprint 2: cuántas posiciones abiertas ya tenemos
        open_count = self.position_repo.count_open() if self.position_repo else 0
        slots_left = max(0, self.max_open_trades - open_count)
        print(
            f"[RiskManagerAgent] {len(hypotheses)} hipótesis | "
            f"Balance ${account_balance:.2f} ({balance_source}) | "
            f"Posiciones abiertas {open_count}/{self.max_open_trades} | "
            f"Riesgo {self.risk_per_trade_pct}% | R:R {self.risk_reward_ratio}:1"
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

            # --- Stop y Take Profit (ATR-based, Sprint 0+2) ---
            stop_distance = max(atr * self.atr_stop_multiplier, entry_price * 0.005)
            tp_distance = atr * self.atr_take_profit_multiplier
            if direction == "long":
                stop_loss = entry_price - stop_distance
                take_profit = entry_price + tp_distance
            else:
                stop_loss = entry_price + stop_distance
                take_profit = entry_price - tp_distance

            # --- Position sizing: qty = risk / distance ---
            risk_amount_usd = account_balance * (self.risk_per_trade_pct / 100.0)
            quantity = risk_amount_usd / stop_distance

            # --- Cap por max_capital_per_trade_pct ---
            notional = quantity * entry_price
            max_notional = account_balance * (self.max_capital_per_trade_pct / 100.0)

            # --- Auto-adjustment for min_order_usd consistency (Sprint 12) ---
            # If the configured max_capital_per_trade_pct results in a notional
            # BELOW the exchange's min_order, the system is internally
            # inconsistent: it says "you can trade up to X% of balance" but
            # X% of balance is less than what the exchange requires.
            #
            # Rather than rejecting the trade (defeats the purpose of being
            # autonomous) or silently ignoring the cap (defeats the purpose
            # of having one), bump the effective cap up to the min_order
            # amount and log it clearly. This way:
            #   - The trade happens (no missed opportunities)
            #   - The user sees the conflict in the audit log
            #   - The cap is honored on EVERY OTHER trade (only bumped here)
            if max_notional < self.min_order_usd:
                # Cap from config is too small. Bump up to min_order.
                adjusted_cap = self.min_order_usd
                adjusted_cap_pct = (adjusted_cap / account_balance) * 100.0 if account_balance > 0 else 0
                quantity = adjusted_cap / entry_price
                notional = quantity * entry_price
                if self.audit:
                    self.audit.append("CAP_AUTO_ADJUSTED", {
                        "asset": h.get("asset"),
                        "reason": "max_cap_below_min_order",
                        "config_pct": self.max_capital_per_trade_pct,
                        "config_notional": max_notional,
                        "min_order_usd": self.min_order_usd,
                        "adjusted_to_pct": round(adjusted_cap_pct, 2),
                        "adjusted_to_notional": round(notional, 2),
                    })
                print(f"  ⚠️ {h['asset']:8} {direction:5} — "
                      f"config cap=${max_notional:.2f} ({self.max_capital_per_trade_pct}% of ${account_balance:.2f}) "
                      f"< min ${self.min_order_usd:.2f}. "
                      f"Bumping to ${notional:.2f} ({adjusted_cap_pct:.1f}% effective). "
                      f"⚠️ FIX your config.yaml: increase max_capital_per_trade_pct or lower min_order_usd.")
            elif notional > max_notional:
                # Normal cap hit
                quantity = max_notional / entry_price
                notional = quantity * entry_price
                risk_amount_usd_eff = quantity * stop_distance
            else:
                risk_amount_usd_eff = risk_amount_usd

            # --- Min order check (post-adjustment) ---
            if notional < self.min_order_usd:
                # Shouldn't happen after the auto-adjust above, but defensive
                rejected.append(
                    {"hypothesis": h, "reason": f"notional_${notional:.2f} < ${self.min_order_usd}"}
                )
                if self.audit:
                    self.audit.append("TRADE_REJECTED", {"asset": h.get("asset"), "reason": f"min_order_${notional:.2f}"})
                print(f"  ❌ {h['asset']:8} {direction:5} — ${notional:.2f} < min ${self.min_order_usd}")
                continue

            # --- Balance check ---
            # If the account balance is so small that even min_order doesn't
            # fit, skip this trade cleanly. Log a clear reason.
            if account_balance < self.min_order_usd:
                rejected.append({
                    "hypothesis": h,
                    "reason": f"balance_${account_balance:.2f} < min_${self.min_order_usd}",
                })
                if self.audit:
                    self.audit.append("TRADE_REJECTED", {
                        "asset": h.get("asset"),
                        "reason": "balance_below_min_order",
                        "balance": account_balance,
                        "min_order_usd": self.min_order_usd,
                    })
                print(f"  ❌ {h['asset']:8} {direction:5} — balance ${account_balance:.2f} < min ${self.min_order_usd} "
                      f"(deposit more funds or lower min_order_usd in config)")
                continue

            trade = {
                "asset": h["asset"],
                "strategy": h["strategy"],
                "direction": direction,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "position_size": round(float(quantity), 8),
                "notional_usd": round(float(notional), 2),
                "risk_usd": round(float(risk_amount_usd_eff), 2),
                "atr_at_signal": atr,
                "balance_source": balance_source,
            }

            # --- Sprint 1: Mandate gate ---
            if self.mandate is not None:
                verdict = self.mandate.validate(trade)
                if not verdict.ok:
                    rejected.append({"trade": trade, "reason": verdict.reason})
                    if self.audit:
                        self.audit.append("MANDATE_BLOCKED", {"asset": trade["asset"], "reason": verdict.reason})
                    print(f"  🛡️  {trade['asset']:8} {direction:5} — blocked by mandate: {verdict.reason}")
                    continue
                if self.audit:
                    self.audit.append("MANDATE_OK", {"asset": trade["asset"], "notional": trade["notional_usd"], "risk": trade["risk_usd"]})

            # --- Sprint 2: max_open_trades ---
            if slots_left <= 0:
                rejected.append({"trade": trade, "reason": f"max_open_trades:{self.max_open_trades}"})
                if self.audit:
                    self.audit.append("TRADE_REJECTED", {"asset": trade["asset"], "reason": "max_open_trades_reached"})
                print(f"  🚫 {trade['asset']:8} {direction:5} — slots llenos ({open_count}/{self.max_open_trades})")
                continue

            approved.append(trade)
            slots_left -= 1

            if self.audit:
                self.audit.append(
                    "TRADE_APPROVED",
                    {
                        "asset": trade["asset"],
                        "direction": direction,
                        "entry_price": entry_price,
                        "stop_loss": stop_loss,
                        "take_profit": take_profit,
                        "qty": trade["position_size"],
                        "notional_usd": trade["notional_usd"],
                        "risk_usd": trade["risk_usd"],
                    },
                )

            # --- Sprint 2: registrar posición abierta ---
            if self.position_repo is not None:
                import time
                pos = Position(
                    asset=trade["asset"],
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    qty=float(quantity),
                    risk_usd=risk_amount_usd_eff,
                    entry_ts=time.time(),
                    strategy=h.get("strategy", ""),
                )
                self.position_repo.add_open(pos)
                if self.audit:
                    self.audit.append(
                        "POSITION_OPENED",
                        {"position_id": pos.position_id, "asset": pos.asset, "qty": pos.qty, "notional_usd": pos.notional_usd},
                    )

            print(
                f"  ✅ {trade['asset']:8} {direction:5} @ ${entry_price:>9.2f} | "
                f"qty={quantity:>10.6f} | "
                f"SL=${stop_loss:>9.2f} | TP=${take_profit:>9.2f} | "
                f"risk=${risk_amount_usd_eff:.2f} | notional=${notional:.2f}"
            )

        return {
            "approved_trades": approved,
            "rejected_trades": rejected,
            "account_balance": account_balance,
            "balance_source": balance_source,
            "open_positions_after": self.position_repo.count_open() if self.position_repo else 0,
        }
