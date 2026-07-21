"""
Sprint 44B — Asset allocation policy: target weights + drift enforcement.

`sset_class.py` answers "what class is this symbol in?". THIS module
answers "how much of the portfolio should be in each class?".

The BlackRock-style portfolio builder (prompt 5, gap #1) defines
allocation as a target weight per category. The bot can then:

  1. Compute current actual weights from open positions.
  2. Compare to the target policy.
  3. Reject new trades that would push a class outside its
     target +/- drift_tolerance.
  4. Optionally recommend rebalancing trades.

The drift gate is the formal version of the Sprint 44A concentration
gate. Instead of a hard "no more than 60% in crypto", we now have
"target 40% crypto, drift tolerance 5% → reject if this trade takes
crypto to >45%". Different philosophy, same protective intent.

Sprint 44B Tier 2 (BlackRock #1, #5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.data.asset_class import get_asset_class, AssetClass


# ----------------------------------------------------------------------
# Policy schema
# ----------------------------------------------------------------------

@dataclass
class AllocationPolicy:
    """Target weights per asset class.

    Targets should sum to ~1.0 (small floating drift OK). A class
    with target 0.0 means "we never want exposure here" — useful
    for keeping FIXED_INCOME or COMMODITY_AGRI at zero until the
    bot adds those symbols.

    `drift_tolerance_pct` is the per-class allowed deviation from
    target before the gate rejects a new trade. Default 5% means
    if crypto target is 40% and the new trade would take it to 46%,
    the trade is rejected (exceeds 40% + 5% = 45% cap).

    `enabled=False` disables the drift gate entirely (the 44A
    concentration gate still runs as a hard backstop).

    `small_account_threshold_usd` (Sprint 47A / audit M15 Option B):
    if the total notional of the current positions PLUS the proposed
    trade is below this dollar amount, the drift gate is skipped
    (returns ok=True with reason "small_account_policy_skipped").
    Rationale: with a $20-100 account and the $10 minimum order, the
    44B drift policy (40% crypto, 40% equity, 10% commodities) is
    structurally impossible — a single position is already >=50% of
    the book. The audit's recommendation: for small accounts, mono-
    position IS the correct behavior, and the concentration cap
    (44A, 60%) is sufficient backstop. Default 50.0; set to 0 to
    disable the bypass (always enforce the drift policy).
    """
    targets: Dict[str, float] = field(default_factory=dict)
    drift_tolerance_pct: float = 5.0
    enabled: bool = True
    small_account_threshold_usd: float = 50.0

    def __post_init__(self):
        # Validate at construction time so misconfigured configs fail
        # loudly at startup, not silently at trade time.
        if not self.enabled:
            # Disabled policy is a sentinel; targets and drift are unused.
            return
        total = sum(self.targets.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(
                f"AllocationPolicy targets must sum to 1.0; got {total:.4f} "
                f"from {self.targets}"
            )
        for cls, w in self.targets.items():
            if w < 0:
                raise ValueError(f"AllocationPolicy target for {cls} is negative: {w}")
            if w > 1:
                raise ValueError(f"AllocationPolicy target for {cls} > 1.0: {w}")
        if self.drift_tolerance_pct < 0 or self.drift_tolerance_pct > 50:
            raise ValueError(
                f"drift_tolerance_pct must be in [0, 50]; got {self.drift_tolerance_pct}"
            )
        if self.small_account_threshold_usd < 0:
            raise ValueError(
                f"small_account_threshold_usd must be >= 0; got {self.small_account_threshold_usd}"
            )

    def target_for(self, asset_class: str) -> float:
        """Target weight for an asset class. 0.0 if not in the policy."""
        return self.targets.get(asset_class, 0.0)

    def cap_for(self, asset_class: str) -> float:
        """Maximum allowed weight for an asset class (target + drift)."""
        return min(1.0, self.target_for(asset_class) + self.drift_tolerance_pct / 100.0)

    def floor_for(self, asset_class: str) -> float:
        """Minimum allowed weight for an asset class (target - drift)."""
        return max(0.0, self.target_for(asset_class) - self.drift_tolerance_pct / 100.0)


# Default policy tuned for the bot's current universe (BTC/ETH/SOL +
# SPY/QQQ + GLD/USO + EURUSD/GBPUSD/USDJPY/USDCAD/AUDUSD). A balanced
# book that doesn't over-concentrate in any one class.
#
# FOREX was missing from this dict when OANDA was added as a broker
# (Sprint: forex integration) — `target_for()` silently defaulted it to
# 0.0, so `cap_for(forex)` was just `0 + drift_tolerance_pct` (10%).
# Every real forex trade sizes to well over 10% of a small paper book,
# so ALLOCATION_POLICY_BLOCKED rejected every single forex hypothesis
# regardless of scalp mode, hypothesis quality, or account size.
DEFAULT_POLICY = AllocationPolicy(
    targets={
        AssetClass.CRYPTO.value: 0.30,
        AssetClass.EQUITY_GROWTH.value: 0.30,
        AssetClass.COMMODITY_SAFE.value: 0.10,
        AssetClass.COMMODITY_ENERGY.value: 0.10,
        AssetClass.FOREX.value: 0.20,
    },
    drift_tolerance_pct=10.0,   # +/- 10% per class — same effect as 60% cap on crypto
    enabled=True,
)


# ----------------------------------------------------------------------
# Current exposure → actual weights
# ----------------------------------------------------------------------

def current_actual_weights(
    positions,
) -> Dict[str, float]:
    """Compute the current actual weights per asset class from a list of positions.

    Args:
        positions: any iterable of objects with `.asset` and
            `.notional_usd` (e.g. Position objects from
            src.data_store.positions). Empty positions → empty dict.

    Returns:
        Dict of {asset_class: weight}, weights sum to 1.0 (or are
        all 0.0 if there are no positions). CASH is included if any
        position is unknown — but CASH is usually parked capital, not
        exposure. Caller can filter it.
    """
    out: Dict[str, float] = {}
    total = 0.0
    for p in positions:
        n = float(p.notional_usd)
        if n <= 0:
            continue
        cls = get_asset_class(p.asset).value
        out[cls] = out.get(cls, 0.0) + n
        total += n
    if total <= 0:
        return {}
    return {k: v / total for k, v in out.items()}


# ----------------------------------------------------------------------
# Drift computation
# ----------------------------------------------------------------------

@dataclass
class DriftReport:
    """Per-class drift between actual and target weights."""
    actual_weights: Dict[str, float]
    target_weights: Dict[str, float]
    drifts: Dict[str, float]          # actual - target, signed
    max_abs_drift_pct: float          # largest |drift| in pct
    within_tolerance: bool            # all classes within drift_tolerance_pct
    classes_over_cap: List[str] = field(default_factory=list)
    classes_under_floor: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "actual_weights": dict(self.actual_weights),
            "target_weights": dict(self.target_weights),
            "drifts": dict(self.drifts),
            "max_abs_drift_pct": self.max_abs_drift_pct,
            "within_tolerance": self.within_tolerance,
            "classes_over_cap": list(self.classes_over_cap),
            "classes_under_floor": list(self.classes_under_floor),
        }


def compute_drift(
    actual_weights: Dict[str, float],
    policy: AllocationPolicy,
) -> DriftReport:
    """Compute per-class drift and overall tolerance check.

    Args:
        actual_weights: {asset_class: weight} from current_actual_weights.
        policy: the AllocationPolicy to compare against.

    Returns:
        DriftReport with per-class drifts and within_tolerance flag.
    """
    tol = policy.drift_tolerance_pct / 100.0
    drifts: Dict[str, float] = {}
    over_cap: List[str] = []
    under_floor: List[str] = []
    # Use the union of actual and target classes (target-only classes
    # may have actual=0, which counts as a drift).
    all_classes = set(actual_weights.keys()) | set(policy.targets.keys())
    for cls in all_classes:
        actual = actual_weights.get(cls, 0.0)
        target = policy.target_for(cls)
        drift = actual - target
        drifts[cls] = drift
        if actual > policy.cap_for(cls) + 1e-9:
            over_cap.append(cls)
        if target > 0 and actual < policy.floor_for(cls) - 1e-9:
            under_floor.append(cls)
    max_abs = max((abs(d) for d in drifts.values()), default=0.0)
    return DriftReport(
        actual_weights=dict(actual_weights),
        target_weights=dict(policy.targets),
        drifts=drifts,
        max_abs_drift_pct=max_abs * 100.0,
        within_tolerance=len(over_cap) == 0 and len(under_floor) == 0,
        classes_over_cap=over_cap,
        classes_under_floor=under_floor,
    )


# ----------------------------------------------------------------------
# Pre-trade gate: would this trade push a class over its cap?
# ----------------------------------------------------------------------

def check_trade_against_policy(
    asset: str,
    proposed_notional_usd: float,
    current_positions,
    policy: AllocationPolicy,
) -> Tuple[bool, str]:
    """Check if adding `proposed_notional_usd` of `asset` would push any
    asset class above its target+drift cap.

    Returns (ok, reason). Same convention as risk_agent._check_concentration:
      - (True, "policy_disabled") if policy.enabled=False
      - (True, "empty_book") if no current positions
      - (True, "ok") if no class would breach
      - (False, "class_X_N.Npct_exceeds_Ypct_cap") if breach

    This is a strict version of the gate: it allows the first trade of
    an empty book (no target to compare against) and rejects trades that
    would push a class above its drift-aware cap.
    """
    if not policy.enabled:
        return True, "policy_disabled"
    if proposed_notional_usd <= 0:
        return True, "zero_notional_skipped"
    cls = get_asset_class(asset)
    if cls == AssetClass.CASH:
        return True, "cash_class_skipped"
    opens = list(current_positions)
    if not opens:
        return True, "empty_book"
    # Sprint 47A (audit M15 Option B): bypass the drift policy on
    # small accounts. With account <$50 and the $10 minimum, the
    # 40/40/10/10 target + 10% drift is structurally unreachable —
    # a single position is already 50-100% of the book. The 44A
    # concentration cap (60%) is the appropriate backstop here.
    if policy.small_account_threshold_usd > 0:
        total_notional = (
            sum(getattr(p, "notional_usd", 0.0) for p in opens)
            + proposed_notional_usd
        )
        if total_notional < policy.small_account_threshold_usd:
            return True, "small_account_policy_skipped"
    # Compute actual weights of current + proposed trade.
    proposed_positions = list(opens) + [_ProposedPosition(asset, proposed_notional_usd)]
    new_weights = current_actual_weights(proposed_positions)
    if not new_weights:
        return True, "zero_weight_post_trade"
    # Check the class of the proposed trade.
    actual_cls_pct = new_weights.get(cls.value, 0.0)
    cap = policy.cap_for(cls.value)
    if actual_cls_pct > cap + 1e-9:
        return False, (
            f"allocation_policy_{cls.value}_{actual_cls_pct * 100:.1f}pct_"
            f"exceeds_{cap * 100:.0f}pct_cap"
        )
    return True, "within_policy"


@dataclass
class _ProposedPosition:
    """Internal: represents a hypothetical position for the gate's projection."""
    asset: str
    notional_usd: float
