"""
Sprint 2+18 — PositionMonitor.

Cada tick del loop, antes de evaluar nuevas hipótesis, el monitor revisa
si stops/TPs de posiciones abiertas han sido cruzados. Si sí:

1. Cierra la posición en el repo (registra realized P&L)
2. Emite evento al event bus (`TRADE_CLOSED`)
3. Log al audit ledger

Esto es crítico para que el bot registre P&L real y no deje posiciones
colgadas con stop ya cruzado pero sin cerrar formalmente.

Sprint 18: Smart Profit Take
============================
Además del check mecánico de SL/TP, este monitor puede detectar
reversals en posiciones EN GANANCIA y cerrarlas preventivamente para
proteger profit antes de que el momentum se revierta.

Trigger conditions (cualquiera es suficiente):
  - La posición está en profit > min_profit_to_protect (e.g., 1× el risk)
  - Y el contexto (reversal_signals / current_signals) contiene una señal
    en dirección OPUESTA a la posición, con fuerza suficiente

Casos de uso:
  - LONG abierto en BTC, BTC está +2%, aparece señal SHORT fuerte → cerrar
    el LONG con profit y dejar que la nueva señal se evalúe en el siguiente
    ciclo (o en este mismo si reemplazable).
  - Posición con profit protegido que de otro modo se revertiría a pérdida.

El método `check_with_signals()` acepta la lista de hipótesis actuales
generadas por StrategyAgent; si alguna contradice una posición en profit,
dispara cierre temprano.
"""
from typing import Dict, Optional, List, Any
import time
from src.data_store.positions import PositionRepository, Position

from src.core.logging_setup import get_logger
logger = get_logger(__name__)
from src.execution.broker_routing import (
    build_asset_to_class_map,
    is_mandate_enabled,
    resolve_broker_for_close,
    send_close_order,
)


