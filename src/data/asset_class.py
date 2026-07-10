"""
Sprint 44A — Asset class taxonomy for portfolio concentration analysis.

Map each tradable symbol to its asset class so the bot can:
  - Compute sector / asset-class exposure of the current portfolio.
  - Reject new trades that would push a single asset class over a config cap.
  - Surface "sector concentration" warnings in the dashboard.

The taxonomy is intentionally simple and conservative. We only need
enough granularity to catch the bridgewater-style failure mode where
"5 different tickers" are actually 3 correlated bets (e.g. BTC + ETH
+ SOL, all in `crypto`).

MUST stay in sync with config.yaml `brokers.*.symbols`. If a new
ticker is added there, add it here too — otherwise the concentration
check will silently bucket it as CASH and skip the gate.

Sprint 44A Tier 1 (Bridgewater risk assessment, prompts 3).
"""
from __future__ import annotations

from enum import Enum
from typing import Dict


class AssetClass(str, Enum):
    """Top-level asset class taxonomy used for concentration analysis.

    Values are short strings so they serialize cleanly into the
    audit ledger and dashboard JSON without extra mapping.
    """
    CRYPTO = "crypto"
    EQUITY_GROWTH = "equity_growth"            # SPY, QQQ — large cap growth-tilted
    EQUITY_VALUE = "equity_value"              # future use (e.g. VTV, IWD)
    COMMODITY_SAFE = "commodity_safe"          # GLD — precious metals / store of value
    COMMODITY_ENERGY = "commodity_energy"      # USO — energy / oil
    COMMODITY_AGRI = "commodity_agriculture"   # future use (e.g. DBA, CORN)
    FIXED_INCOME = "fixed_income"              # future use (e.g. TLT, IEF, AGG)
    CASH = "cash"                              # USDT/USD, parked, or unknown


# Sprint 44A — single source of truth for asset→class mapping.
# MUST stay in sync with config.yaml `brokers.*.symbols`.
ASSET_CLASS_MAP: Dict[str, AssetClass] = {
    # Crypto (broker: binanceus)
    "BTC-USD": AssetClass.CRYPTO,
    "ETH-USD": AssetClass.CRYPTO,
    "SOL-USD": AssetClass.CRYPTO,
    "BTCUSDT": AssetClass.CRYPTO,
    "ETHUSDT": AssetClass.CRYPTO,
    "SOLUSDT": AssetClass.CRYPTO,
    # Equity — broad US large cap growth
    "SPY": AssetClass.EQUITY_GROWTH,
    "QQQ": AssetClass.EQUITY_GROWTH,
    # Commodities
    "GLD": AssetClass.COMMODITY_SAFE,
    "USO": AssetClass.COMMODITY_ENERGY,
}


def get_asset_class(symbol: str) -> AssetClass:
    """Return the AssetClass for a symbol.

    Unknown symbols return AssetClass.CASH (not raise) so callers can
    decide how to handle them. For the concentration check, CASH is
    treated as a separate bucket — it doesn't add to a risky class,
    so it won't trigger any rejection.
    """
    return ASSET_CLASS_MAP.get(symbol, AssetClass.CASH)


def is_known_tradable(symbol: str) -> bool:
    """True if the symbol is mapped to a tradable asset class (not CASH)."""
    return symbol in ASSET_CLASS_MAP
