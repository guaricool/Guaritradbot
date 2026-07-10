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
from src.data_store.positions import PositionRepository, Position


class PositionMonitor:
    def __init__(
        self,
        repo: PositionRepository,
        audit=None,
        event_bus=None,
        broker=None,
        min_profit_to_protect: float = 0.0,
    ):
        """
        Args:
            repo: position repository
            audit: optional audit ledger
            event_bus: optional event bus
            broker: optional broker client
            min_profit_to_protect: minimum unrealized PnL (USD) required to
                trigger an early close on reversal. 0 = always protect if in
                profit. Default 0 (any profit triggers if reversal signal).
        """
        self.repo = repo
        self.audit = audit
        self.event_bus = event_bus
        self.broker = broker
        self.min_profit_to_protect = min_profit_to_protect

    def check(self, current_prices: Dict[str, float]) -> list:
        """
        Revisa stops/TPs y cierra las que cruzaron.
        Devuelve lista de posiciones cerradas en este ciclo.
        """
        closes = []
        for pos in list(self.repo.open()):
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

    def check_with_signals(
        self,
        current_prices: Dict[str, float],
        signals: List[Dict[str, Any]],
        signal_min_strength: float = 0.6,
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

        Returns:
            Lista de posiciones cerradas tempranamente.
        """
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
            if upnl <= self.min_profit_to_protect:
                # No hay profit que proteger
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
                print(
                    f"  💎 SMART_PROFIT_TAKE {asset:8} {pos.direction:5} "
                    f"@ ${price:.2f} (unrealized ${upnl:+.2f}, "
                    f"signal {opposite} strength {matching_signal.get('strength'):.2f})"
                )
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

        For paper mode (no broker), the existing behavior is preserved
        (close_position is called directly) because there is no real
        exchange to talk to.
        """
        # If we have a real broker, try to close on the exchange first.
        if self.broker is not None:
            try:
                side = "sell" if pos.direction == "long" else "buy"
                symbol = pos.asset.replace("-", "/") if "-" in pos.asset else pos.asset
                broker_order = self.broker.create_market_order(symbol, side, pos.qty)
                # Some broker adapters return a dict with a status;
                # if it explicitly says "failed", treat as failure.
                if isinstance(broker_order, dict) and broker_order.get("status") == "failed":
                    raise RuntimeError(
                        f"broker_rejected:{broker_order.get('error', 'unknown')}"
                    )
            except Exception as e:
                msg = f"[PositionMonitor] ⚠️ Broker FAILED cerrando {pos.asset} ({reason}): {e}. " \
                      f"Position {pos.position_id} stays open in repo — will retry next cycle."
                print(msg)
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

        closed = self.repo.close_position(pos.position_id, price, reason)
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