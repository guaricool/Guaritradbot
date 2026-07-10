"""
Sprint 23 — Live Equity Tracker.

Carlos: "¿No hay manera de que si mete 10 dólares entonces y va arriba así
sea muy poquito entonces pueda enseñarte cuántos centavos o dólares vas
ganando o perdiendo?"

YES. Este módulo rastrea el equity en vivo con precisión de 4 decimales
(centavos). Funciona con cualquier balance inicial (incluido $10).

Tres componentes:
1. `EquitySnapshot` — dataclass con todos los campos visibles
2. `EquityTracker` — calcula y mantiene historial de equity en cada ciclo
3. `format_equity_line()` — helper para dashboards / logs / Telegram

Diseño:
- **Precision**: 4 decimales (`$10.0123`) — crítico para cuentas pequeñas
- **History**: ring buffer de últimos N snapshots (default 200)
- **Source-of-truth**: `PositionRepository` para realized, precios live para unrealized
- **Idempotent**: se puede llamar `update()` múltiples veces; cada llamada
  agrega un snapshot al historial
- **Sin estados globales**: cada tracker tiene su propio state
"""
from __future__ import annotations
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Deque, Dict, List, Optional

from src.data_store.positions import PositionRepository


@dataclass
class EquitySnapshot:
    """Una 'foto' del equity en un momento dado."""
    timestamp: float           # unix ts
    iso: str                  # ISO string
    starting_balance: float   # baseline
    realized_pnl: float       # acumulado de posiciones cerradas
    unrealized_pnl: float     # mark-to-market de posiciones abiertas
    total_equity: float        # starting + realized + unrealized
    delta_usd: float           # total_equity - starting_balance
    delta_pct: float           # (delta / starting) * 100
    open_positions: int        # count
    closed_positions: int      # count
    drawdown_usd: float        # total_equity - max(historical total_equity)
    drawdown_pct: float        # drawdown / max * 100

    def to_dict(self) -> dict:
        return asdict(self)


