"""
Sprint 2 — PositionMonitor.

Cada tick del loop, antes de evaluar nuevas hipótesis, el monitor revisa
si stops/TPs de posiciones abiertas han sido cruzados. Si sí:

1. Cierra la posición en el repo (registra realized P&L)
2. Emite evento al event bus (`TRADE_CLOSED`)
3. Log al audit ledger

Esto es crítico para que el bot registre P&L real y no deje posiciones
colgadas con stop ya cruzado pero sin cerrar formalmente.
"""
from typing import Dict, Optional
from src.data_store.positions import PositionRepository, Position


class PositionMonitor:
    def __init__(self, repo: PositionRepository, audit=None, event_bus=None, broker=None):
        self.repo = repo
        self.audit = audit
        self.event_bus = event_bus
        self.broker = broker

    def check(self, current_prices: Dict[str, float]) -> list:
        """
        Revisa stops/TPs y cierra las que cruzaron.
        Devuelve lista de (position, close_price, reason) cerradas en este ciclo.
        """
        closes = []
        # Snapshot para evitar mutación durante iteración
        for pos in list(self.repo.open()):
            asset = pos.asset
            # yfinance usa "BTC-USD" pero ccxt usa "BTC/USDT". Para el monitor
            # sólo necesitamos el precio, no la simbología exacta.
            price = current_prices.get(asset)
            if price is None:
                # Intenta variantes
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

    def _execute_close(self, pos: Position, price: float, reason: str):
        # Si tenemos broker real, enviar orden opuesta
        if self.broker is not None:
            try:
                side = "sell" if pos.direction == "long" else "buy"
                symbol = pos.asset.replace("-", "/") if "-" in pos.asset else pos.asset
                self.broker.create_market_order(symbol, side, pos.qty)
            except Exception as e:
                print(f"[PositionMonitor] Error cerrando {pos.asset} en broker: {e}")
                # Aún así marcamos como cerrada localmente
                pass

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
