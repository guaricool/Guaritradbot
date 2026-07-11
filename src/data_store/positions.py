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

    # Sprint 46I — native broker-side protection (Carlos: "quiero que
    # sea super ultra robusto", worried an hourly bot cycle could miss
    # a stop-loss/take-profit cross). When the exchange itself holds a
    # resting OCO/bracket order for this position, `protection_mode` is
    # "native_oco" and `broker_oco_order_id` identifies it — the
    # exchange enforces the stop/TP with ZERO dependency on the bot's
    # cycle timing. Default "polling" preserves the original behavior
    # (PositionMonitor compares price vs stop_loss/take_profit each
    # cycle and sends a fresh close order) for every position opened
    # before this existed, for paper mode, and for any broker/asset
    # class that doesn't support native protection (Alpaca fractional
    # shares — see src/execution/alpaca_broker.py's module docstring).
    protection_mode: str = "polling"  # "polling" | "native_oco"
    broker_oco_order_id: Optional[str] = None

    # Sprint 46J — real exchange fees. Before this, `realized_pnl` was
    # pure gross price movement (entry vs close), with zero accounting
    # for the taker fee binance.us actually charges on every market-
    # order fill. That's the exact "ignoring fees" mistake that makes a
    # bot look profitable in the dashboard while quietly losing money
    # once real trading costs are counted — especially dangerous here
    # given how small this account's positions are (fees are a bigger
    # % of a $10-20 trade than of a $10,000 one). Default 0.0 preserves
    # the original gross-only calculation for every position opened
    # before this existed and for Alpaca equities (commission-free —
    # see main.py's fee_pct_for_asset). Informational only; does not
    # change WHEN a position closes, only what P&L gets recorded.
    fees_paid_usd: float = 0.0

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
        # Sprint 46N (audit C7): set by _quarantine_corrupt_file() when
        # the on-disk file couldn't be parsed. None means the load was
        # clean (file missing or parsed fine). Callers (main.py's
        # startup sequence, the dashboard API) can check this to
        # surface a loud warning instead of silently continuing as if
        # nothing happened.
        self.load_error: Optional[str] = None
        self.quarantined_path: Optional[Path] = None
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.positions = [Position(**p) for p in data.get("positions", [])]
        except Exception as e:
            self._quarantine_corrupt_file(e)

    def _quarantine_corrupt_file(self, error: Exception) -> None:
        """Sprint 46N (audit C7): a corrupt/unparseable positions.json
        used to be silently treated as "no positions" — `self.positions`
        just stayed at its initial empty list, and the VERY NEXT write
        (`_save()`, triggered by ANY subsequent `add_open`/
        `close_position` call) would overwrite the corrupt file with
        that empty state via the atomic tmp+replace pattern below,
        permanently destroying whatever position history was in the
        corrupt file with zero chance of manual recovery — exactly the
        "silently wipes positions.json" finding.

        Now: the corrupt file's raw bytes are copied to a timestamped
        `<name>.corrupt-<epoch>` quarantine file BEFORE any write can
        touch the original, the failure is recorded on `self.load_error`
        (not just printed — so callers like main.py's startup sequence
        or the dashboard API can surface it, e.g. as a SYSTEM_ERROR
        event or a dashboard banner) and `self.positions` stays empty
        (there's no safe way to guess valid position data out of a
        corrupt file) — but the ORIGINAL bytes survive on disk for
        manual inspection/recovery instead of vanishing on the next
        save.
        """
        self.load_error = str(error)
        try:
            quarantine_path = self.path.with_name(
                f"{self.path.name}.corrupt-{int(time.time())}"
            )
            quarantine_path.write_bytes(self.path.read_bytes())
            self.quarantined_path = quarantine_path
            print(
                f"[PositionRepo] ⚠️ {self.path} está corrupto ({error}). "
                f"Copia de seguridad guardada en {quarantine_path}. "
                f"Arrancando con 0 posiciones — REVISAR MANUALMENTE si "
                f"había posiciones abiertas antes de operar."
            )
        except Exception as qe:
            print(
                f"[PositionRepo] ⚠️ {self.path} está corrupto ({error}) Y "
                f"la cuarentena también falló ({qe}). Arrancando con 0 "
                f"posiciones — REVISAR MANUALMENTE."
            )

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

    def close_position(
        self,
        position_id: str,
        close_price: float,
        reason: str,
        fee_pct: float = 0.0,
    ) -> Optional[Position]:
        """Close an open position and record realized P&L.

        Args:
            fee_pct: Sprint 46J — round-trip trading-cost fraction (e.g.
                0.001 = 0.1% one-way, charged on BOTH entry and exit
                notional since binance.us market orders are always
                taker). Default 0.0 preserves the exact original
                gross-only calculation for every existing caller —
                callers that know the asset's real fee (see main.py's
                `fee_pct_for_asset`) opt in explicitly.
        """
        for p in self.positions:
            if p.position_id == position_id and p.is_open:
                p.closed_ts = time.time()
                p.closed_price = close_price
                p.close_reason = reason
                direction_sign = 1.0 if p.direction == "long" else -1.0
                gross_pnl = direction_sign * (close_price - p.entry_price) * p.qty
                fees = 0.0
                if fee_pct:
                    entry_notional = abs(p.entry_price * p.qty)
                    exit_notional = abs(close_price * p.qty)
                    fees = (entry_notional + exit_notional) * fee_pct
                p.fees_paid_usd = fees
                p.realized_pnl = gross_pnl - fees
                self._save()
                return p
        return None

    def close_for_asset(
        self, asset: str, close_price: float, reason: str, fee_pct: float = 0.0
    ) -> List[Position]:
        closed = []
        for p in list(self.positions):
            if p.is_open and p.asset == asset:
                if self.close_position(p.position_id, close_price, reason, fee_pct=fee_pct):
                    closed.append(p)
        return closed
