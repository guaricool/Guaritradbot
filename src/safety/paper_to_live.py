"""
Sprint 22 — Paper-to-Live Transition Safe Mode.

El bot actualmente (pre-Sprint 22) hace transición paper → live sin
ningún guard. Eso es peligroso:

1. Si hay posiciones paper abiertas en `data_store/positions.json`,
   el bot cree que las tiene pero NO existen en el exchange real.
2. PositionMonitor intentará cerrar las posiciones al SL/TP → orden
   de venta de un asset que NO tienes en live.
3. MandateGate calcula exposure sobre posiciones paper → puede
   rechazar señales legítimas o permitir overexposure.
4. daily_loss se calcula sobre P&L paper → no corresponde con el
   exchange real.

Este módulo añade un checklist que:

1. **Verifica conectividad del broker live** (antes de cualquier
   transición).
2. **Detecta posiciones paper abiertas**.
3. **Pide confirmación al usuario** (o auto-actúa según flag) con
   3 opciones: `close` / `ignore` / `abort`.
4. **Loggea la transición al audit** (evento `LIVE_TRANSITION_*`).
5. **Dry-run mode**: permite enviar órdenes con qty mínima para
   validar el flujo sin riesgo financiero real.

Tres principios:
- **No surprises**: el usuario ve exactamente qué va a pasar antes de que pase.
- **Forenseable**: cada decisión queda registrada en el audit ledger.
- **Reversible**: el dry-run permite abortar sin perder dinero.
"""
from __future__ import annotations
import os
import time
from typing import Optional, List

from src.data_store.positions import PositionRepository, Position


# Minimum order qty for dry-run validation trades on binance.us (BTC is 0.00001).
DRY_RUN_MIN_QTY = 0.00001


class TransitionDecision:
    """Container for the result of `PaperToLiveChecklist.run()`."""

    def __init__(
        self,
        proceed: bool,
        reason: str,
        paper_positions_closed: int = 0,
        broker_balance: Optional[float] = None,
        broker_connected: bool = False,
        dry_run_validated: bool = False,
    ):
        self.proceed = proceed
        self.reason = reason
        self.paper_positions_closed = paper_positions_closed
        self.broker_balance = broker_balance
        self.broker_connected = broker_connected
        self.dry_run_validated = dry_run_validated

    def __repr__(self):
        return (
            f"TransitionDecision(proceed={self.proceed}, reason='{self.reason}', "
            f"closed={self.paper_positions_closed}, balance=${self.broker_balance}, "
            f"connected={self.broker_connected}, dry_run={self.dry_run_validated})"
        )


