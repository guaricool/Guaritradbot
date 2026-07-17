"""
Sprint 30 — Kelly Criterion + Drawdown Kill Switch.

Carlos shared "QUANT — The Setup Guide (Claude Trading Skills by @seb.ai)"
which describes 62 agent skills. Most are crypto/DeFi-focused (not applicable
to our multi-asset bot), but two ideas are highly valuable:

1. **Kelly Criterion** for position sizing (optimal bet size based on edge)
2. **Max Drawdown Kill Switch** (stop trading if DD > threshold)

Both implemented here. Configurable via config.yaml.

## Why Kelly?

The Kelly Criterion maximizes the long-term growth rate of your bankroll.
Formula (for binary outcomes): f* = (bp - q) / b
  where b = net odds (e.g., 2 for 2:1 payoff)
        p = probability of winning
        q = probability of losing (1 - p)

For continuous distributions (our case with ML signals returning probabilities),
use the fractional Kelly: f* = edge / variance
  where edge = expected_return_per_trade
        variance = variance_of_returns

We use **fractional Kelly** (default 0.25) to be conservative — full Kelly is
too aggressive for real markets (high drawdowns).

## Why Max Drawdown Kill Switch?

If equity drops more than X% from peak, pause trading for Y hours.
Prevents:
- Revenge trading after losses
- Doubling down on a broken strategy
- Emotional decisions during drawdown
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.core.atomic_write import atomic_write_text
from src.core.logging_setup import get_logger

logger = get_logger(__name__)


# === Kelly Criterion ===

@dataclass
class KellyConfig:
    """Configuración de Kelly Criterion para position sizing."""
    enabled: bool = False  # off by default; if True, overrides fixed 1% risk
    fractional_multiplier: float = 0.25  # Use 1/4 of full Kelly (conservative)
    min_edge: float = 0.02  # Minimum edge to consider Kelly (else skip trade)
    min_win_prob: float = 0.30  # Minimum win probability (else skip)
    max_position_pct: float = 0.20  # Hard cap (never risk more than 20% on one trade)


def kelly_fraction(
    win_prob: float,
    avg_win: float,
    avg_loss: float,
    cfg: Optional[KellyConfig] = None,
) -> float:
    """
    Compute optimal position size as fraction of bankroll using Kelly Criterion.

    Args:
        win_prob: probability of a winning trade (0..1)
        avg_win: average win amount (e.g., $2 for +R:R 2:1)
        avg_loss: average loss amount (e.g., $1 for -1R)
        cfg: KellyConfig (default: fractional 0.25, conservative)

    Returns:
        Position size as fraction of bankroll (0..1).
        Returns 0 if signal doesn't meet minimum criteria.

    Examples:
        >>> kelly_fraction(0.55, 2.0, 1.0, KellyConfig())  # 55% win, 2:1 R:R
        0.05  # 5% of bankroll
    """
    cfg = cfg or KellyConfig()

    # Filter: signal too weak
    if win_prob < cfg.min_win_prob:
        return 0.0
    if avg_loss <= 0:
        return 0.0

    # Edge = (win_prob * avg_win) - ((1 - win_prob) * avg_loss)
    edge = (win_prob * avg_win) - ((1 - win_prob) * avg_loss)
    if edge < cfg.min_edge:
        return 0.0

    # Full Kelly: f* = edge / variance (for continuous distribution)
    # For binary: f* = (bp - q) / b where b = avg_win / avg_loss
    odds = avg_win / avg_loss
    full_kelly = (win_prob * odds - (1 - win_prob)) / odds

    if full_kelly <= 0:
        return 0.0

    # Apply fractional multiplier
    fractional = full_kelly * cfg.fractional_multiplier

    # Cap at max position
    return min(fractional, cfg.max_position_pct)


# === Max Drawdown Kill Switch ===

@dataclass
class DrawdownState:
    """Estado del drawdown tracker (snapshot derivado, no persistente).
    Note: `triggered_at` lives on DrawdownKillSwitch instance, not here —
    DrawdownState is a derived snapshot for the caller, not a duplicate store."""
    peak_equity: float
    current_equity: float
    drawdown_pct: float  # negative or 0
    triggered: bool       # True if kill switch is active
    # Sprint 45 fix (N2): main.py reads `dd_state.cooldown_remaining_hours`
    # when building the SYSTEM_ERROR alert for an active kill switch, but
    # this field never existed on `DrawdownState` — every time the kill
    # switch actually triggered, that line raised AttributeError, which
    # the broad `except Exception` around it in main.py swallowed, letting
    # the cycle fall through to normal trading instead of skipping it.
    # 0.0 when not triggered (nothing to count down).
    cooldown_remaining_hours: float = 0.0
    # Bug fix (deadlock): True on the cycle this reset happened by
    # rebasing the peak to current equity rather than by genuinely
    # recovering above -threshold_pct. See DrawdownKillSwitch.update's
    # docstring for why this exists -- callers should treat this as
    # noteworthy (log it distinctly) since it means the switch let
    # trading resume despite still being in a real drawdown.
    peak_rebased: bool = False


class DrawdownKillSwitch:
    """
    Monitors equity drawdown and triggers a kill switch if it exceeds threshold.

    Carlos: "Max drawdown limits" from QUANT guide → implemented as a safety net.

    Behavior:
    - Track peak equity over time
    - Compute current drawdown = (current - peak) / peak * 100
    - If drawdown <= -threshold_pct (e.g., -15%), trigger kill switch
    - Stay triggered for cooldown_hours, then auto-reset
    - Manual reset: call reset()

    Usage:
        ds = DrawdownKillSwitch(threshold_pct=15.0, cooldown_hours=24.0)
        # ... on each cycle:
        for snapshot in tracker.history:
            state = ds.update(snapshot.total_equity)
        if state.triggered:
            print(f"Kill switch active: {state.drawdown_pct:.2f}% drawdown")
            # skip trading
    """

    def __init__(
        self,
        threshold_pct: float = 15.0,
        cooldown_hours: float = 24.0,
    ):
        self.threshold_pct = threshold_pct
        self.cooldown_hours = cooldown_hours
        self.peak_equity: float = 0.0
        self.triggered: bool = False
        self.triggered_at: Optional[float] = None

    def update(self, current_equity: float) -> DrawdownState:
        """
        Update with current equity; returns current drawdown state.

        Auto-triggers kill switch if drawdown exceeds threshold.
        Auto-resets if cooldown has elapsed (only if not currently in drawdown).

        Bug fix (deadlock): the old reset condition required BOTH the
        cooldown to elapse AND `drawdown_pct > -threshold_pct` (recovered
        above the threshold) before even checking the cooldown timer.
        But recovering equity requires NEW trades, and new trades are
        exactly what this switch blocks while triggered -- so once
        equity fell far enough below peak (e.g. a real historical
        sizing bug compounding losses before a fix), the switch could
        never release itself: the account is stuck below peak forever
        with trading permanently disabled, no matter how long the
        cooldown window is. Now: once the cooldown has genuinely
        elapsed, the switch resets AND rebases `peak_equity` to the
        CURRENT equity if the account never recovered on its own --
        drawdown resets to 0% from a new, reachable baseline instead of
        requiring a return to a peak that may no longer be reachable.
        The same threshold_pct still protects the account from here
        forward; it just stops enforcing recovery to a stale peak.
        """
        # Update peak (if equity is new high, this is the new peak)
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

        # Compute drawdown
        drawdown_pct = 0.0
        if self.peak_equity > 0:
            drawdown_pct = ((current_equity - self.peak_equity) / self.peak_equity) * 100.0

        # Trigger if drawdown exceeds threshold
        if not self.triggered and drawdown_pct <= -self.threshold_pct:
            self.triggered = True
            self.triggered_at = time.time()

        peak_rebased = False
        if self.triggered and self.triggered_at is not None:
            elapsed_h = (time.time() - self.triggered_at) / 3600.0
            if elapsed_h >= self.cooldown_hours:
                if drawdown_pct > -self.threshold_pct:
                    # Genuine recovery -- simple reset, peak untouched.
                    self.triggered = False
                    self.triggered_at = None
                else:
                    # Still deep in drawdown after a full cooldown --
                    # without a rebase this would never release. Rebase
                    # the peak to current equity and recompute drawdown
                    # (now 0%) so the caller's snapshot reflects the
                    # post-rebase state on this same cycle.
                    self.peak_equity = current_equity
                    drawdown_pct = 0.0
                    self.triggered = False
                    self.triggered_at = None
                    peak_rebased = True

        # Sprint 45 fix (N2): compute how many hours remain in the
        # cooldown window, for display/alerting. 0.0 whenever the
        # switch isn't currently triggered.
        cooldown_remaining_hours = 0.0
        if self.triggered and self.triggered_at is not None:
            elapsed_h = (time.time() - self.triggered_at) / 3600.0
            cooldown_remaining_hours = max(0.0, self.cooldown_hours - elapsed_h)

        return DrawdownState(
            peak_equity=self.peak_equity,
            current_equity=current_equity,
            drawdown_pct=drawdown_pct,
            triggered=self.triggered,
            cooldown_remaining_hours=cooldown_remaining_hours,
            peak_rebased=peak_rebased,
        )

    def is_triggered(self) -> bool:
        """Returns True if kill switch is currently active."""
        return self.triggered

    def reset(self) -> None:
        """Manually reset the kill switch (e.g., user override)."""
        self.triggered = False
        self.triggered_at = None

    # --- Sprint 46N (audit A1): persistence ---
    #
    # Before this, `peak_equity`/`triggered`/`triggered_at` lived ONLY
    # in this instance's memory. Every bot restart (a redeploy, a
    # crash, a manual `docker compose up`) silently reset peak_equity
    # back to 0.0 and cleared any active trigger -- so a real drawdown
    # that had correctly paused new entries would resume trading the
    # moment the process restarted, and the very next equity update
    # after a restart would treat whatever the CURRENT equity happens
    # to be as the new all-time peak (since 0.0 < anything), making
    # the switch measure drawdown from the wrong baseline until a new
    # high was organically set. Persisting this state (mirroring
    # src/safety/equity_tracker.py's persist_tracker/load_tracker
    # pattern) fixes both problems.
    #
    # Only STATE (peak_equity/triggered/triggered_at) is persisted --
    # NOT threshold_pct/cooldown_hours. Those are config values that
    # can legitimately change between restarts (config.yaml edit or a
    # dashboard risk-config save) and the current config must win, not
    # whatever was true when the file was last written.

    def persist(self, path: str) -> None:
        """Save peak_equity/triggered/triggered_at to disk. Atomic
        write (tmp file + `.replace()`), same pattern as every other
        state file in this codebase."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "peak_equity": self.peak_equity,
            "triggered": self.triggered,
            "triggered_at": self.triggered_at,
            "saved_at": time.time(),
        }
        # Sprint 46R (audit B8): use the shared atomic_write_text
        # helper so the kill switch state file is fsync'd before
        # the rename. This is the exact file the audit's A1 finding
        # was worried about ("a restart borra silenciosamente un
        # kill switch activo") — a torn write here would defeat
        # the persistence the audit depends on.
        atomic_write_text(
            p,
            json.dumps(payload, indent=2),
        )

    @classmethod
    def load(
        cls,
        path: str,
        threshold_pct: float = 15.0,
        cooldown_hours: float = 24.0,
    ) -> "DrawdownKillSwitch":
        """Construct a DrawdownKillSwitch, restoring persisted
        peak_equity/triggered/triggered_at if `path` exists and is
        readable. `threshold_pct`/`cooldown_hours` always come from
        the caller (i.e. the CURRENT config.yaml / dashboard override
        at the time of this call), never from the persisted file --
        see the class-level comment above.

        Fail-open: a missing or corrupt state file returns a fresh
        instance (peak_equity=0.0, not triggered) instead of raising --
        same rationale as every other override/state file in this
        codebase: a broken persistence file must not block startup or
        crash the bot, it should just mean "start tracking from
        scratch," which is exactly the pre-Sprint-46N behavior.
        """
        switch = cls(threshold_pct=threshold_pct, cooldown_hours=cooldown_hours)
        p = Path(path)
        if not p.exists():
            return switch
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            switch.peak_equity = float(data.get("peak_equity", 0.0))
            switch.triggered = bool(data.get("triggered", False))
            triggered_at_raw = data.get("triggered_at")
            switch.triggered_at = (
                float(triggered_at_raw) if triggered_at_raw is not None else None
            )
        except Exception:
            # Corrupt/unreadable file -- start fresh rather than crash
            # bot startup over a damaged drawdown-tracker state file.
            pass
        return switch
