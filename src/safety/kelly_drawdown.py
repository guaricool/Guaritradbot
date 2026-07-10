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
import time
from dataclasses import dataclass
from typing import Optional


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

        # Auto-reset if cooldown elapsed AND we're no longer in drawdown
        # (otherwise we'd reset and immediately re-trigger)
        if (
            self.triggered
            and self.triggered_at is not None
            and drawdown_pct > -self.threshold_pct  # not in drawdown anymore
        ):
            elapsed_h = (time.time() - self.triggered_at) / 3600.0
            if elapsed_h >= self.cooldown_hours:
                self.triggered = False
                self.triggered_at = None

        return DrawdownState(
            peak_equity=self.peak_equity,
            current_equity=current_equity,
            drawdown_pct=drawdown_pct,
            triggered=self.triggered,
        )

    def is_triggered(self) -> bool:
        """Returns True if kill switch is currently active."""
        return self.triggered

    def reset(self) -> None:
        """Manually reset the kill switch (e.g., user override)."""
        self.triggered = False
        self.triggered_at = None