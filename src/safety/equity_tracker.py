"""
Sprint 23+24 — Live Equity Tracker.

Carlos: "¿No hay manera de que si mete 10 dólares entonces y va arriba así
sea muy poquito entonces pueda enseñarte cuántos centavos o dólares vas
ganando o perdiendo?"

YES. Este módulo rastrea el equity en vivo con precisión de 4 decimales
(centavos). Funciona con cualquier balance inicial (incluido $10).

Sprint 24 añade persistencia crash-only:
- `persist(path)` — guarda state + history a JSON en disco
- `load(path)` — reconstruye tracker desde disco
- Si el bot se reinicia, el equity curve NO se pierde

Tres componentes:
1. `EquitySnapshot` — dataclass con todos los campos visibles
2. `EquityTracker` — calcula y mantiene historial de equity en cada ciclo
3. `format_equity_line()` — helper para dashboards / logs / Telegram

Diseño:
- **Precision**: 4 decimales (`$10.0123`) — crítico para cuentas pequeñas
- **History**: ring buffer de últimos N snapshots (default 200)
- **Persistence**: JSON en disco, escritura atómica (Sprint 2 pattern)
- **Source-of-truth**: `PositionRepository` para realized, precios live para unrealized
- **Idempotent**: se puede llamar `update()` múltiples veces; cada llamada
  agrega un snapshot al historial
- **Sin estados globales**: cada tracker tiene su propio state
"""
from __future__ import annotations
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Deque, Dict, List, Optional

from src.data_store.positions import PositionRepository
from src.core.atomic_write import atomic_write_text
from src.core.logging_setup import get_logger

logger = get_logger(__name__)


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

    def reconcile_external_balance(
        self,
        broker_balance: float,
        current_open_position_notional: float = 0.0,
    ) -> Dict[str, float]:
        """
        Sprint 46R (audit B4): reconcile the tracker's expected
        balance with the broker's live balance, treating any
        difference as an external deposit or withdrawal.

        The pre-46R tracker only knew about starting_balance +
        realized P&L. If Carlos deposited $20 from his bank to
        the broker, the tracker's "expected" balance stayed at
        the old number and the live broker balance jumped by $20.
        The next snapshot would show a $20 "delta" that was
        really a deposit, not a P&L gain. After a few deposits
        the drawdown calculation would be wildly off too (peak
        equity never reset to include the deposits).

        The fix: the caller passes the broker's current
        free balance. We compute
            expected_free = starting_balance
                          + realized_pnl
                          - current_open_position_notional
        (realized P&L is the net of closed trades; open
        positions are deducted from free balance because the
        cash is locked in them). Any discrepancy
            delta = broker_balance - expected_free
        is treated as a deposit (positive) or withdrawal
        (negative) and ADJUSTS `self.starting_balance` so the
        tracker's future snapshots are accurate.

        Returns a dict describing what happened (or didn't):
          {
            "deposit_usd": float,  # positive = Carlos deposited
            "withdrawal_usd": float,  # positive = Carlos withdrew
            "new_starting_balance": float,
            "broker_balance": float,
            "expected_free_before": float,
            "audit_emitted": bool,
          }

        The caller (main.py) should call this method
        periodically (e.g. once per cycle or once per day) with
        the live broker balance. The actual wiring in main.py
        is a follow-up; this method is the audit's "make the
        math right" piece.

        Edge cases handled:
        - broker_balance < 0: skip (bad data from the broker;
          the broker doesn't have a negative free balance in
          any currency we trade).
        - abs(delta) < tolerance_usd: skip (avoid noise from
          rounding or in-flight orders). The default tolerance
          is $0.01 (one cent); pass tolerance_usd to override.
        """
        if broker_balance < 0:
            # The broker doesn't report a negative free balance
            # in any currency we trade. If the caller passes
            # one, that's a broker API error - don't silently
            # apply a phantom deposit/withdrawal.
            return {
                "deposit_usd": 0.0,
                "withdrawal_usd": 0.0,
                "new_starting_balance": self.starting_balance,
                "broker_balance": broker_balance,
                "expected_free_before": 0.0,
                "audit_emitted": False,
            }
        # Latest realized PnL is in the most recent snapshot.
        latest = self.history[-1] if self.history else None
        realized_pnl = latest.realized_pnl if latest else 0.0
        expected_free = (
            self.starting_balance + realized_pnl - current_open_position_notional
        )
        delta = broker_balance - expected_free
        result = {
            "deposit_usd": 0.0,
            "withdrawal_usd": 0.0,
            "new_starting_balance": self.starting_balance,
            "broker_balance": broker_balance,
            "expected_free_before": expected_free,
            "audit_emitted": False,
        }
        if abs(delta) < 0.01:
            # Within rounding tolerance - no event.
            return result
        if delta > 0:
            # Deposit: bump the starting_balance by delta and
            # bump the peak equity if the new expected total
            # exceeds it (the deposit itself isn't a peak unless
            # no other equity has been realized; be conservative
            # and only bump the peak if the new starting_balance
            # alone is a new high).
            self.starting_balance += delta
            if self.starting_balance > self._max_equity:
                self._max_equity = self.starting_balance
            result["deposit_usd"] = delta
            result["new_starting_balance"] = self.starting_balance
            kind = "EQUITY_DEPOSIT"
        else:
            # Withdrawal: drop starting_balance by |delta| and
            # possibly drop the peak equity if the post-withdraw
            # expected total is below it.
            abs_delta = abs(delta)
            self.starting_balance -= abs_delta
            # If the peak was based on the OLD starting balance,
            # pull the peak down by the withdrawal amount so a
            # $50 withdraw from a $100 account at $150 peak
            # becomes a $50 account with a $100 peak.
            if self._max_equity > self.starting_balance:
                self._max_equity = max(self._max_equity - abs_delta, self.starting_balance)
            result["withdrawal_usd"] = abs_delta
            result["new_starting_balance"] = self.starting_balance
            kind = "EQUITY_WITHDRAWAL"

        if self.audit is not None:
            try:
                self.audit.append(kind, {
                    "amount_usd": abs(delta),
                    "broker_balance": round(broker_balance, 4),
                    "expected_free_before": round(expected_free, 4),
                    "new_starting_balance": round(self.starting_balance, 4),
                    "new_max_equity": round(self._max_equity, 4),
                })
                result["audit_emitted"] = True
            except Exception as e:
                logger.error(f"reconcile_external_balance: audit append failed: {e}")
        return result

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


