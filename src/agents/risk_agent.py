"""
Sprint 0+1+2+18 — RiskManagerAgent.

Sprint 0 fixes: ATR-based stops, qty = risk/distance, min order check.
Sprint 1: Mandate Gate + audit ledger integration.
Sprint 2: Take profit ATR-based, max_open_trades respetado, persiste
posición abierta en el PositionRepository.
Sprint 18 (Audit fix): auto-adjust cuando notional < min_order (no solo
max_notional < min_order). Esto permite operar cuentas pequeñas donde
1% de riesgo + stop ATR produce notional < $10 (Binance min).
Sprint 18 (Portfolio mgmt): position replacement — si max_open_trades
está lleno pero aparece una señal MUY superior, cerrar la peor posición
y abrir la nueva. Score = combinación de expected value + momentum.
"""
import os
from typing import Dict, Any, List, Optional, Tuple

from src.data_store.positions import Position, PositionRepository


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
        enable_position_replacement: bool = True,
        replacement_score_threshold: float = 0.20,
        current_prices: Optional[Dict[str, float]] = None,
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
        # Sprint 18: portfolio management
        self.enable_position_replacement = enable_position_replacement
        # New hypothesis must score at least +20% above the worst open position
        self.replacement_score_threshold = replacement_score_threshold
        # Used by position-replacement scoring (passed per-cycle from main loop)
        self.current_prices = current_prices or {}

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
        # Sprint 18 fix (B020): at most ONE position replacement per cycle.
        # Without this flag, if max_open_trades is reached and we receive N
        # hypotheses, the bot would do N consecutive replacements (close +
        # open) instead of approving only the best candidate. Each replacement
        # resets slots_left=1, then the next iteration sees slots_left=0
        # again and triggers another replacement → exposure can spiral.
        did_replace_this_cycle = False
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

            # --- Step 1: Cap to max_notional if risk-sized position exceeds it ---
            if notional > max_notional:
                quantity = max_notional / entry_price
                notional = quantity * entry_price

            # --- Step 2: Auto-adjust if notional < min_order_usd (Sprint 18 fix) ---
            # The original Sprint 12 logic only triggered when max_notional < min_order.
            # That misses the common case where:
            #   max_notional = $10 (50% of $20) ✅ above min
            #   but risk/stop_distance = $5 (1% risk, 4% stop) ❌ below min
            # Without this fix, the trade is rejected by min_order check.
            # Sprint 18: trigger auto-adjust whenever computed notional < min_order,
            # log WHY (so user knows if it's config cap or risk/distance that's the
            # bottleneck), bump to min_order.
            auto_adjust_reason = None
            if notional < self.min_order_usd:
                if max_notional < self.min_order_usd:
                    auto_adjust_reason = "max_cap_below_min_order"
                else:
                    auto_adjust_reason = "risk_below_min_order"
                adjusted_cap = self.min_order_usd
                adjusted_cap_pct = (adjusted_cap / account_balance) * 100.0 if account_balance > 0 else 0
                quantity = adjusted_cap / entry_price
                notional = quantity * entry_price
                if self.audit:
                    self.audit.append("CAP_AUTO_ADJUSTED", {
                        "asset": h.get("asset"),
                        "reason": auto_adjust_reason,
                        "config_pct": self.max_capital_per_trade_pct,
                        "config_notional": round(max_notional, 2),
                        "raw_risk_notional": round(risk_amount_usd / stop_distance * entry_price, 2) if stop_distance > 0 else 0.0,
                        "min_order_usd": self.min_order_usd,
                        "adjusted_to_pct": round(adjusted_cap_pct, 2),
                        "adjusted_to_notional": round(notional, 2),
                    })
                print(f"  ⚠️ {h['asset']:8} {direction:5} — "
                      f"notional ${notional:.2f} < min ${self.min_order_usd:.2f} "
                      f"({auto_adjust_reason}). "
                      f"Bumping to ${notional:.2f} ({adjusted_cap_pct:.1f}% effective). "
                      f"⚠️ FIX config.yaml: increase max_capital_per_trade_pct or risk_per_trade_pct, "
                      f"or lower min_order_usd.")

            risk_amount_usd_eff = quantity * stop_distance

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
                # Sprint 18: try position replacement before outright rejection.
                # If the new hypothesis scores higher than the worst open position
                # (by replacement_score_threshold), close the worst and free a slot.
                #
                # B020 fix: at most ONE replacement per validate_and_size() call.
                # Subsequent hypotheses in the same cycle that hit max_open_trades
                # are rejected normally (no more churning the portfolio).
                replaced = False
                if (
                    self.enable_position_replacement
                    and self.position_repo is not None
                    and not did_replace_this_cycle
                ):
                    replaced = self._try_replace_position(
                        new_hyp=h,
                        new_trade=trade,
                        new_score_inputs={
                            "expected_move_pct": float(h.get("expected_move_pct", atr * 4 / max(entry_price, 1e-9) * 100)),
                            "atr_at_signal": atr,
                            "entry_price": entry_price,
                            "stop_loss": stop_loss,
                            "take_profit": take_profit,
                            "direction": direction,
                            "strategy": h.get("strategy", ""),
                        },
                    )
                if not replaced:
                    rejected.append({"trade": trade, "reason": f"max_open_trades:{self.max_open_trades}"})
                    if self.audit:
                        self.audit.append("TRADE_REJECTED", {"asset": trade["asset"], "reason": "max_open_trades_reached"})
                    print(f"  🚫 {trade['asset']:8} {direction:5} — slots llenos ({open_count}/{self.max_open_trades})")
                    continue
                # Replacement happened — slot freed, fall through to approval.
                did_replace_this_cycle = True
                slots_left = 1  # we just freed one slot by closing worst

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
                # Sprint 34: emit TRADE_OPENED for the NotificationAgent to
                # forward to Telegram. This is the canonical "position was
                # actually added to the local repo" event — happens AFTER
                # the broker fill (or after a paper trade is approved),
                # so subscribers see consistent state.
                if self.event_bus is not None:
                    self.event_bus.publish("TRADE_OPENED", {
                        "position_id": pos.position_id,
                        "asset": pos.asset,
                        "direction": pos.direction,
                        "entry_price": pos.entry_price,
                        "qty": pos.qty,
                        "stop_loss": pos.stop_loss,
                        "take_profit": pos.take_profit,
                        "risk_usd": pos.risk_usd,
                        "notional_usd": pos.notional_usd,
                        "strategy": pos.strategy,
                        "entry_ts": pos.entry_ts,
                    })

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

    # ----------------------------------------------------------------------
    # Sprint 18: Portfolio Management — Position Replacement + Scoring
    # ----------------------------------------------------------------------

    def score_position(self, pos: Position, current_price: Optional[float] = None) -> float:
        """
        Score an OPEN position by expected remaining value.

        Lower score = worse candidate to replace. Combines:
          - unrealized P&L % (losing positions score lower)
          - time held (positions held long with no progress score lower)
          - distance to TP vs SL (closer to SL = lower)
          - momentum alignment (if current price is moving against direction)

        Range: roughly [-1.0, +1.0] but unbounded. Higher is better.
        """
        price = current_price if current_price is not None else self.current_prices.get(pos.asset)
        score = 0.0

        # --- 1. Unrealized P&L % of notional (-1 to +1) ---
        if price is not None and pos.notional_usd > 0:
            upnl_pct = pos.unrealized_pnl(price) / pos.notional_usd
            score += upnl_pct  # losing = negative, winning = positive

        # --- 2. Distance from current price to SL (closer = worse) ---
        if price is not None and pos.stop_loss > 0:
            if pos.direction == "long":
                dist_to_sl = (price - pos.stop_loss) / max(price, 1e-9)
            else:
                dist_to_sl = (pos.stop_loss - price) / max(price, 1e-9)
            # dist_to_sl in [0, 1+]; > 0.05 = "comfortable"
            score += min(dist_to_sl * 2.0, 0.5) - 0.1  # up to +0.4, -0.1 if at SL

        # --- 3. Time decay (positions held > 24h without progress get penalized) ---
        import time as _t
        age_h = (pos.entry_ts and (_t.time() - pos.entry_ts) / 3600.0) or 0.0
        if age_h > 24:
            score -= 0.15  # stale positions lose points
        if age_h > 72:
            score -= 0.15  # even more stale

        # --- 4. Reward-to-risk remaining ---
        if price is not None and pos.entry_price > 0:
            if pos.direction == "long":
                remaining_tp = max(pos.take_profit - price, 0.0)
                remaining_sl = max(price - pos.stop_loss, 0.0)
            else:
                remaining_tp = max(price - pos.take_profit, 0.0)
                remaining_sl = max(pos.stop_loss - price, 0.0)
            if remaining_sl > 0:
                rr_remaining = remaining_tp / remaining_sl
                # rr_remaining > 1 = good, < 1 = bad
                score += min(rr_remaining * 0.2, 0.3) - 0.1
            else:
                # already past stop, score very low
                score -= 0.5

        return score

    def score_new_hypothesis(self, inputs: dict) -> float:
        """
        Score a NEW hypothesis (proposed trade) by expected edge.

        Higher = better candidate to enter. Combines:
          - expected_move_pct (theoretical edge before fees)
          - risk/reward ratio of the proposed trade
          - atr_at_signal (lower ATR relative to entry = cleaner setup)

        Range: roughly [-0.5, +1.0]. Higher is better.
        """
        score = 0.0

        # --- 1. Expected move (% of entry) ---
        expected_move_pct = float(inputs.get("expected_move_pct", 0.0))
        # 5% expected move is "very good", 1% is "okay", 0 = neutral
        score += min(expected_move_pct * 5.0, 0.6)  # up to +0.6

        # --- 2. R:R of the proposed trade ---
        entry = float(inputs.get("entry_price", 0.0))
        sl = float(inputs.get("stop_loss", 0.0))
        tp = float(inputs.get("take_profit", 0.0))
        if entry > 0 and sl > 0 and tp > 0:
            if inputs.get("direction", "long") == "long":
                risk = max(entry - sl, 1e-9)
                reward = max(tp - entry, 1e-9)
            else:
                risk = max(sl - entry, 1e-9)
                reward = max(entry - tp, 1e-9)
            rr = reward / risk if risk > 0 else 0
            # rr >= 2 = good (matches config default 4:2 ATR)
            score += min(rr * 0.2, 0.4)  # up to +0.4

        # --- 3. ATR relative to entry (tight stops = cleaner) ---
        atr = float(inputs.get("atr_at_signal", 0.0))
        if entry > 0 and atr > 0:
            atr_pct = atr / entry
            # 2% ATR = neutral, > 5% = noisy (penalty), < 1% = clean (bonus)
            if atr_pct > 0.05:
                score -= 0.2
            elif atr_pct < 0.01:
                score += 0.1

        return score

    def _try_replace_position(
        self,
        new_hyp: dict,
        new_trade: dict,
        new_score_inputs: dict,
    ) -> bool:
        """
        Try to close the worst-scoring open position to free a slot for `new_trade`.

        Returns True if a position was closed (slot freed) and `new_trade` should
        be approved. Returns False if no open position scored worse than
        `new_trade` minus the threshold (no replacement happens).
        """
        if self.position_repo is None:
            return False

        opens = self.position_repo.open()
        if not opens:
            return False

        new_score = self.score_new_hypothesis(new_score_inputs)
        # Find worst open position
        scored = []
        for p in opens:
            current_price = self.current_prices.get(p.asset)
            s = self.score_position(p, current_price=current_price)
            scored.append((p, s))
        scored.sort(key=lambda x: x[1])
        worst_pos, worst_score = scored[0]

        threshold = self.replacement_score_threshold
        if new_score <= worst_score + threshold:
            # New signal not enough better than the worst open. Don't replace.
            if self.audit:
                self.audit.append("REPLACEMENT_SKIPPED", {
                    "new_asset": new_trade["asset"],
                    "new_score": round(new_score, 3),
                    "worst_asset": worst_pos.asset,
                    "worst_score": round(worst_score, 3),
                    "threshold": threshold,
                    "delta": round(new_score - worst_score, 3),
                    "reason": "insufficient_edge",
                })
            return False

        # --- Replace: close worst at current price, free slot ---
        # B021 fix: if we don't have a fresh price for this asset, ABORT the
        # replacement rather than fall back to entry_price. Closing at
        # entry_price would record realized_pnl=0 even if the position is
        # actually in profit or loss — that corrupts the audit log and
        # daily_loss accounting. Better to skip this cycle and let the
        # PositionMonitor close it next tick when we have real prices.
        close_price = self.current_prices.get(worst_pos.asset)
        if close_price is None or close_price <= 0:
            if self.audit:
                self.audit.append("REPLACEMENT_SKIPPED", {
                    "new_asset": new_trade["asset"],
                    "new_score": round(new_score, 3),
                    "worst_asset": worst_pos.asset,
                    "worst_score": round(worst_score, 3),
                    "threshold": threshold,
                    "delta": round(new_score - worst_score, 3),
                    "reason": "no_current_price",
                })
            print(
                f"  ⏸️  REPLACEMENT_ABORTED {worst_pos.asset:8} — "
                f"no current price available; will retry next cycle"
            )
            return False
        closed = self.position_repo.close_position(
            worst_pos.position_id,
            close_price=close_price,
            reason="REPLACED_BY_BETTER_SIGNAL",
        )
        if closed is None:
            return False

        realized = closed.realized_pnl or 0.0
        if self.audit:
            self.audit.append("POSITION_REPLACED", {
                "closed_position_id": closed.position_id,
                "closed_asset": closed.asset,
                "closed_direction": closed.direction,
                "closed_entry": closed.entry_price,
                "closed_price": close_price,
                "closed_pnl_usd": round(realized, 4),
                "closed_score": round(worst_score, 3),
                "new_asset": new_trade["asset"],
                "new_direction": new_trade["direction"],
                "new_score": round(new_score, 3),
                "delta_score": round(new_score - worst_score, 3),
                "threshold": threshold,
            })

        # Send a broker order for the close
        if self.broker is not None:
            try:
                side = "sell" if closed.direction == "long" else "buy"
                symbol = closed.asset.replace("-", "/") if "-" in closed.asset else closed.asset
                self.broker.create_market_order(symbol, side, closed.qty)
            except Exception as e:
                print(f"[RiskManagerAgent] Error cerrando {closed.asset} para replacement: {e}")

        print(
            f"  🔄 REPLACED {closed.asset:8} {closed.direction:5} "
            f"(score {worst_score:.2f}, pnl ${realized:+.2f}) → "
            f"opened {new_trade['asset']:8} {new_trade['direction']:5} "
            f"(score {new_score:.2f}, Δ {new_score - worst_score:+.2f})"
        )
        return True