class PaperToLiveChecklist:
    """
    Pre-flight checklist for paper → live transition.

    Usage:
        checklist = PaperToLiveChecklist(
            position_repo=position_repo,
            audit=audit,
            broker=broker_client,  # configured with use_testnet=False for live
            interactive=True,       # ask user
        )
        decision = checklist.run()
        if not decision.proceed:
            raise SystemExit(f"Live transition aborted: {decision.reason}")
        # safe to enable mandate.enabled=True now
    """

    def __init__(
        self,
        position_repo: PositionRepository,
        audit=None,
        broker=None,
        interactive: bool = True,
        auto_action: str = "abort",  # "close" | "ignore" | "abort" (when interactive=False)
        min_order_qty: float = DRY_RUN_MIN_QTY,
    ):
        self.repo = position_repo
        self.audit = audit
        self.broker = broker
        self.interactive = interactive
        self.auto_action = auto_action
        self.min_order_qty = min_order_qty

    # === public API ===
    def run(self, dry_run: bool = True) -> TransitionDecision:
        """
        Execute the full checklist.

        Returns a TransitionDecision indicating whether it's safe to
        proceed to live mode. The caller is responsible for actually
        flipping `mandate.enabled = true` afterwards.
        """
        # 1. Broker connectivity check
        balance = self._check_broker_connection()
        if balance is None:
            return TransitionDecision(
                proceed=False,
                reason="broker_unreachable",
                broker_connected=False,
            )

        # 2. Count paper positions
        open_paper = self._count_paper_positions()
        if self.audit is not None:
            self.audit.append("LIVE_TRANSITION_CHECK", {
                "open_paper_positions": open_paper,
                "broker_balance_usd": balance,
                "interactive": self.interactive,
                "auto_action": self.auto_action,
            })

        # 3. Handle paper positions
        closed_count = 0
        if open_paper > 0:
            if self.interactive:
                choice = self._prompt_user(open_paper)
            else:
                choice = self.auto_action

            if choice == "abort":
                return TransitionDecision(
                    proceed=False,
                    reason=f"user_aborted_with_{open_paper}_paper_positions",
                    broker_balance=balance,
                    broker_connected=True,
                )
            elif choice == "close":
                closed_count = self._close_paper_positions(reason="PRE_LIVE_CLOSE")
            elif choice == "ignore":
                if self.audit is not None:
                    self.audit.append("LIVE_TRANSITION_PAPER_IGNORED", {
                        "open_paper_positions": open_paper,
                        "warning": "Paper positions remain in repo but do not exist in live exchange",
                    })
            else:
                return TransitionDecision(
                    proceed=False,
                    reason=f"unknown_choice:{choice}",
                    broker_balance=balance,
                    broker_connected=True,
                )

        # 4. Dry-run validation (optional but recommended)
        dry_run_ok = False
        if dry_run:
            dry_run_ok = self._validate_dry_run()
            if not dry_run_ok:
                return TransitionDecision(
                    proceed=False,
                    reason="dry_run_validation_failed",
                    broker_balance=balance,
                    broker_connected=True,
                    paper_positions_closed=closed_count,
                )

        # 5. Final approval
        if self.audit is not None:
            self.audit.append("LIVE_TRANSITION_APPROVED", {
                "broker_balance_usd": balance,
                "paper_positions_closed": closed_count,
                "open_paper_after": self._count_paper_positions(),
                "dry_run_validated": dry_run_ok,
            })

        return TransitionDecision(
            proceed=True,
            reason="all_checks_passed",
            broker_balance=balance,
            broker_connected=True,
            paper_positions_closed=closed_count,
            dry_run_validated=dry_run_ok,
        )

    # === internal helpers ===
    def _check_broker_connection(self) -> Optional[float]:
        """
        Try to fetch the live balance from the broker. Returns the
        balance in USDT/USD or None if unreachable.
        """
        if self.broker is None:
            print("[Checklist] ❌ No broker configured. Aborting.")
            return None
        try:
            balance = self.broker.get_usdt_balance()
            if balance is None or balance <= 0:
                print(f"[Checklist] ❌ Broker returned invalid balance: {balance}")
                return None
            print(f"[Checklist] ✅ Broker connected. Balance: ${balance:.2f}")
            return float(balance)
        except Exception as e:
            print(f"[Checklist] ❌ Broker connection failed: {e}")
            return None

    def _count_paper_positions(self) -> int:
        """Count currently-open positions in the repo."""
        return self.repo.count_open()

    def _prompt_user(self, open_paper: int) -> str:
        """
        Interactive prompt asking what to do with paper positions.

        Options:
        - close: mark all open positions as closed (simulated P&L 0)
        - ignore: proceed with live, but log a WARNING about the discrepancy
        - abort: do not proceed to live
        """
        print(
            f"\n⚠️  LIVE TRANSITION CHECKLIST\n"
            f"   {open_paper} paper positions detected in repo.\n"
            f"   These DO NOT exist on the live exchange.\n"
        )
        print("What should we do with these positions?")
        print(f"  [C]lose all (mark as closed in repo, simulated P&L)")
        print(f"  [I]gnore (proceed with live; bot will track them but they don't exist on exchange)")
        print(f"  [A]bort (do NOT proceed to live)")
        try:
            choice = input("\nChoice (C/I/A): ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            print("\n[Checklist] No TTY. Defaulting to ABORT for safety.")
            return "abort"

        if choice in ("C", "CLOSE"):
            return "close"
        elif choice in ("I", "IGNORE"):
            return "ignore"
        elif choice in ("A", "ABORT"):
            return "abort"
        else:
            print(f"[Checklist] Unrecognized choice '{choice}'. Aborting.")
            return "abort"

    def _close_paper_positions(self, reason: str = "PRE_LIVE_CLOSE") -> int:
        """
        Mark all open positions as closed at their entry_price (simulated
        zero P&L). This is safe because paper positions don't have a
        real exchange counterpart to settle against.

        Returns the number of positions closed.
        """
        closed_count = 0
        for pos in list(self.repo.open()):
            # Close at entry_price — paper positions, no real P&L realized
            closed = self.repo.close_position(
                pos.position_id,
                close_price=pos.entry_price,
                reason=reason,
            )
            if closed is not None:
                closed_count += 1
                if self.audit is not None:
                    self.audit.append("PAPER_POSITION_CLOSED_PRE_LIVE", {
                        "position_id": closed.position_id,
                        "asset": closed.asset,
                        "entry_price": closed.entry_price,
                        "reason": reason,
                    })
        print(f"[Checklist] Closed {closed_count} paper positions.")
        return closed_count

    def _validate_dry_run(self) -> bool:
        """
        Sprint 43 H4 fix: validate the live broker connection WITHOUT
        placing a real order. The previous version called
        `broker.create_market_order("BTC/USDT", "buy", min_order_qty)`
        and then never closed the position or registered it via
        `PositionRepository.add_open()`. Result: a few cents of
        real BTC was bought but never sold, and the position was
        invisible to the bot's repo — neither the mandate gate
        nor the position monitor tracked it. The audit called
        this "un test destructivo sin rollback".

        Now: use a read-only API call (`get_usdt_balance`) to
        verify auth + connectivity. No position is opened, no
        order is sent. If the broker needs more than a balance
        read to validate (some Alpaca accounts require an
        initial trade to fully activate), the operator can
        opt into the legacy destructive path with the
        `dry_run_placement: true` flag in `live_transition` config.

        Returns True if the dry-run succeeded.
        """
        if self.broker is None:
            return False
        # Sprint 43 H4: skip the destructive test by default.
        # Set live_transition.dry_run_placement = true in config
        # to opt into the legacy "place and forget" behavior
        # (NOT recommended — kept for backward compat only).
        legacy_placement = False
        try:
            cfg = getattr(self, "_config", None) or {}
            legacy_placement = bool(
                cfg.get("live_transition", {}).get("dry_run_placement", False)
            )
        except Exception:
            legacy_placement = False

        if legacy_placement:
            return self._legacy_destructive_dry_run()

        try:
            # Read-only validation: does the broker respond with a
            # valid balance? This exercises auth, connectivity, and
            # the JSON parsing of ccxt/Alpaca responses — all the
            # things that the actual order path would fail on.
            bal = self.broker.get_usdt_balance()
            if bal is None or (isinstance(bal, float) and not (bal >= 0)):
                print(f"[Checklist] ❌ Dry-run failed: invalid balance {bal!r}")
                if self.audit is not None:
                    self.audit.append("DRY_RUN_FAILED", {
                        "reason": "invalid_balance",
                        "balance": bal,
                    })
                return False
            print(
                f"[Checklist] ✅ Dry-run succeeded (read-only): "
                f"balance=${bal:.2f}"
            )
            if self.audit is not None:
                self.audit.append("DRY_RUN_OK", {
                    "method": "read_only",
                    "balance_usd": bal,
                })
            return True
        except Exception as e:
            print(f"[Checklist] ❌ Dry-run exception: {e}")
            if self.audit is not None:
                self.audit.append("DRY_RUN_EXCEPTION", {"error": str(e)[:200]})
            return False

    def _legacy_destructive_dry_run(self) -> bool:
        """
        BACKWARD-COMPATIBLE destructive dry-run. Sprint 43 H4
        keeps this path for operators who explicitly opt in via
        config (live_transition.dry_run_placement = true), but
        the default is the read-only path in _validate_dry_run.

        The audit's complaint: this method places a real market
        buy on BTC/USDT, never closes the position, never
        registers it via PositionRepository.add_open(). The
        result is a few cents of real BTC bought and forgotten.
        The new default avoids this; this method is only called
        if the operator has explicitly accepted the risk.
        """
        if self.broker is None:
            return False
        try:
            symbol = "BTC/USDT"
            print(
                f"[Checklist] ⚠️ LEGACY destructive dry-run: "
                f"placing real order on {symbol} qty={self.min_order_qty}. "
                f"This is opt-in only — set live_transition.dry_run_placement=true."
            )
            result = self.broker.create_market_order(symbol, "buy", self.min_order_qty)
            if result is None or result.get("status") == "failed":
                print(f"[Checklist] ❌ Dry-run failed: {result}")
                if self.audit is not None:
                    self.audit.append("DRY_RUN_FAILED", {
                        "result": str(result)[:200] if result else "None",
                    })
                return False
            print(
                f"[Checklist] ⚠️ Dry-run succeeded but position is now OPEN and UNREGISTERED. "
                f"Order id: {result.get('id', '?')}. Manually close the position."
            )
            if self.audit is not None:
                self.audit.append("DRY_RUN_LEGACY_PLACED_ORDER", {
                    "symbol": symbol,
                    "qty": self.min_order_qty,
                    "order_id": str(result.get("id", "?")),
                    "warning": "position_not_registered_in_repo",
                })
            return True
        except Exception as e:
            print(f"[Checklist] ❌ Dry-run exception: {e}")
            if self.audit is not None:
                self.audit.append("DRY_RUN_EXCEPTION", {"error": str(e)[:200]})
            return False


def run_preflight(
    config: dict,
    position_repo: PositionRepository,
    audit,
    broker,
    interactive: bool = True,
) -> TransitionDecision:
    """
    Convenience entrypoint: build and run the checklist with sensible
    defaults. Called from main.py when transitioning to live.

    Args:
        config: full config dict (for logging context)
        position_repo: shared repo instance
        audit: shared audit ledger
        broker: broker client (configured with use_testnet=False for live)
        interactive: whether to prompt the user
    """
    checklist = PaperToLiveChecklist(
        position_repo=position_repo,
        audit=audit,
        broker=broker,
        interactive=interactive,
        auto_action=config.get("live_transition", {}).get("auto_action", "abort"),
        min_order_qty=config.get("live_transition", {}).get("dry_run_qty", DRY_RUN_MIN_QTY),
    )
    return checklist.run(dry_run=True)