def persist_tracker(tracker: EquityTracker, path: str) -> None:
    """
    Save tracker state (starting balance + history + max equity) to disk.

    Atomic write: temp file + replace, so a crash during write doesn't
    corrupt the existing file (Sprint 2 crash-only design).

    Format: JSON
    {
      "starting_balance": 10.0,
      "precision_decimals": 4,
      "max_equity": 10.5,
      "saved_at": 1234567890.0,
      "history": [<equity_snapshot_dict>, ...]
    }
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "starting_balance": tracker.starting_balance,
        "precision_decimals": tracker.precision,
        "max_equity": tracker._max_equity,
        "saved_at": time.time(),
        "history": [s.to_dict() for s in tracker.history],
    }
    # Sprint 46R (audit B8): use the shared atomic_write_text helper
    # so the equity state file is fsync'd before the rename. Pre-46R
    # the write+rename pattern was correct on POSIX (rename is
    # atomic) but had a power-loss window: the rename could land
    # before the data was on disk, leaving a 0-byte or stale
    # equity_state.json that the drawdown kill switch would then
    # silently clear (the exact failure mode audit A1 fixed for
    # the kill switch's own state file).
    atomic_write_text(
        p,
        json.dumps(payload, indent=2, default=str),
    )


def load_tracker(path: str, position_repo: Optional[PositionRepository] = None,
                audit=None) -> EquityTracker:
    """
    Load tracker from disk. If file doesn't exist or is corrupt, return
    a fresh tracker with starting_balance from current broker balance
    (caller can pre-set this before calling).

    Restores:
      - starting_balance
      - precision
      - max_equity (peak for drawdown calc)
      - history (up to history_size)

    Does NOT restore position_repo or audit (those are passed in).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"EquityTracker state file not found: {path}")

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"EquityTracker state file corrupt: {path}: {e}") from e

    starting_balance = float(data["starting_balance"])
    history_size = max(len(data.get("history", [])), 1)
    tracker = EquityTracker(
        starting_balance=starting_balance,
        position_repo=position_repo,
        audit=audit,
        history_size=history_size,
        precision_decimals=int(data.get("precision_decimals", 4)),
    )
    # Restore max_equity
    if "max_equity" in data:
        tracker._max_equity = float(data["max_equity"])

    # Restore history (skip the synthetic initial snapshot from __init__)
    if "history" in data and len(data["history"]) > 0:
        # Replace the auto-initialized history with the persisted one
        tracker.history.clear()
        for snap_dict in data["history"]:
            tracker.history.append(EquitySnapshot(**snap_dict))

    return tracker