"""
Category (asset-class) historical performance scoring.

Gap identified from reviewing the kalshi-ai-trading-bot reference
project: it gates position sizing by each category's historical
ROI/win-rate, not just by the individual signal's confidence. This
bot already has per-asset "lessons" (src/safety/decision_log.py) and
an asset-class taxonomy (src/data/asset_class.py) for the
concentration gate — this module is the missing link: aggregate
closed-trade outcomes BY asset class and turn a poor track record
into a conservative, size-only-ever-DOWN multiplier.

Design mirrors the existing Kelly-sizing gate in risk_agent.py:
  - opt-in (default multiplier is 1.0 = no change)
  - only ever scales risk DOWN, never up (a good historical win
    rate on a small sample doesn't justify sizing UP — that's how
    a lucky streak turns into overconfidence)
  - requires a minimum trade count before it does anything (a
    2-trade sample isn't a track record)
  - fails open: any missing/malformed data returns multiplier 1.0
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from src.data.asset_class import AssetClass, get_asset_class


@dataclass
class CategoryStats:
    asset_class: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_usd: float
    avg_pnl_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_class": self.asset_class,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "total_pnl_usd": self.total_pnl_usd,
            "avg_pnl_pct": self.avg_pnl_pct,
        }


def compute_category_stats(
    outcome_records: Iterable[Dict[str, Any]],
) -> Dict[str, CategoryStats]:
    """Aggregate DecisionLog outcome records by asset class.

    `outcome_records` is any iterable of dicts shaped like
    DecisionLog's OutcomeRecord (must have "asset", "pnl_usd",
    "pnl_pct"; records missing those keys or not outcome-kind are
    skipped rather than raising — this is a read-only analytics
    helper, it should never be the reason a trade fails).

    Returns {asset_class_value: CategoryStats}, one entry per
    asset class that has at least one closed trade. Asset classes
    with zero trades are simply absent (not zero-filled) so callers
    can distinguish "no track record yet" from "0% win rate".
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for rec in outcome_records:
        if not isinstance(rec, dict):
            continue
        if rec.get("kind") not in (None, "outcome"):
            # Allow both raw OutcomeRecord dicts (no "kind") and
            # DecisionLog's tagged records (kind="outcome").
            continue
        asset = rec.get("asset")
        if not asset:
            continue
        try:
            pnl_usd = float(rec.get("pnl_usd", 0.0))
            pnl_pct = float(rec.get("pnl_pct", 0.0))
        except (TypeError, ValueError):
            continue
        asset_class = get_asset_class(asset).value
        buckets.setdefault(asset_class, []).append(
            {"pnl_usd": pnl_usd, "pnl_pct": pnl_pct}
        )

    out: Dict[str, CategoryStats] = {}
    for asset_class, rows in buckets.items():
        trades = len(rows)
        wins = sum(1 for r in rows if r["pnl_usd"] > 0)
        losses = trades - wins
        total_pnl_usd = sum(r["pnl_usd"] for r in rows)
        avg_pnl_pct = sum(r["pnl_pct"] for r in rows) / trades if trades else 0.0
        out[asset_class] = CategoryStats(
            asset_class=asset_class,
            trades=trades,
            wins=wins,
            losses=losses,
            win_rate=(wins / trades) if trades else 0.0,
            total_pnl_usd=total_pnl_usd,
            avg_pnl_pct=avg_pnl_pct,
        )
    return out


def category_risk_multiplier(
    stats: Optional[CategoryStats],
    min_trades: int = 10,
    poor_win_rate_threshold: float = 0.35,
    reduction_factor: float = 0.5,
) -> float:
    """Return the risk-sizing multiplier for one asset class.

    1.0 (no change) unless:
      - `stats` is None (no track record yet — nothing to act on), or
      - `stats.trades < min_trades` (too small a sample to trust), or
      - `stats.win_rate >= poor_win_rate_threshold` (track record is
        fine, or at least not bad enough to act on).

    Only ever returns 1.0 or `reduction_factor` (never anything
    above 1.0, never anything below `reduction_factor`) — this is a
    conservative brake, not a leverage dial. A good historical win
    rate does NOT increase the multiplier past 1.0; the strategy's
    own signal confidence and Kelly sizing already handle sizing up,
    and amplifying on a historically-good-but-small sample is
    exactly the overconfidence trap Sprint 46S's Kelly cap exists to
    avoid.
    """
    if stats is None:
        return 1.0
    if stats.trades < min_trades:
        return 1.0
    if stats.win_rate >= poor_win_rate_threshold:
        return 1.0
    return float(reduction_factor)


def asset_category_multiplier(
    asset: str,
    outcome_records: Iterable[Dict[str, Any]],
    min_trades: int = 10,
    poor_win_rate_threshold: float = 0.35,
    reduction_factor: float = 0.5,
) -> float:
    """Convenience wrapper: asset symbol -> multiplier in one call,
    for callers (RiskManagerAgent) that just want a number and don't
    need the full per-category breakdown."""
    stats_by_class = compute_category_stats(outcome_records)
    asset_class = get_asset_class(asset).value
    return category_risk_multiplier(
        stats_by_class.get(asset_class),
        min_trades=min_trades,
        poor_win_rate_threshold=poor_win_rate_threshold,
        reduction_factor=reduction_factor,
    )
