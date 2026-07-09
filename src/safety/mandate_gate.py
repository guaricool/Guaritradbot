"""
Sprint 1 — Mandate Gate.

Inspirado en el patrón de Vibe-Trading (mandate gate) y TradingAgents
(risk management team). Antes de ejecutar CUALQUIER trade:

1. El símbolo debe estar en el universe permitido.
2. El notional por trade debe ser <= max_position_usd.
3. El risk del trade no puede exceder la pérdida diaria permitida
   (rolling 24h, basada en audit ledger).
4. El exposure total (open positions + esta propuesta) no puede
   superar el límite del mandate.

Si cualquier check falla, la propuesta se rechaza con razón explícita.
Si todo pasa, se sella con `mandate_ok=True` y la razón.

NO muta estado. Es una clase pura de validación.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Set
import time


@dataclass
class MandateConfig:
    enabled: bool = False
    allowed_symbols: Set[str] = field(default_factory=set)
    max_position_usd: float = 20.0
    max_daily_loss_usd: float = 5.0
    max_total_exposure_usd: float = 100.0


@dataclass
class MandateVerdict:
    ok: bool
    reason: str = ""
    daily_loss_so_far_usd: float = 0.0
    open_exposure_usd: float = 0.0


class MandateGate:
    def __init__(self, config: MandateConfig, audit_ledger=None):
        self.config = config
        self.audit = audit_ledger  # para calcular daily loss rolling

    def _daily_loss_usd(self, now_ts: float | None = None) -> float:
        """
        Suma el risk de todas las trades colocadas en las últimas 24h
        (no P&L real todavía — eso requiere fills). Lo leemos del audit.
        """
        if self.audit is None:
            return 0.0
        now = now_ts or time.time()
        cutoff = now - 24 * 3600
        rows = self.audit.read_since(cutoff)
        return sum(float(r.get("risk_usd", 0.0)) for r in rows if r.get("event_type") == "TRADE_APPROVED")

    def _open_exposure_usd(self) -> float:
        """Lee open positions del audit (Sprint 1: lo más simple posible)."""
        if self.audit is None:
            return 0.0
        # TRADE_OPEN aumenta exposure; TRADE_CLOSE/TP/SL la reduce
        # (en Sprint 1 sólo emitimos TRADE_APPROVED + TRADE_FILLED, simplificamos)
        rows = self.audit.read_all()
        exposure = 0.0
        for r in rows:
            if r.get("event_type") == "TRADE_FILLED":
                side = r.get("direction", "long")
                qty = float(r.get("filled_qty", 0))
                price = float(r.get("fill_price", 0))
                notional = qty * price
                exposure += notional if side == "long" else -notional
        return abs(exposure)

    def validate(self, trade_proposal: dict) -> MandateVerdict:
        if not self.config.enabled:
            return MandateVerdict(ok=True, reason="mandate_disabled")

        asset = trade_proposal.get("asset", "")
        notional = float(trade_proposal.get("notional_usd", 0.0))
        risk = float(trade_proposal.get("risk_usd", 0.0))

        # 1. Universe
        if self.config.allowed_symbols and asset not in self.config.allowed_symbols:
            return MandateVerdict(
                ok=False,
                reason=f"symbol_not_allowed:{asset}",
            )

        # 2. Per-trade size
        if notional > self.config.max_position_usd:
            return MandateVerdict(
                ok=False,
                reason=f"notional_exceeds_max:${notional:.2f}>${self.config.max_position_usd:.2f}",
            )

        # 3. Daily loss rolling 24h
        daily_loss = self._daily_loss_usd()
        if daily_loss + risk > self.config.max_daily_loss_usd:
            return MandateVerdict(
                ok=False,
                reason=f"daily_loss_cap:${daily_loss + risk:.2f}>${self.config.max_daily_loss_usd:.2f}",
                daily_loss_so_far_usd=daily_loss,
            )

        # 4. Total exposure (open positions + este trade)
        open_exp = self._open_exposure_usd()
        projected = open_exp + notional
        if projected > self.config.max_total_exposure_usd:
            return MandateVerdict(
                ok=False,
                reason=f"exposure_cap:${projected:.2f}>${self.config.max_total_exposure_usd:.2f}",
                open_exposure_usd=open_exp,
            )

        return MandateVerdict(
            ok=True,
            reason="all_checks_passed",
            daily_loss_so_far_usd=daily_loss,
            open_exposure_usd=open_exp,
        )
