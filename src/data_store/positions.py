"""
Sprint 2 — Position Repository.

Estado persistente de posiciones abiertas y cerradas. Persistido en
JSON en disco (`data_store/positions.json`). Permite:

- Saber cuántas posiciones hay abiertas (para max_open_trades del RiskAgent)
- Calcular exposure real en vivo (para Mandate Gate)
- Detectar stops/TPs cruzados
- Reportar realized P&L por posición cerrada
- Cargar el estado al startup (Crash-only design: si el bot muere, las
  posiciones siguen vivas)

Inspirado en NautilusTrader's `Position` class + el concepto de
`state persistence`.
"""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional


@dataclass
class Position:
    asset: str
    direction: str  # "long" | "short"
    entry_price: float
    stop_loss: float
    take_profit: float
    qty: float
    risk_usd: float
    entry_ts: float
    strategy: str
    position_id: str = field(
        default_factory=lambda: f"pos_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
    )

    # Set on close
    closed_ts: Optional[float] = None
    closed_price: Optional[float] = None
    close_reason: Optional[str] = None  # "STOP_HIT" | "TP_HIT" | "MANUAL" | "REVERSE_SIGNAL"
    realized_pnl: Optional[float] = None

    @property
    def notional_usd(self) -> float:
        return abs(self.entry_price * self.qty)

    @property
    def is_open(self) -> bool:
        return self.closed_ts is None

    def unrealized_pnl(self, current_price: float) -> float:
        if self.is_open:
            direction_sign = 1.0 if self.direction == "long" else -1.0
            return direction_sign * (current_price - self.entry_price) * self.qty
        return self.realized_pnl or 0.0

    def should_close_at(self, current_price: float) -> tuple[bool, str]:
        """Devuelve (hit, reason) si el current_price cruzó el SL o TP."""
        if not self.is_open:
            return (False, "")
        if self.direction == "long":
            if current_price <= self.stop_loss:
                return (True, "STOP_HIT")
            if current_price >= self.take_profit:
                return (True, "TP_HIT")
        else:  # short
            if current_price >= self.stop_loss:
                return (True, "STOP_HIT")
            if current_price <= self.take_profit:
                return (True, "TP_HIT")
        return (False, "")


class PositionRepository:
    def __init__(self, path: str = "data_store/positions.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.positions: List[Position] = []
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.positions = [Position(**p) for p in data.get("positions", [])]
        except Exception as e:
            print(f"[PositionRepo] No se pudo cargar {self.path}: {e}")

    def _save(self):
        data = {"positions": [asdict(p) for p in self.positions], "saved_at": time.time()}
        # Atomic write: temp + replace
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)
        # Sprint 11: también escribir al volumen compartido (audit/) para
        # que el dashboard container pueda ver las posiciones. El bot
        # container NO comparte data_store/ con el dashboard, pero sí
        # comparte audit/.
        try:
            mirror = Path("audit/positions.json")
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        except Exception as e:
            # No fatal — el bot sigue funcionando, solo el dashboard no
            # podrá ver las posiciones en este ciclo.
            print(f"[PositionRepo] mirror to audit/ failed: {e}")

    # --- queries ---
    def all(self) -> List[Position]:
        return list(self.positions)

    def open(self) -> List[Position]:
        return [p for p in self.positions if p.is_open]

    def open_for_asset(self, asset: str) -> List[Position]:
        return [p for p in self.positions if p.is_open and p.asset == asset]

    def total_exposure_usd(self) -> float:
        return sum(p.notional_usd for p in self.open())

    def count_open(self) -> int:
        return len(self.open())

    def total_realized_pnl_usd(self) -> float:
        return sum(p.realized_pnl or 0.0 for p in self.positions if p.realized_pnl is not None)

    # --- mutations ---
    def add_open(self, position: Position) -> None:
        self.positions.append(position)
        self._save()

    def close_position(self, position_id: str, close_price: float, reason: str) -> Optional[Position]:
        for p in self.positions:
            if p.position_id == position_id and p.is_open:
                p.closed_ts = time.time()
                p.closed_price = close_price
                p.close_reason = reason
                direction_sign = 1.0 if p.direction == "long" else -1.0
                p.realized_pnl = direction_sign * (close_price - p.entry_price) * p.qty
                self._save()
                return p
        return None

    def close_for_asset(self, asset: str, close_price: float, reason: str) -> List[Position]:
        closed = []
        for p in list(self.positions):
            if p.is_open and p.asset == asset:
                if self.close_position(p.position_id, close_price, reason):
                    closed.append(p)
        return closed
