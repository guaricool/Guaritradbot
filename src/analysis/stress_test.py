"""
Sprint 44B — Recession / Crisis stress test.

Applies historical shock vectors to the current portfolio to estimate
the drawdown under each named crisis scenario. This is the
Bridgewater-style "recession stress test" (prompt 3, gap #5): instead
of trusting that "diversified" means safe, the bot should know
"if 2008 happens again, my portfolio loses ~X%".

Why this matters
----------------
The bot's existing risk tools (Kelly, drawdown kill switch, Monte Carlo)
operate on the current regime. A stress test forces the question
"what if the regime breaks?". The answer is often uncomfortable and
is the only way to size positions defensively across cycles.

Scenario design
---------------
We use real historical drawdowns from 3 crisis periods. Numbers are
approximate peak-to-trough over weeks-to-months, not day-to-day vol.
Crypto and gold get a special note (pre-2009 crypto didn't exist, so
2008 GFC is a "no data" scenario for BTC/ETH — we use a synthetic
estimate based on the post-2020 behavior of crypto in risk-off events).

Each shock is applied to the asset's notional. The portfolio's
stressed value is `sum(notional * (1 + shock))`. The stress drawdown
is `(stressed_value - original_value) / original_value`.

Sprint 44B Tier 2 (Bridgewater #5, BlackRock #4).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence

from src.data.asset_class import get_asset_class, AssetClass


# ----------------------------------------------------------------------
# Historical shock vectors
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class StressScenario:
    """A named crisis scenario with per-asset-class shock percentages.

    Shock values are FRACTIONAL returns (e.g. -0.38 = -38% in this
    scenario). 0.0 means "no data" or "insignificant move" — distinct
    from "no exposure" which would mean the asset isn't in the book.
    """
    name: str
    description: str
    period: str                     # e.g. "Sep 2008 - Mar 2009"
    shocks: Dict[str, float]        # asset class -> fractional return


# 2008 Global Financial Crisis (Sep 2008 - Mar 2009 S&P trough).
#   S&P 500: -38% peak-to-trough
#   Gold: -25% initial (liquidity crunch), then +25% by end-2009
#   Crude oil: -78% peak-to-trough (Jul 2008 - Feb 2009)
#   BTC: didn't exist — use a conservative proxy based on the
#     post-2020 behavior of crypto in macro risk-off events (~-50%).
SCENARIO_2008_GFC = StressScenario(
    name="2008_GFC",
    description="Global Financial Crisis — equity drawdown, commodities crash, gold liquidity drop",
    period="2008-09 to 2009-03",
    shocks={
        AssetClass.EQUITY_GROWTH.value: -0.38,
        AssetClass.EQUITY_VALUE.value: -0.42,   # value suffered more in 2008
        AssetClass.COMMODITY_SAFE.value: -0.10,  # gold dropped initially on liquidity
        AssetClass.COMMODITY_ENERGY.value: -0.78,  # oil collapsed
        AssetClass.COMMODITY_AGRI.value: -0.30,
        AssetClass.CRYPTO.value: -0.50,         # synthetic; BTC N/A in 2008
        AssetClass.FIXED_INCOME.value: -0.05,   # Treasuries rallied (flight to safety)
        AssetClass.CASH.value: 0.0,
    },
)

# 2020 COVID crash (Feb 19 - Mar 23, ~5 weeks).
#   S&P 500: -34% in 33 days
#   Crude oil: -65% (Apr 2020 WTI briefly went negative)
#   Gold: -12% initial then +25% to year-end
#   BTC: -50% on Mar 12-13 then +300% by year-end
# We use the TROUGH numbers here (worst 5 weeks), not the full year.
SCENARIO_2020_COVID = StressScenario(
    name="2020_COVID",
    description="COVID crash — fast equity drawdown, oil collapse, gold initial drop, BTC halving",
    period="2020-02-19 to 2020-04",
    shocks={
        AssetClass.EQUITY_GROWTH.value: -0.34,
        AssetClass.EQUITY_VALUE.value: -0.36,
        AssetClass.COMMODITY_SAFE.value: -0.12,
        AssetClass.COMMODITY_ENERGY.value: -0.65,
        AssetClass.COMMODITY_AGRI.value: -0.15,
        AssetClass.CRYPTO.value: -0.50,
        AssetClass.FIXED_INCOME.value: +0.08,  # bonds rallied (Fed cuts)
        AssetClass.CASH.value: 0.0,
    },
)

# 2022 inflation + rate hikes (Jan - Oct 2022).
#   S&P 500: -25%
#   Nasdaq (QQQ): -33%
#   Bonds (TLT): -32% (worst bond year in history)
#   Gold: -8% but relatively flat
#   Oil: +45% in early 2022 (Russia/Ukraine) then -30% by year-end
#   BTC: -64% (LUNA collapse, 3AC, FTX)
SCENARIO_2022_RATE_HIKES = StressScenario(
    name="2022_RATE_HIKES",
    description="Inflation/rate hikes — equity + bond drawdown, BTC crashed, gold flat",
    period="2022-01 to 2022-10",
    shocks={
        AssetClass.EQUITY_GROWTH.value: -0.33,  # QQQ was hit harder than SPY
        AssetClass.EQUITY_VALUE.value: -0.10,   # value held up better
        AssetClass.COMMODITY_SAFE.value: -0.08,
        AssetClass.COMMODITY_ENERGY.value: +0.30,  # net positive after Russia spike
        AssetClass.COMMODITY_AGRI.value: +0.10,
        AssetClass.CRYPTO.value: -0.64,
        AssetClass.FIXED_INCOME.value: -0.32,   # bond rout
        AssetClass.CASH.value: 0.0,
    },
)


# All scenarios the bot knows about. Order = how aggressive we think they
# were for our asset mix.
DEFAULT_SCENARIOS: List[StressScenario] = [
    SCENARIO_2008_GFC,
    SCENARIO_2020_COVID,
    SCENARIO_2022_RATE_HIKES,
]


# ----------------------------------------------------------------------
# Stress result types
# ----------------------------------------------------------------------

@dataclass
class PositionStress:
    """Per-position stress impact under a scenario."""
    asset: str
    asset_class: str
    notional_usd: float
    shock_pct: float
    stressed_value_usd: float
    pnl_usd: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StressTestResult:
    """Result of a stress test under one scenario."""
    scenario_name: str
    scenario_description: str
    period: str
    original_portfolio_usd: float
    stressed_portfolio_usd: float
    total_pnl_usd: float
    drawdown_pct: float                 # negative number
    per_position: List[PositionStress] = field(default_factory=list)
    per_asset_class_pnl: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ----------------------------------------------------------------------
# Position input shape
# ----------------------------------------------------------------------

# We don't want to couple to the Position dataclass directly to keep
# this module testable in isolation. Callers pass any object with
# .asset and .notional_usd attributes.
class _HasAsset:
    asset: str

    @property
    def notional_usd(self) -> float:
        ...


# ----------------------------------------------------------------------
# Core API
# ----------------------------------------------------------------------

def stress_position(
    asset: str,
    notional_usd: float,
    scenario: StressScenario,
) -> PositionStress:
    """Compute the stress impact of a scenario on a single position.

    The shock is looked up by asset CLASS (not by ticker), so SPY and
    QQQ both get the EQUITY_GROWTH shock. Unknown asset classes (CASH)
    use the scenario's CASH shock (0.0 by default — cash is cash).
    """
    cls = get_asset_class(asset)
    shock = scenario.shocks.get(cls.value, 0.0)
    stressed_value = notional_usd * (1.0 + shock)
    pnl = stressed_value - notional_usd
    return PositionStress(
        asset=asset,
        asset_class=cls.value,
        notional_usd=notional_usd,
        shock_pct=shock,
        stressed_value_usd=stressed_value,
        pnl_usd=pnl,
    )


def stress_portfolio(
    positions: Sequence[_HasAsset],
    scenario: StressScenario,
) -> StressTestResult:
    """Apply a scenario to a list of positions and aggregate.

    Args:
        positions: any iterable of objects with `.asset` (str) and
            `.notional_usd` (float, absolute value). The Position
            dataclass from src.data_store.positions satisfies this.
        scenario: a StressScenario.

    Returns:
        StressTestResult with per-position, per-class, and aggregate
        P&L and drawdown.
    """
    per_position: List[PositionStress] = []
    per_class_pnl: Dict[str, float] = {}
    original_total = 0.0
    stressed_total = 0.0
    for p in positions:
        notional = float(p.notional_usd)
        if notional <= 0:
            continue
        ps = stress_position(p.asset, notional, scenario)
        per_position.append(ps)
        per_class_pnl[ps.asset_class] = per_class_pnl.get(ps.asset_class, 0.0) + ps.pnl_usd
        original_total += notional
        stressed_total += ps.stressed_value_usd
    total_pnl = stressed_total - original_total
    dd = (total_pnl / original_total) if original_total > 0 else 0.0
    return StressTestResult(
        scenario_name=scenario.name,
        scenario_description=scenario.description,
        period=scenario.period,
        original_portfolio_usd=original_total,
        stressed_portfolio_usd=stressed_total,
        total_pnl_usd=total_pnl,
        drawdown_pct=dd,
        per_position=per_position,
        per_asset_class_pnl=per_class_pnl,
    )


def stress_portfolio_all_scenarios(
    positions: Sequence[_HasAsset],
    scenarios: Optional[Sequence[StressScenario]] = None,
) -> List[StressTestResult]:
    """Run stress_portfolio for every scenario in `scenarios` (or DEFAULT_SCENARIOS)."""
    if scenarios is None:
        scenarios = DEFAULT_SCENARIOS
    return [stress_portfolio(positions, s) for s in scenarios]


def worst_case_drawdown(results: Sequence[StressTestResult]) -> StressTestResult:
    """Return the scenario with the most negative drawdown.

    Useful as a single "how bad can it get" number for the dashboard.
    Empty input → zero result (no signal).
    """
    if not results:
        return StressTestResult(
            scenario_name="",
            scenario_description="",
            period="",
            original_portfolio_usd=0.0,
            stressed_portfolio_usd=0.0,
            total_pnl_usd=0.0,
            drawdown_pct=0.0,
        )
    return min(results, key=lambda r: r.drawdown_pct)