class EquityTracker:
    """
    Rastrea el equity del bot en tiempo real con precisión sub-dólar.

    Args:
        starting_balance: el balance inicial (ej. $10.00)
        position_repo: shared PositionRepository instance
        history_size: cuántos snapshots guardar (default 200)
        audit: opcional, para loggear EQUITY_UPDATE events

    Uso:
        tracker = EquityTracker(starting_balance=10.00, position_repo=repo)
        snapshot = tracker.update(current_prices={"BTC-USD": 50123.45})
        print(f"Equity: ${snapshot.total_equity:.4f} | Δ ${snapshot.delta_usd:+.4f}")
    """

    def __init__(
        self,
        starting_balance: float,
        position_repo: Optional[PositionRepository] = None,
        history_size: int = 200,
        audit=None,
        precision_decimals: int = 4,
    ):
        if starting_balance <= 0:
            raise ValueError(f"starting_balance must be > 0, got {starting_balance}")
        self.starting_balance = starting_balance
        self.position_repo = position_repo
        self.precision = precision_decimals
        self.audit = audit
        self.history: Deque[EquitySnapshot] = deque(maxlen=history_size)
        # Track max equity seen for drawdown calc
        self._max_equity = starting_balance
        # Initialize with a baseline snapshot at t=0
        self._append_snapshot(
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            open_positions=0,
            closed_positions=0,
        )

    def update(self, current_prices: Optional[Dict[str, float]] = None) -> EquitySnapshot:
        """
        Compute current equity and append a snapshot to history.

        Args:
            current_prices: dict of {asset: price} for mark-to-market of open positions.
                            If None or asset missing, that position's unrealized_pnl is 0.

        Returns:
            EquitySnapshot with all fields populated.
        """
        realized_pnl = 0.0
        unrealized_pnl = 0.0
        open_count = 0
        closed_count = 0

        if self.position_repo is not None:
            for p in self.position_repo.all():
                if p.is_open:
                    open_count += 1
                    price = (current_prices or {}).get(p.asset)
                    if price is not None:
                        unrealized_pnl += p.unrealized_pnl(price)
                else:
                    closed_count += 1
                    realized_pnl += (p.realized_pnl or 0.0)

        return self._append_snapshot(
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            open_positions=open_count,
            closed_positions=closed_count,
        )

    def _append_snapshot(
        self,
        realized_pnl: float,
        unrealized_pnl: float,
        open_positions: int,
        closed_positions: int,
    ) -> EquitySnapshot:
        total_equity = self.starting_balance + realized_pnl + unrealized_pnl
        delta_usd = total_equity - self.starting_balance
        delta_pct = (delta_usd / self.starting_balance) * 100.0 if self.starting_balance > 0 else 0.0

        # Update max for drawdown
        if total_equity > self._max_equity:
            self._max_equity = total_equity
        drawdown_usd = total_equity - self._max_equity
        drawdown_pct = (drawdown_usd / self._max_equity) * 100.0 if self._max_equity > 0 else 0.0

        snap = EquitySnapshot(
            timestamp=time.time(),
            iso=time.strftime("%Y-%m-%dT%H:%M:%S"),
            starting_balance=self.starting_balance,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_equity=total_equity,
            delta_usd=delta_usd,
            delta_pct=delta_pct,
            open_positions=open_positions,
            closed_positions=closed_positions,
            drawdown_usd=drawdown_usd,
            drawdown_pct=drawdown_pct,
        )
        self.history.append(snap)
        if self.audit is not None:
            self.audit.append("EQUITY_UPDATE", {
                "total_equity": round(snap.total_equity, self.precision),
                "realized_pnl": round(snap.realized_pnl, self.precision),
                "unrealized_pnl": round(snap.unrealized_pnl, self.precision),
                "delta_usd": round(snap.delta_usd, self.precision),
                "delta_pct": round(snap.delta_pct, 4),
                "drawdown_pct": round(snap.drawdown_pct, 4),
                "open_positions": snap.open_positions,
                "closed_positions": snap.closed_positions,
            })
        return snap

    def latest(self) -> EquitySnapshot:
        """Returns the most recent snapshot."""
        return self.history[-1]

    def equity_series(self, precision: Optional[int] = None) -> List[float]:
        """Returns just the equity values over time (for sparklines)."""
        p = precision if precision is not None else self.precision
        return [round(s.total_equity, p) for s in self.history]

    def delta_series(self, precision: Optional[int] = None) -> List[float]:
        """Returns just the delta_usd values over time (for sparklines)."""
        p = precision if precision is not None else self.precision
        return [round(s.delta_usd, p) for s in self.history]

    def summary(self) -> dict:
        """Returns a compact summary for the dashboard."""
        latest = self.latest()
        return {
            "starting_balance": round(self.starting_balance, self.precision),
            "total_equity": round(latest.total_equity, self.precision),
            "delta_usd": round(latest.delta_usd, self.precision),
            "delta_pct": round(latest.delta_pct, 4),
            "realized_pnl": round(latest.realized_pnl, self.precision),
            "unrealized_pnl": round(latest.unrealized_pnl, self.precision),
            "drawdown_pct": round(latest.drawdown_pct, 4),
            "open_positions": latest.open_positions,
            "closed_positions": latest.closed_positions,
            "snapshots": len(self.history),
        }


def format_equity_line(snap: EquitySnapshot, precision: int = 4) -> str:
    """
    Format a one-line equity report for logs / Telegram / CLI.

    Example output:
        💰 Equity: $10.0123 | ΔP&L: $+0.0123 (+0.12%) | Open: 1 | Drawdown: -0.50%
    """
    p = precision
    delta_sign = "+" if snap.delta_usd >= 0 else ""
    emoji = "🟢" if snap.delta_usd >= 0 else "🔴"
    return (
        f"{emoji} Equity: ${snap.total_equity:.{p}f} | "
        f"ΔP&L: {delta_sign}${snap.delta_usd:.{p}f} ({snap.delta_pct:+.2f}%) | "
        f"Open: {snap.open_positions} | "
        f"Drawdown: {snap.drawdown_pct:.2f}%"
    )