class PositionMonitor:
    def __init__(
        self,
        repo: PositionRepository,
        audit=None,
        event_bus=None,
        broker=None,
        min_profit_to_protect: float = 0.0,
        # Sprint 46O (audit M2): multiplier applied on top of the
        # round-trip fee when computing the effective "minimum profit
        # to protect" floor. Default 2.0 = require gross profit to
        # clear 2x the round-trip fee before allowing a
        # SMART_PROFIT_TAKE close, so a $0.01 gross profit on a $10
        # position (~$0.004 round-trip on the real 0.02% binance.us
        # tier — but ~$0.04 at the conservative 0.1% configured
        # default) never triggers a NET realized loss. The audit's
        # exact wording: "min_profit_to_protect ≥ 2× fee". 1.0 would
        # reproduce the pre-fix behavior (1x fee, breakeven after
        # fees — every basis point of slippage becomes a loss).
        min_profit_fee_multiplier: float = 2.0,
        fee_pct_for_asset=None,
        # Sprint 46N (audit C1/C2): route closes by asset class instead
        # of always hitting `broker` (the crypto client), and never send
        # a real order while in paper mode. See broker_routing.py.
        alpaca_broker=None,
        brokers_config: Optional[dict] = None,
        mode_override_path: str = "audit/mode_override.json",
    ):
        """
        Args:
            repo: position repository
            audit: optional audit ledger
            event_bus: optional event bus
            broker: optional CRYPTO broker client (ccxt/binance.us). Kept
                as the name `broker` for backward compatibility — this
                is ONLY used now for crypto-class assets; see
                `alpaca_broker` below for equities.
            min_profit_to_protect: minimum unrealized PnL (USD) required to
                trigger an early close on reversal. 0 = always protect if in
                profit. Default 0 (any profit triggers if reversal signal).
            fee_pct_for_asset: Sprint 46J — optional callable
                `(asset: str) -> float` returning the round-trip fee
                fraction to charge THIS asset's close against realized
                P&L (see `PositionRepository.close_position`'s
                `fee_pct` docstring). Wired in main.py from
                `brokers_config` + `trading.crypto_taker_fee_pct` —
                crypto assets get the real binance.us taker fee,
                Alpaca equities get 0.0 (commission-free). Default None
                = always 0.0, i.e. every position closed through this
                monitor keeps the exact original gross-only P&L unless
                the caller explicitly opts in.
            alpaca_broker: Sprint 46N — optional Alpaca broker client,
                used for closing EQUITY-class positions. Before this,
                every close (regardless of asset) was sent to `broker`
                (the ccxt client), so equity closes silently failed
                forever (CLOSE_FAILED loop) — audit finding C1.
            brokers_config: Sprint 46N — config.yaml's `brokers:`
                section, used to build an asset→class map so we know
                which of `broker`/`alpaca_broker` to call for a given
                position's asset. None/empty ⇒ every asset resolves to
                "unknown" ⇒ closes fall back to a local/simulated close
                (same as if no broker were configured at all) rather
                than guessing.
            mode_override_path: Sprint 46N — path to `mode_override.json`.
                In PAPER mode, closes are now ALWAYS simulated locally
                (repo mutation only, no real order) — audit finding C2.
                Previously there was no paper/live check at all here,
                unlike the entry side (`ExecutionNode`).
        """
        self.repo = repo
        self.audit = audit
        self.event_bus = event_bus
        self.broker = broker
        self.min_profit_to_protect = min_profit_to_protect
        # Clamp the multiplier to a sane range: 0 means "don't pad fee
        # at all" (pre-Sprint-46O behavior — only useful for tests
        # asserting that exact path); values above 10x are almost
        # certainly a typo. Negative makes no economic sense.
        self.min_profit_fee_multiplier = max(0.0, min(float(min_profit_fee_multiplier), 10.0))
        self.fee_pct_for_asset = fee_pct_for_asset
        self.alpaca_broker = alpaca_broker
        self.brokers_config = brokers_config or {}
        self.mode_override_path = mode_override_path
        self._asset_to_class = build_asset_to_class_map(self.brokers_config)

    def _fee_pct(self, asset: str) -> float:
        if self.fee_pct_for_asset is None:
            return 0.0
        try:
            return float(self.fee_pct_for_asset(asset) or 0.0)
        except Exception:
            return 0.0

    def check(self, current_prices: Dict[str, float]) -> list:
        """
        Revisa stops/TPs y cierra las que cruzaron.
        Devuelve lista de posiciones cerradas en este ciclo.

        Sprint 46I: positions with `protection_mode == "native_oco"`
        (a real OCO order resting on binance.us, placed by
        ExecutionNode at entry — see execution_node.py) are handled by
        `_reconcile_native_oco` instead of the price-threshold check
        below. This is INTENTIONAL and important: the exchange, not
        this loop, is what actually closes those positions — if we
        ALSO ran `should_close_at`/`_execute_close` (which sends a
        fresh market order) on a native_oco position, we could sell
        the same qty twice (once via the exchange's own OCO fill, once
        via our own duplicate market order) the moment both conditions
        happen to trigger in the same window. Every other position
        (equities, paper mode, any crypto position where OCO placement
        failed at entry) keeps the EXACT original polling behavior.
        """
        closes = []
        for pos in list(self.repo.open()):
            if pos.protection_mode == "native_oco" and pos.broker_oco_order_id:
                closed = self._reconcile_native_oco(pos, current_prices)
                if closed:
                    closes.append(closed)
                continue

            asset = pos.asset
            price = current_prices.get(asset)
            if price is None:
                # Intenta variantes de simbología
                if "/" in asset:
                    alt = asset.replace("/", "-").replace("USDT", "-USD")
                    price = current_prices.get(alt)
                    if price is None:
                        alt2 = asset.replace("/", "") + "USDT"
                        price = current_prices.get(alt2)
            if price is None:
                continue

            hit, reason = pos.should_close_at(price)
            if hit:
                closed = self._execute_close(pos, price, reason)
                if closed:
                    closes.append(closed)
        return closes

    def _reconcile_native_oco(self, pos: Position, current_prices: Dict[str, float]):
        """Sprint 46I: for a position protected by a real exchange-side
        OCO order, ask the exchange whether it has ALREADY closed the
        position (via the stop or take-profit leg) and, if so, mark it
        closed in the LOCAL repo to match — the bot never sends its
        own close order here, it's purely catching up to what the
        exchange already did.

        Sprint 46Q (audit M5): `listOrderStatus == ALL_DONE` is the
        exchange's terminal state for BOTH a successful OCO fill AND
        a manual cancellation of the orderList. The pre-fix code
        treated every ALL_DONE as a fill, which produced two bugs:
        (a) a manual cancel in the binance.us UI would mark the
        position closed with a `TP_HIT_OCO` reason and a phantom
        profit (the close was never executed, so a realized gain
        was recorded against money that never moved), and (b) the
        fallback "no price feed at this exact tick" path used
        `pos.take_profit` as a neutral default — the SAME phantom
        profit bug in a different trigger. Both are now fixed:
        we parse `orderReports` to see the actual sub-order status
        and only close on a real FILLED leg, and a missing price
        feed produces an audit event and a `None` return (the
        position stays open and we retry next tick — the live OCO
        on the exchange is unaffected by our reconcile either way).

        Close-price approximation: OCO legs fill AT (or extremely
        close to) their specified trigger price, so we use
        `pos.take_profit`/`pos.stop_loss` (whichever the leg's
        trigger was) rather than trying to fetch the exact fill
        price from a second API call — same "good enough, reconciles
        further next cycle if wrong" spirit as the dashboard's
        manual-close endpoint (see src/api/state.py::close_position).

        Fail-safe: any error talking to the exchange (network, auth,
        unexpected response shape) leaves the position untouched — it
        stays open in the repo and we simply retry on the next cycle.
        The real OCO order is still resting on the exchange regardless
        of whether OUR reconciliation succeeds, so this is safe to
        retry indefinitely.
        """
        if self.broker is None or not hasattr(self.broker, "get_oco_order_status"):
            return None
        symbol = pos.asset.replace("-", "/") if "-" in pos.asset else pos.asset
        try:
            status = self.broker.get_oco_order_status(symbol, pos.broker_oco_order_id)
        except Exception as e:
            logger.warning(f'[PositionMonitor] ⚠️ OCO status check falló para {pos.asset}: {e} (reintenta próximo ciclo)')
            return None
        if not isinstance(status, dict) or status.get("status") == "failed":
            # Query itself failed — leave untouched, retry next cycle.
            return None
        list_status = status.get("listOrderStatus")
        if list_status != "ALL_DONE":
            # Still resting on the exchange (EXECUTING) or some other
            # non-terminal state — nothing to reconcile yet.
            return None

        # Sprint 46Q (audit M5): inspect the per-leg status BEFORE
        # assuming the OCO actually filled. `orderReports` is binance's
        # list of the two sub-orders (the LIMIT take-profit and the
        # STOP_LOSS_LIMIT); each has a `status` of FILLED, CANCELED,
        # REJECTED, EXPIRED, etc. Only FILLED means a real close
        # happened. If BOTH legs are CANCELED/REJECTED/EXPIRED, the
        # operator (or the exchange) cancelled the OCO without a
        # fill — the position is still open in the broker sense
        # and we MUST NOT mark it closed in the repo.
        order_reports = status.get("orderReports") or []
        filled_legs = [
            r for r in order_reports
            if isinstance(r, dict) and str(r.get("status", "")).upper() == "FILLED"
        ]
        if not filled_legs:
            # The OCO reached ALL_DONE but no leg FILLED — was a
            # manual/system cancel. Leave the position open, audit
            # the situation, retry next cycle (the position is now
            # unprotected; the next `_execute_close` flow from
            # PositionMonitor.check — or a manual dashboard close —
            # is what should close it from here on).
            if self.audit is not None:
                try:
                    self.audit.append("OCO_CANCELLED_NOT_FILLED", {
                        "asset": pos.asset,
                        "direction": pos.direction,
                        "order_list_id": pos.broker_oco_order_id,
                        "list_status": list_status,
                        "order_reports_statuses": [
                            str(r.get("status")) for r in order_reports
                            if isinstance(r, dict)
                        ],
                        "detail": (
                            "Exchange reports the OCO as ALL_DONE but no "
                            "sub-order FILLED. This means the OCO was "
                            "cancelled (manually on the exchange, by the "
                            "system, or by another bot instance), NOT filled "
                            "by the take-profit or stop-loss. Position is "
                            "left open in the repo and the next check() will "
                            "arm its polling fallback. Audit-only event — no "
                            "realized PnL is recorded because no real close "
                            "happened on the exchange."
                        ),
                    })
                except Exception as _audit_err:
                    logger.warning(f'[PositionMonitor] ⚠️ No pude auditar OCO_CANCELLED_NOT_FILLED: {_audit_err}')
            logger.warning(f'  ⚠️ OCO cancelado sin fill: {pos.asset} {pos.direction} — position sigue abierta, armando polling fallback.')
            return None

        # Identify WHICH leg filled (the higher of the two prices is
        # the TP LIMIT, the lower is the STOP trigger). For a
        # long-protect OCO, FILLED at price >= take_profit is the
        # take-profit leg, FILLED at price <= stop_loss is the stop
        # leg. For a short-protect OCO (not yet used by this bot but
        # future-proof) the same shape works because we only protect
        # longs right now (see execution_node.py:974).
        filled_leg = filled_legs[0]  # at most one leg fills
        try:
            # STOP_LOSS_LIMIT legs report the actual sell-limit
            # price in `price` (not `stopPrice` — that's just the
            # trigger). The pre-Sprint-46Q code ignored this and
            # used pos.stop_loss for realized PnL, silently
            # under-reporting slippage on stop fills.
            filled_price = float(
                filled_leg.get("price")
                or filled_leg.get("stopPrice")
                or 0.0
            )
        except (TypeError, ValueError):
            filled_price = 0.0

        if filled_price >= pos.take_profit:
            # Normal take-profit fill (or marginal slippage above TP).
            # Use the exchange-reported fill price for accurate
            # realized PnL — the pre-Sprint-46Q code used
            # pos.take_profit, which silently rounded the slip into
            # the bot's equity curve as "no slippage".
            close_price = filled_price if filled_price > 0 else pos.take_profit
            reason = "TP_HIT_OCO"
        elif filled_price > 0 and filled_price <= pos.stop_loss:
            # Normal stop fill. If the fill is well outside the
            # configured 1.5% buffer (i.e. >2% below the trigger),
            # label it OCO_FILL_OUTSIDE_TRIGGER_RANGE so the operator
            # can see this was a gap event in the audit feed — the
            # stop LIMIT filled, just not at the planned price.
            buffer_pct = 2.0  # anything past 2% below stop = gap
            buffer_floor = pos.stop_loss * (1.0 - buffer_pct / 100.0)
            if filled_price < buffer_floor:
                close_price = filled_price
                reason = "OCO_FILL_OUTSIDE_TRIGGER_RANGE"
                if self.audit is not None:
                    try:
                        self.audit.append("OCO_FILL_SLIPPAGE", {
                            "asset": pos.asset,
                            "expected_tp": pos.take_profit,
                            "expected_sl": pos.stop_loss,
                            "actual_fill": filled_price,
                            "buffer_floor": buffer_floor,
                            "using_as_close": close_price,
                        })
                    except Exception as _audit_err:
                        logger.warning(f'[PositionMonitor] ⚠️ No pude auditar OCO_FILL_SLIPPAGE: {_audit_err}')
            else:
                # Fill is within the stop buffer — the stop LIMIT
                # honored its guarantee within 2% of the trigger.
                # Use the trigger price for the label but the actual
                # fill for the accounting (rare slippage of a few
                # bps is normal).
                close_price = filled_price
                reason = "STOP_HIT_OCO"
        else:
            # Malformed response (e.g. filled_price is 0 or NaN).
            # Fall back to the more conservative of the two triggers
            # (the SL) so we don't accidentally record a phantom
            # profit at the TP for an exchange fill we can't price.
            close_price = pos.stop_loss
            reason = "OCO_FILL_PRICE_UNREADABLE"
            if self.audit is not None:
                try:
                    self.audit.append("OCO_FILL_PRICE_UNREADABLE", {
                        "asset": pos.asset,
                        "expected_tp": pos.take_profit,
                        "expected_sl": pos.stop_loss,
                        "raw_leg": filled_leg,
                    })
                except Exception as _audit_err:
                    logger.warning(f'[PositionMonitor] ⚠️ No pude auditar OCO_FILL_PRICE_UNREADABLE: {_audit_err}')

        closed = self._finalize_close(pos, close_price, reason)
        if closed:
            logger.info(f'  🔒 OCO reconciled: {pos.asset} {pos.direction} cerrado por el exchange ({reason}) @ ${close_price:.4f}')
        return closed

    def check_with_signals(
        self,
        current_prices: Dict[str, float],
        signals: List[Dict[str, Any]],
        signal_min_strength: float = 0.6,
        max_signal_age_s: Optional[float] = None,
    ) -> list:
        """
        Sprint 18: smart profit-take on reversal.

        Para cada posición abierta:
          1. Si current_price está disponible, calcular unrealized PnL.
          2. Si está en profit >= min_profit_to_protect:
            - Buscar si hay una `signal` en `signals` para el mismo asset
              en dirección OPUESTA con strength >= signal_min_strength.
            - Si sí, cerrar preventivamente.

        Args:
            current_prices: {asset: price} mapa de precios actuales.
            signals: lista de hipótesis generadas por StrategyAgent.
                     Cada signal tiene: asset, direction, strength (0..1).
            signal_min_strength: fuerza mínima de la señal opuesta para
                                 gatillar el cierre temprano.
            max_signal_age_s: Sprint 46R (audit M16) - max age (in
                              seconds) of a signal to consider it
                              for a reversal close. A signal older
                              than this is filtered out as "stale"
                              (the reversal may no longer be valid
                              given the current price). None = no
                              filtering (caller's responsibility).
                              The default in main.py is 300s (5 min).

        Returns:
            Lista de posiciones cerradas tempranamente.
        """
        # Sprint 46R audit M16: defensively filter stale signals at
        # the source, not at the caller. The audit found main.py was
        # feeding signals up to 1h old against fresh prices, which
        # can drive a SMART_PROFIT_TAKE close on a reversal that
        # is no longer valid. The caller (main.py) passes a window
        # via max_signal_age_s; we honor it here too so future
        # callers don't accidentally re-introduce the bug. Signals
        # that don't have a 'ts' field are skipped - we can't age
        # them, so we don't act on them (safer default than
        # including them).
        if max_signal_age_s is not None:
            now = time.time()
            fresh_signals = []
            for sig in signals:
                sig_ts = sig.get("ts")
                if sig_ts is None:
                    # No timestamp = can't verify age. Skip.
                    continue
                age_s = now - float(sig_ts)
                if age_s > max_signal_age_s:
                    continue
                fresh_signals.append(sig)
            signals = fresh_signals
        closes = []
        for pos in list(self.repo.open()):
            asset = pos.asset
            price = current_prices.get(asset)
            if price is None:
                # Variantes de simbología
                if "/" in asset:
                    alt = asset.replace("/", "-").replace("USDT", "-USD")
                    price = current_prices.get(alt)
                    if price is None:
                        alt2 = asset.replace("/", "") + "USDT"
                        price = current_prices.get(alt2)
            if price is None:
                continue

            upnl = pos.unrealized_pnl(price)
            # Sprint 46N (audit M2): min_profit_to_protect used to be
            # compared against RAW gross unrealized PnL. The eventual
            # close below (_execute_close -> close_position) correctly
            # subtracts round-trip fees (Sprint 46J), but this GATE
            # deciding whether to close early at all was fee-blind --
            # with the config default of 0.0, a $0.01 gross profit
            # could trigger a SMART_PROFIT_TAKE close that nets a
            # REALIZED LOSS once ~$0.02 of round-trip fees are
            # deducted. Now the effective floor is whichever is
            # higher: the configured min_profit_to_protect, or the
            # actual round-trip fee cost for THIS position at the
            # current price -- so an early close is only taken when
            # it's provably still a net win after fees, regardless of
            # how low the operator set min_profit_to_protect.
            fee_pct = self._fee_pct(asset)
            round_trip_fee = (
                (pos.entry_price * pos.qty) + (price * pos.qty)
            ) * fee_pct if fee_pct else 0.0
            # Sprint 46O (audit M2): pad the round-trip fee with the
            # configured multiplier (default 2.0x) so a SMART_PROFIT_TAKE
            # close always nets strictly more than 0 after fees — the
            # pre-fix 1x floor was the exact breakeven point and any
            # basis point of slippage or rounding turned it into a
            # realized loss. Higher multiplier = more conservative
            # (skips marginal profits); lower = more aggressive
            # (catches more reversals but risks fee-negative closes).
            fee_adjusted_min = round_trip_fee * self.min_profit_fee_multiplier
            effective_min_profit = max(self.min_profit_to_protect, fee_adjusted_min)
            if upnl <= effective_min_profit:
                # No hay profit que proteger (neto de fees)
                continue

            # Buscar señal opuesta con fuerza suficiente
            opposite = "short" if pos.direction == "long" else "long"
            matching_signal = None
            for sig in signals:
                if (
                    sig.get("asset") == asset
                    and sig.get("direction") == opposite
                    and float(sig.get("strength", 0.0)) >= signal_min_strength
                ):
                    matching_signal = sig
                    break

            if matching_signal is None:
                continue

            # --- Cerrar preventivamente ---
            closed = self._execute_close(
                pos,
                price,
                reason=f"SMART_PROFIT_TAKE:{opposite}_signal_strength_{matching_signal.get('strength'):.2f}",
            )
            if closed:
                closes.append(closed)
                logger.info(f"  💎 SMART_PROFIT_TAKE {asset:8} {pos.direction:5} @ ${price:.2f} (unrealized ${upnl:+.2f}, signal {opposite} strength {matching_signal.get('strength'):.2f})")
        return closes

    def _execute_close(self, pos: Position, price: float, reason: str):
        """Ejecuta cierre en broker + repo + audit/event_bus.

        Sprint 43 C4 fix: the broker call now happens BEFORE the repo
        mutation. If the broker rejects/throws, we DO NOT close the
        position locally — leaving it open for the next monitoring
        cycle to retry. The previous behavior marked the position
        closed in the repo regardless of broker outcome, which meant
        PositionMonitor would stop watching the SL/TP and the mandate
        would stop counting it toward exposure, while the position
        was still live on the exchange (or never opened in the first
        place after a failed close attempt).

        Sprint 46N: "no real broker call" now happens for TWO reasons —
        no broker resolved for this asset's class at all (as before),
        OR we're in paper mode (new) — either way we fall through to
        `_finalize_close` and simulate the close locally.

        Sprint 46I: this is also the path `check_with_signals`
        (SMART_PROFIT_TAKE) uses for ANY open position, including ones
        protected by a real exchange-side OCO order. If we sent a
        fresh market sell WITHOUT first canceling that resting OCO
        order, the position would end up with two live exit paths at
        once — the OCO order would keep sitting on the exchange after
        the repo already considers the position closed, and could
        later try to sell qty the account no longer holds. So: cancel
        the OCO first (best-effort — if it's already filled/gone,
        that's fine, we proceed anyway) whenever this is a
        native_oco position.
        """
        if (
            pos.protection_mode == "native_oco"
            and pos.broker_oco_order_id
            and self.broker is not None
            and hasattr(self.broker, "cancel_oco_order")
        ):
            try:
                symbol = pos.asset.replace("-", "/") if "-" in pos.asset else pos.asset
                self.broker.cancel_oco_order(symbol, pos.broker_oco_order_id)
            except Exception as e:
                logger.warning(f'[PositionMonitor] ⚠️ No se pudo cancelar OCO {pos.broker_oco_order_id} para {pos.asset} antes del cierre manual: {e} (puede que ya se haya ejecutado — continuando con el cierre)')

        # Sprint 46N (audit C1/C2): resolve the broker for THIS asset's
        # class (crypto → self.broker, equity → self.alpaca_broker,
        # unknown → None) instead of always calling self.broker, and
        # never place a real order while in paper mode — both fall
        # through to the same "close locally, no real order" path that
        # already existed for "no broker configured at all".
        close_broker, asset_class = resolve_broker_for_close(
            pos.asset, self._asset_to_class, self.broker, self.alpaca_broker
        )
        is_paper = not is_mandate_enabled(self.mode_override_path)
        if close_broker is not None and not is_paper:
            try:
                side = "sell" if pos.direction == "long" else "buy"
                symbol = pos.asset.replace("-", "/") if "-" in pos.asset and asset_class == "crypto" else pos.asset
                broker_order = send_close_order(close_broker, asset_class, symbol, side, pos.qty)
                # Some broker adapters return a dict with a status;
                # if it explicitly says "failed", treat as failure.
                if isinstance(broker_order, dict) and broker_order.get("status") == "failed":
                    raise RuntimeError(
                        f"broker_rejected:{broker_order.get('error', 'unknown')}"
                    )
            except Exception as e:
                msg = f"[PositionMonitor] ⚠️ Broker FAILED cerrando {pos.asset} ({reason}): {e}. " \
                      f"Position {pos.position_id} stays open in repo — will retry next cycle."
                logger.info(msg)
                if self.audit is not None:
                    self.audit.append(
                        "CLOSE_FAILED",
                        {
                            "position_id": pos.position_id,
                            "asset": pos.asset,
                            "reason_attempted": reason,
                            "broker_error": str(e),
                            "action": "position_remains_open",
                        },
                    )
                if self.event_bus is not None:
                    # CLOSE_FAILED is a critical state — emit SYSTEM_ERROR
                    # so NotificationAgent alerts via Telegram regardless
                    # of paper/live mode.
                    self.event_bus.publish(
                        "SYSTEM_ERROR",
                        {
                            "kind": "CLOSE_FAILED",
                            "position_id": pos.position_id,
                            "asset": pos.asset,
                            "reason_attempted": reason,
                            "broker_error": str(e),
                        },
                    )
                return None  # DO NOT close in repo

        return self._finalize_close(pos, price, reason)

    def _finalize_close(self, pos: Position, price: float, reason: str):
        """Sprint 46I: the actual repo mutation + audit/event_bus
        notification, extracted out of `_execute_close` so
        `_reconcile_native_oco` can share it — that path never sends
        its own broker order (the exchange already closed the position
        via the OCO fill), it only needs this bookkeeping tail.
        """
        # Sprint 46J: charge the asset's real round-trip fee (0.0 for
        # equities / any caller that didn't wire fee_pct_for_asset —
        # see this class's docstring above) against realized P&L.
        closed = self.repo.close_position(
            pos.position_id, price, reason, fee_pct=self._fee_pct(pos.asset)
        )
        if closed and self.audit is not None:
            self.audit.append(
                "TRADE_CLOSED",
                {
                    "position_id": closed.position_id,
                    "asset": closed.asset,
                    "direction": closed.direction,
                    "qty": closed.qty,
                    "entry_price": closed.entry_price,
                    "close_price": price,
                    "reason": reason,
                    "realized_pnl_usd": round(closed.realized_pnl or 0.0, 4),
                    "fees_paid_usd": round(closed.fees_paid_usd or 0.0, 4),
                    "duration_s": (closed.closed_ts - closed.entry_ts) if closed.closed_ts else 0,
                },
            )
        if self.event_bus is not None and closed is not None:
            self.event_bus.publish(
                "TRADE_CLOSED",
                {
                    "position_id": closed.position_id,
                    "asset": closed.asset,
                    "pnl_usd": closed.realized_pnl,
                    "reason": reason,
                },
            )
        return closed
