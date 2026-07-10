"""
Sprint 1+18 — Mandate Gate.

Inspirado en el patrón de Vibe-Trading (mandate gate) y TradingAgents
(risk management team). Antes de ejecutar CUALQUIER trade:

1. El símbolo debe estar en el universe permitido.
2. El notional por trade debe ser <= max_position_usd.
3. El risk del trade no puede exceder la pérdida diaria permitida
   (rolling 24h, basada en P&L REALIZADO — no en risk teórico).
4. El exposure total (open positions + esta propuesta) no puede
   superar el límite del mandate.

Sprint 18 fixes (Audit Team report):
- B. Phantom Exposure: ahora se calcula desde `PositionRepository`
  (suma de notional de posiciones abiertas) en vez de acumular
  TRADE_FILLED sin restar TRADE_CLOSED del audit ledger.
- C. Punished for Trying: daily_loss ahora suma `realized_pnl` de
  posiciones CERRADAS en las últimas 24h. Si todas son ganadoras,
  daily_loss = 0 (no se castiga al bot por abrir trades buenos).

Si cualquier check falla, la propuesta se rechaza con razón explícita.
Si todo pasa, se sella con `mandate_ok=True` y la razón.

NO muta estado. Es una clase pura de validación.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Set, Optional
import math
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
    def __init__(self, config: MandateConfig, audit_ledger=None, position_repo=None):
        """
        Args:
            config: Mandate limits
            audit_ledger: optional, used as fallback if position_repo missing
            position_repo: preferred source of truth for exposure + realized PnL
        """
        self.config = config
        self.audit = audit_ledger
        self.position_repo = position_repo

    def _daily_loss_usd(self, now_ts: float | None = None) -> float:
        """
        Sprint 18 fix: sum REALIZED P&L of closed positions in the last 24h.

        Previously this summed `risk_usd` of TRADE_APPROVED events, which made
        the bot think it had LOST money every time it OPENED a trade (because
        risk_usd is theoretical, not realized). After 5 winning trades the bot
        would still kill-switch for 24h.

        Now: only count actual realized losses from closed positions.
        If you made money, daily_loss = 0. Win-win trades don't trigger the cap.
        """
        now = now_ts or time.time()
        cutoff = now - 24 * 3600

        # --- Preferred: query PositionRepository (more reliable than audit) ---
        if self.position_repo is not None:
            loss = 0.0
            for p in self.position_repo.all():
                if (
                    p.is_open is False
                    and p.closed_ts is not None
                    and p.closed_ts >= cutoff
                    and p.realized_pnl is not None
                    and p.realized_pnl < 0
                ):
                    loss += abs(p.realized_pnl)
            return loss

        # --- Fallback: read audit ledger for TRADE_CLOSED events ---
        if self.audit is None:
            return 0.0
        rows = self.audit.read_since(cutoff)
        loss = 0.0
        for r in rows:
            if r.get("event_type") == "TRADE_CLOSED":
                pnl = float(r.get("realized_pnl_usd", 0))
                if pnl < 0:
                    loss += abs(pnl)
        return loss

    def _open_exposure_usd(self) -> float:
        """
        Sprint 18 fix: read REAL open exposure from PositionRepository.

        Previously this summed TRADE_FILLED events without ever subtracting
        TRADE_CLOSED events, causing exposure to grow unboundedly until the
        bot was permanently blocked.

        Now: sum notional_usd of currently open positions. If position_repo
        is unavailable, fall back to a corrected audit scan that DOES
        subtract closes.
        """
        # --- Preferred: PositionRepository is the source of truth ---
        if self.position_repo is not None:
            return self.position_repo.total_exposure_usd()

        # --- Fallback: corrected audit ledger scan (handles closed properly) ---
        if self.audit is None:
            return 0.0
        rows = self.audit.read_all()
        # Build a map of open positions from the audit (position_id -> notional).
        open_notional: dict[str, float] = {}
        for r in rows:
            et = r.get("event_type")
            pid = r.get("position_id")
            if not pid:
                continue
            if et == "POSITION_OPENED":
                open_notional[pid] = float(r.get("notional_usd", 0))
            elif et == "TRADE_CLOSED":
                # Position is no longer open; remove from exposure.
                open_notional.pop(pid, None)
        return sum(open_notional.values())

    def validate(self, trade_proposal: dict) -> MandateVerdict:
        if not self.config.enabled:
            return MandateVerdict(ok=True, reason="mandate_disabled")

        asset = trade_proposal.get("asset", "")
        notional = float(trade_proposal.get("notional_usd", 0.0))
        risk = float(trade_proposal.get("risk_usd", 0.0))

        # Sprint 43 C3 fix: reject NaN/Inf BEFORE running the cap checks.
        # Python's `NaN > x` returns False, so a NaN notional would
        # silently pass all 3 caps (per-trade, daily loss, total exposure)
        # and the mandate would approve a trade whose size is undefined.
        # This is a fail-open vulnerability: the audit agent could be
        # convinced the mandate is working, while in reality every check
        # returns False and the proposal slips through.
        if not (math.isfinite(notional) and math.isfinite(risk)):
            return MandateVerdict(
                ok=False,
                reason=f"non_finite_notional_or_risk:notional={notional!r},risk={risk!r}",
            )

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

        # 3. Daily loss rolling 24h (REALIZED P&L — Sprint 18 fix)
        daily_loss = self._daily_loss_usd()
        if daily_loss + risk > self.config.max_daily_loss_usd:
            return MandateVerdict(
                ok=False,
                reason=f"daily_loss_cap:${daily_loss + risk:.2f}>${self.config.max_daily_loss_usd:.2f}",
                daily_loss_so_far_usd=daily_loss,
            )

        # 4. Total exposure (open positions + este trade) — REAL exposure (Sprint 18 fix)
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