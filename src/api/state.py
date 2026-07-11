"""
Sprint 46A — State snapshot builder for the bot HTTP API.

Pure read-only layer that turns the bot's on-disk state (positions
JSON, audit JSONL, mode_override JSON) + a live price snapshot into
typed Pydantic models the REST/WS endpoints can serve.

Design principle
----------------
The bot writes state to disk (positions, audit, mode). The API
layer does NOT mutate the state — it only reads. Mutations (close
position, toggle mode) go through dedicated POST endpoints in
server.py that write to the same on-disk files the bot already reads.
That keeps the bot logic untouched.

What this module does
---------------------
- `build_state_snapshot()` — full snapshot for the dashboard's
  initial load: positions + P&L + balance + mode + counts.
- `build_positions()` — list of positions with computed current
  P&L (calls yfinance for live prices; falls back to entry price
  if fetch fails, with `current_price_source` flag so the UI can
  label it appropriately).
- `build_audit()` — recent audit events (filterable by since/until).
- `read_mode()` — current mode from `audit/mode_override.json` or
  config.yaml fallback.
- `write_mode()` — toggle mode (writes the override file; the bot
  re-reads it on next cycle via B033 paper-mode gate in the broker,
  and on next startup for the main loop).
- `read_current_prices()` — live prices via yfinance with a 30s
  in-process cache so we don't hammer the API.

Threading model
---------------
This module is sync. The FastAPI app can run the heavy lifting
(yfinance fetch) in a thread pool via `run_in_threadpool` from
`fastapi.concurrency`. The state files are read in the request
handler — short operations, no need for a worker process.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.data_store.positions import PositionRepository, Position
from src.safety.audit_ledger import AuditLedger


# ----------------------------------------------------------------------
# Pydantic response models
# ----------------------------------------------------------------------

# Pydantic models for the API surface. Kept here (not in server.py) so
# the snapshot builder can return them directly and the server can
# just `return` them — FastAPI serializes them automatically.
from pydantic import BaseModel, Field


class PositionSummary(BaseModel):
    """One open position with live P&L. Suitable for the dashboard table."""
    id: str
    asset: str
    direction: str
    entry_price: float
    current_price: Optional[float] = None
    current_price_source: str = "live"   # "live" | "entry_fallback" | "stale_cache"
    qty: float
    notional_usd: float
    unrealized_pnl_usd: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None
    stop_loss: float
    take_profit: float
    entry_ts: float
    strategy: str
    age_hours: Optional[float] = None


class ModeInfo(BaseModel):
    """Current LIVE/PAPER mode and the path used to toggle it."""
    mode: str                                       # "live" or "paper"
    mandate_enabled: bool
    use_testnet: bool                               # from config.yaml
    switched_at: Optional[float] = None
    switched_by: Optional[str] = None
    mode_override_path: str


class StateSnapshot(BaseModel):
    """Full dashboard snapshot — what the home page requests once on load."""
    mode: ModeInfo
    balance_usd: float = 0.0
    balance_source: str = "unknown"                 # "broker" | "testnet_sim" | "live"
    # Sprint 46C: per-broker available cash. `balance_usd` above was
    # ALWAYS 0.0/"unknown" — nothing ever populated it, so the
    # dashboard never showed real money available, even though both
    # BINANCE_API_KEY/SECRET and ALPACA_API_KEY/SECRET_KEY were set.
    # These two fields are populated from the SAME broker instances
    # main.py already constructed for trading (passed in via
    # `set_brokers()`), so no duplicate exchange connections are made.
    binance_balance_usd: Optional[float] = None
    binance_balance_source: str = "unavailable"      # "live" | "unavailable" | "not_configured"
    alpaca_balance_usd: Optional[float] = None
    alpaca_balance_source: str = "unavailable"       # "live" | "unavailable" | "not_configured"
    positions: List[PositionSummary] = Field(default_factory=list)
    open_count: int = 0
    total_unrealized_usd: float = 0.0
    total_unrealized_pct: float = 0.0
    total_exposure_usd: float = 0.0
    daily_realized_pnl_usd: float = 0.0
    total_realized_pnl_usd: float = 0.0
    last_update_ts: Optional[float] = None


class AuditEvent(BaseModel):
    """A single line from audit.jsonl, lightly typed."""
    ts: float
    iso: str
    event_type: str
    payload: Dict = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict) -> "AuditEvent":
        # The audit row has `ts`, `iso`, `event_type`, plus all
        # payload fields spread at top level. Pull the rest into
        # `payload` so the API surface is consistent.
        ts = float(row.get("ts", 0.0))
        iso = str(row.get("iso", ""))
        et = str(row.get("event_type", "unknown"))
        known = {"ts", "iso", "event_type"}
        payload = {k: v for k, v in row.items() if k not in known}
        return cls(ts=ts, iso=iso, event_type=et, payload=payload)


# ----------------------------------------------------------------------
# Live price cache
# ----------------------------------------------------------------------

_PRICE_CACHE: Dict[str, Tuple[float, float, str]] = {}
"""asset -> (price, fetched_at, source). Source: 'live' | 'cache'."""

PRICE_CACHE_TTL_S = 30.0


def _fetch_one_price(asset: str) -> Tuple[Optional[float], str]:
    """Fetch latest close for one asset via yfinance.

    Returns (price, source) where source is 'live' on a fresh fetch
    and 'fetch_failed' if the API call returned no data. The
    caller decides how to use this.
    """
    from src.data.yf_safe import safe_yf_download  # local to avoid import cost at module load
    try:
        df = safe_yf_download(asset, period="5d", interval="1d")
    except Exception:
        return None, "fetch_failed"
    if df is None or df.empty:
        return None, "fetch_failed"
    price_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    if price_col not in df.columns:
        return None, "fetch_failed"
    try:
        price = float(df[price_col].dropna().iloc[-1])
    except Exception:
        return None, "fetch_failed"
    if not (price == price and price != float("inf")):  # NaN/Inf check
        return None, "fetch_failed"
    return price, "live"


def read_current_prices(assets: List[str], max_age_s: float = PRICE_CACHE_TTL_S) -> Dict[str, float]:
    """Return {asset: price} for the given assets.

    Uses a process-level cache (TTL = max_age_s) to avoid hammering
    yfinance on every dashboard refresh. Unknown assets or fetch
    failures are simply omitted from the result — the caller can
    fall back to the entry price (which it does, with a
    `current_price_source = "entry_fallback"` label so the UI can
    show the staleness to the operator).
    """
    now = time.time()
    out: Dict[str, float] = {}
    for asset in assets:
        if not asset:
            continue
        cached = _PRICE_CACHE.get(asset)
        if cached and (now - cached[1]) < max_age_s:
            out[asset] = cached[0]
            continue
        price, source = _fetch_one_price(asset)
        if price is not None:
            _PRICE_CACHE[asset] = (price, now, source)
            out[asset] = price
    return out


def invalidate_price_cache(asset: Optional[str] = None) -> None:
    """Clear the price cache. Pass an asset to clear one, or None for all.

    Useful for tests and for a "force refresh" button on the
    dashboard if the user suspects stale data.
    """
    if asset is None:
        _PRICE_CACHE.clear()
    else:
        _PRICE_CACHE.pop(asset, None)


# ----------------------------------------------------------------------
# Broker balances (Sprint 46C)
# ----------------------------------------------------------------------
#
# The dashboard is supposed to show "how much money do I actually have
# available" per broker (binance.us for crypto, Alpaca for equities).
# Before this, `StateSnapshot.balance_usd` existed as a field but NO
# caller ever set it to anything other than its 0.0 default — the API
# never had a reference to the bot's actual broker connections. This
# module registers the SAME BrokerClient/AlpacaBroker instances main.py
# already built for trading (see `set_brokers()`, called once from
# `main.py::_start_api_server`), so balance calls reuse the existing
# authenticated connections instead of opening new ones with duplicate
# credentials/rate-limit budget.
#
# Cached with a short TTL — balance doesn't need to be sub-second-live
# and both `get_usdt_balance()`/`get_usd_balance()` are real network
# calls to the exchange/broker.

_BROKER_CLIENT = None      # BrokerClient (binance.us / ccxt) or None
_ALPACA_BROKER = None      # AlpacaBroker or None (not configured if None)

BALANCE_CACHE_TTL_S = 15.0
_BALANCE_CACHE: Dict[str, Tuple[Optional[float], str, float]] = {}
"""broker_name -> (balance_or_None, source, fetched_at)."""


def set_brokers(broker_client=None, alpaca_broker=None) -> None:
    """Register the bot's live broker instances for balance lookups.

    Called once from `main.py::_start_api_server`, right before the
    uvicorn thread starts, with the exact same `broker_client` /
    `alpaca_broker` objects the trading loop uses. Safe to call with
    both None (e.g. in tests, or if neither broker is configured) —
    every consumer below treats a None broker as "not configured"
    rather than raising.
    """
    global _BROKER_CLIENT, _ALPACA_BROKER
    _BROKER_CLIENT = broker_client
    _ALPACA_BROKER = alpaca_broker


def _get_binance_balance() -> Tuple[Optional[float], str]:
    """Return (balance, source) for the binance.us broker.

    source is one of: "live" (fresh fetch), "cache" (within TTL),
    "unavailable" (broker configured but the call failed — e.g.
    network/API-key issue), "not_configured" (no broker instance
    registered at all, e.g. exchange section missing from config).
    """
    if _BROKER_CLIENT is None:
        return None, "not_configured"
    now = time.time()
    cached = _BALANCE_CACHE.get("binance")
    if cached and (now - cached[2]) < BALANCE_CACHE_TTL_S:
        return cached[0], "cache" if cached[1] == "live" else cached[1]
    try:
        bal = float(_BROKER_CLIENT.get_usdt_balance())
        _BALANCE_CACHE["binance"] = (bal, "live", now)
        return bal, "live"
    except Exception:
        # Don't cache failures — retry on the next request instead of
        # being stuck showing "unavailable" for a full TTL window
        # after a single transient network blip.
        return None, "unavailable"


def _get_alpaca_balance() -> Tuple[Optional[float], str]:
    """Return (balance, source) for the Alpaca broker. See
    `_get_binance_balance()` for the meaning of each source value."""
    if _ALPACA_BROKER is None:
        return None, "not_configured"
    now = time.time()
    cached = _BALANCE_CACHE.get("alpaca")
    if cached and (now - cached[2]) < BALANCE_CACHE_TTL_S:
        return cached[0], "cache" if cached[1] == "live" else cached[1]
    try:
        bal = float(_ALPACA_BROKER.get_usd_balance())
        _BALANCE_CACHE["alpaca"] = (bal, "live", now)
        return bal, "live"
    except Exception:
        return None, "unavailable"


# ----------------------------------------------------------------------
# Position summary
# ----------------------------------------------------------------------

def _build_position_summary(
    pos: Position,
    current_price: Optional[float],
    current_price_source: str,
) -> PositionSummary:
    """Build a PositionSummary from a Position + its current price.

    If current_price is None, falls back to entry_price (so the
    P&L is reported as 0.0 and the source label is "entry_fallback").
    The dashboard can then highlight the staleness.
    """
    notional = pos.notional_usd
    if current_price is None:
        # Fallback to entry price — P&L is 0, source flagged.
        current_price = pos.entry_price
        current_price_source = "entry_fallback"
    if pos.direction == "long":
        upnl = (current_price - pos.entry_price) * pos.qty
    else:
        upnl = (pos.entry_price - current_price) * pos.qty
    upnl_pct = (upnl / notional) if notional > 0 else 0.0
    age_h = (time.time() - pos.entry_ts) / 3600.0 if pos.entry_ts else None
    return PositionSummary(
        id=pos.position_id,
        asset=pos.asset,
        direction=pos.direction,
        entry_price=pos.entry_price,
        current_price=current_price,
        current_price_source=current_price_source,
        qty=pos.qty,
        notional_usd=notional,
        unrealized_pnl_usd=upnl,
        unrealized_pnl_pct=upnl_pct,
        stop_loss=pos.stop_loss,
        take_profit=pos.take_profit,
        entry_ts=pos.entry_ts,
        strategy=pos.strategy,
        age_hours=age_h,
    )


# ----------------------------------------------------------------------
# Mode
# ----------------------------------------------------------------------

def read_mode(
    config: Optional[dict] = None,
    audit_path: Optional[str] = None,
) -> ModeInfo:
    """Read the current LIVE/PAPER mode.

    Source of truth is `audit/mode_override.json` (the file the
    dashboard writes when you click the toggle). Falls back to
    `config.yaml:mandate.enabled` and `config.yaml:exchange.use_testnet`
    if the override file doesn't exist.

    The override file's `mandate_enabled` is the master switch. The
    config's `use_testnet` only affects the broker endpoint URL when
    the override is missing.

    `audit_path` defaults to the DASHBOARD_AUDIT_PATH env var, then
    to "audit/audit.jsonl" relative to the CWD.
    """
    if audit_path is None:
        audit_path = os.getenv("DASHBOARD_AUDIT_PATH", "audit/audit.jsonl")
    """Read the current LIVE/PAPER mode.

    Source of truth is `audit/mode_override.json` (the file the
    dashboard writes when you click the toggle). Falls back to
    `config.yaml:mandate.enabled` and `config.yaml:exchange.use_testnet`
    if the override file doesn't exist.

    The override file's `mandate_enabled` is the master switch. The
    config's `use_testnet` only affects the broker endpoint URL when
    the override is missing.
    """
    override_path = str(Path(audit_path).parent / "mode_override.json")
    mandate_enabled = False
    use_testnet = True
    switched_at = None
    switched_by = None
    if config is None:
        config = {}
    if "mandate" in config:
        mandate_enabled = bool(config["mandate"].get("enabled", False))
    if "exchange" in config:
        use_testnet = bool(config["exchange"].get("use_testnet", True))
    if os.path.exists(override_path):
        try:
            with open(override_path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            if "mandate_enabled" in ov:
                mandate_enabled = bool(ov["mandate_enabled"])
            switched_at_raw = ov.get("switched_at")
            if switched_at_raw is not None:
                try:
                    switched_at = float(switched_at_raw)
                except (TypeError, ValueError):
                    switched_at = None
            switched_by = ov.get("switched_by")
        except Exception:
            pass  # Malformed override file = ignore, use config fallback
    mode = "live" if mandate_enabled else "paper"
    return ModeInfo(
        mode=mode,
        mandate_enabled=mandate_enabled,
        use_testnet=use_testnet,
        switched_at=switched_at,
        switched_by=switched_by,
        mode_override_path=str(override_path),
    )


def write_mode(
    mandate_enabled: bool,
    switched_by: str = "api",
    audit_path: Optional[str] = None,
) -> ModeInfo:
    """Write the mode override file. Returns the new ModeInfo.

    Creates `audit/mode_override.json` (parent dir if needed) with:
        {
          "mandate_enabled": bool,
          "switched_at": <unix_ts>,
          "switched_by": "api"
        }

    The bot reads this on next startup (main loop) AND on every
    broker call (B033 paper-mode gate in `AlpacaBroker` /
    `binanceus`), so the toggle takes effect within ~1 cycle without
    requiring a bot restart.
    """
    if audit_path is None:
        audit_path = os.getenv("DASHBOARD_AUDIT_PATH", "audit/audit.jsonl")
    override_path = Path(audit_path).parent / "mode_override.json"
    """Write the mode override file. Returns the new ModeInfo.

    Creates `audit/mode_override.json` (parent dir if needed) with:
        {
          "mandate_enabled": bool,
          "switched_at": <unix_ts>,
          "switched_by": "api"
        }

    The bot reads this on next startup (main loop) AND on every
    broker call (B033 paper-mode gate in `AlpacaBroker` /
    `binanceus`), so the toggle takes effect within ~1 cycle without
    requiring a bot restart.
    """
    override_path = Path(audit_path).parent / "mode_override.json"
    override_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mandate_enabled": bool(mandate_enabled),
        "switched_at": time.time(),
        "switched_by": switched_by,
    }
    tmp = override_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(override_path)  # atomic on POSIX
    return read_mode(audit_path=audit_path)


# ----------------------------------------------------------------------
# Trading config (Sprint 46D) — dashboard-editable trading settings
# ----------------------------------------------------------------------
#
# Same pattern as mode_override.json above: config.yaml is the file
# Carlos edits by hand (with comments explaining every field), so the
# API never rewrites it directly — PyYAML's dump() isn't round-trip
# safe and would silently destroy every comment in the file. Instead,
# dashboard edits go to `audit/trading_config_override.json`, a flat
# JSON of {field: value} that OVERLAYS config.yaml's `trading:` section
# at read time. Whatever's in the override file wins.
#
# IMPORTANT caveat (surfaced to the user via `pending_restart` in the
# API response): `main.py` reads `trading_cfg` (config.yaml merged with
# this override file) ONCE at startup and passes the individual values
# into RiskManagerAgent's constructor — they are NOT re-read per cycle.
# So a saved change here only takes effect after the bot restarts
# (POST /api/restart). This mirrors the existing mode-toggle behavior
# for the main loop's mandate check (see SetModeResponse.note in
# server.py) — nothing new architecturally, just extended to cover all
# of `trading:` instead of only `mandate.enabled`.

TRADING_CONFIG_DEFAULTS: Dict[str, Any] = {
    "risk_per_trade_pct": 1.0,
    "max_open_trades": 5,
    "min_order_usd": 10.0,
    "max_capital_per_trade_pct": 10.0,
    "atr_stop_multiplier": 2.0,
    "atr_take_profit_multiplier": 4.0,
    "risk_reward_ratio": 2.0,
    "enable_position_replacement": True,
    "replacement_score_threshold": 0.20,
    "min_profit_to_protect": 0.0,
}


def _trading_override_path(audit_path: Optional[str] = None) -> Path:
    if audit_path is None:
        audit_path = os.getenv("DASHBOARD_AUDIT_PATH", "audit/audit.jsonl")
    return Path(audit_path).parent / "trading_config_override.json"


def read_trading_config(
    config: Optional[dict] = None,
    audit_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Effective trading config: config.yaml's `trading:` section with
    any dashboard-saved override layered on top.

    Returns the 10 trading fields plus two bookkeeping keys the caller
    (server.py) uses to compute `pending_restart` and strips before
    returning the response: `_override_updated_at` (unix ts of the
    last dashboard save, or None) and `_override_updated_by`.
    """
    if config is None:
        config = {}
    merged: Dict[str, Any] = {**TRADING_CONFIG_DEFAULTS, **(config.get("trading", {}) or {})}
    updated_at = None
    updated_by = None
    override_path = _trading_override_path(audit_path)
    if override_path.exists():
        try:
            with open(override_path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            if isinstance(ov, dict):
                for k, v in ov.items():
                    if k in TRADING_CONFIG_DEFAULTS:
                        merged[k] = v
                updated_at = ov.get("_updated_at")
                updated_by = ov.get("_updated_by")
        except Exception:
            pass  # Malformed override file = ignore, use config.yaml fallback
    merged["_override_updated_at"] = updated_at
    merged["_override_updated_by"] = updated_by
    return merged


def write_trading_config(
    updates: Dict[str, Any],
    updated_by: str = "dashboard",
    config: Optional[dict] = None,
    audit_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge `updates` (a partial dict of trading fields) into
    `audit/trading_config_override.json` and return the new effective
    config (same shape as `read_trading_config`).

    Only keys present in TRADING_CONFIG_DEFAULTS are persisted —
    unknown keys are silently dropped (defense in depth; server.py's
    Pydantic model should already reject them before this is called).
    """
    override_path = _trading_override_path(audit_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    existing: Dict[str, Any] = {}
    if override_path.exists():
        try:
            with open(override_path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    for k, v in updates.items():
        if k in TRADING_CONFIG_DEFAULTS:
            existing[k] = v
    existing["_updated_at"] = time.time()
    existing["_updated_by"] = updated_by
    tmp = override_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    tmp.replace(override_path)  # atomic on POSIX
    return read_trading_config(config=config, audit_path=audit_path)


# ----------------------------------------------------------------------
# Risk / mandate config (Sprint 46F) — dashboard-editable safety gates
# ----------------------------------------------------------------------
#
# Same override-file pattern as trading config above, covering the
# other two places safety-relevant numbers live in config.yaml:
#   - `risk:` — drawdown kill-switch threshold/cooldown, plus the
#     Sprint 44/45 portfolio-risk gate caps (asset-class concentration,
#     correlation, CVaR, stress-test) that RiskManagerAgent has always
#     supported as constructor params but main.py never actually read
#     from config.yaml at all (they silently used the class's
#     hard-coded defaults, so hand-editing config.yaml wouldn't even
#     have changed them before this sprint).
#   - `mandate.allowed_symbols` — the symbol allow-list the MandateGate
#     enforces. Kept separate from the other mandate fields
#     (max_position_usd/max_daily_loss_usd/max_total_exposure_usd) —
#     those are intentionally NOT exposed here yet; ask if you want
#     them added too, same mechanism.
#
# Like trading config, this is READ at startup only — a saved change
# needs a bot restart (POST /api/restart) to take effect.

RISK_CONFIG_DEFAULTS: Dict[str, Any] = {
    "drawdown_kill_threshold_pct": 15.0,
    "drawdown_cooldown_hours": 24.0,
    "max_asset_class_concentration_pct": 60.0,
    "max_avg_correlation_pct": 75.0,
    "max_cvar_95_pct": 20.0,
    "max_stress_drawdown_pct": 70.0,
    "mandate_allowed_symbols": [],
}


def _risk_override_path(audit_path: Optional[str] = None) -> Path:
    if audit_path is None:
        audit_path = os.getenv("DASHBOARD_AUDIT_PATH", "audit/audit.jsonl")
    return Path(audit_path).parent / "risk_config_override.json"


def read_risk_config(
    config: Optional[dict] = None,
    audit_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Effective risk/mandate config: config.yaml's `risk:` section
    (plus `mandate.allowed_symbols`) with any dashboard-saved override
    layered on top. Mirrors `read_trading_config` — see that function
    and this section's header comment for the full rationale.
    """
    if config is None:
        config = {}
    risk_cfg = config.get("risk", {}) or {}
    mandate_cfg = config.get("mandate", {}) or {}
    merged: Dict[str, Any] = {
        "drawdown_kill_threshold_pct": risk_cfg.get(
            "drawdown_kill_threshold_pct", RISK_CONFIG_DEFAULTS["drawdown_kill_threshold_pct"]
        ),
        "drawdown_cooldown_hours": risk_cfg.get(
            "drawdown_cooldown_hours", RISK_CONFIG_DEFAULTS["drawdown_cooldown_hours"]
        ),
        "max_asset_class_concentration_pct": risk_cfg.get(
            "max_asset_class_concentration_pct", RISK_CONFIG_DEFAULTS["max_asset_class_concentration_pct"]
        ),
        "max_avg_correlation_pct": risk_cfg.get(
            "max_avg_correlation_pct", RISK_CONFIG_DEFAULTS["max_avg_correlation_pct"]
        ),
        "max_cvar_95_pct": risk_cfg.get("max_cvar_95_pct", RISK_CONFIG_DEFAULTS["max_cvar_95_pct"]),
        "max_stress_drawdown_pct": risk_cfg.get(
            "max_stress_drawdown_pct", RISK_CONFIG_DEFAULTS["max_stress_drawdown_pct"]
        ),
        "mandate_allowed_symbols": list(mandate_cfg.get("allowed_symbols", [])),
    }
    updated_at = None
    updated_by = None
    override_path = _risk_override_path(audit_path)
    if override_path.exists():
        try:
            with open(override_path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            if isinstance(ov, dict):
                for k, v in ov.items():
                    if k in RISK_CONFIG_DEFAULTS:
                        merged[k] = v
                updated_at = ov.get("_updated_at")
                updated_by = ov.get("_updated_by")
        except Exception:
            pass  # Malformed override file = ignore, use config.yaml fallback
    merged["_override_updated_at"] = updated_at
    merged["_override_updated_by"] = updated_by
    return merged


def write_risk_config(
    updates: Dict[str, Any],
    updated_by: str = "dashboard",
    config: Optional[dict] = None,
    audit_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge `updates` (partial dict of risk/mandate fields) into
    `audit/risk_config_override.json`. Mirrors `write_trading_config`.
    """
    override_path = _risk_override_path(audit_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    existing: Dict[str, Any] = {}
    if override_path.exists():
        try:
            with open(override_path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    for k, v in updates.items():
        if k in RISK_CONFIG_DEFAULTS:
            existing[k] = v
    existing["_updated_at"] = time.time()
    existing["_updated_by"] = updated_by
    tmp = override_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    tmp.replace(override_path)  # atomic on POSIX
    return read_risk_config(config=config, audit_path=audit_path)


# ----------------------------------------------------------------------
# State snapshot
# ----------------------------------------------------------------------

def build_state_snapshot(
    config: Optional[dict] = None,
    audit_path: str = "audit/audit.jsonl",
    positions_path: str = "data_store/positions.json",
) -> StateSnapshot:
    """Build the full dashboard snapshot.

    Reads:
      - `data_store/positions.json` (PositionRepository)
      - `audit/audit.jsonl` (AuditLedger, for daily/total realized P&L)
      - `audit/mode_override.json` (mode)
      - yfinance (live prices for open positions)

    Pure read-only — does NOT mutate any state file.
    """
    repo = PositionRepository(path=positions_path)
    opens = repo.open()
    assets = [p.asset for p in opens if p.asset]
    prices = read_current_prices(assets) if assets else {}
    summaries: List[PositionSummary] = []
    for p in opens:
        price = prices.get(p.asset)
        src = "live" if (price is not None and p.asset in prices) else "fetch_failed"
        summaries.append(_build_position_summary(p, price, src))
    total_unrealized = sum(
        s.unrealized_pnl_usd or 0.0 for s in summaries
    )
    total_exposure = sum(s.notional_usd for s in summaries)
    total_unrealized_pct = (total_unrealized / total_exposure) if total_exposure > 0 else 0.0

    # Realized P&L: from positions.closed() (preferred) or audit fallback
    daily_pnl = 0.0
    total_pnl = 0.0
    cutoff = time.time() - 24 * 3600
    for p in repo.all():
        if p.closed_ts is None or p.realized_pnl is None:
            continue
        total_pnl += p.realized_pnl
        if p.closed_ts >= cutoff:
            daily_pnl += p.realized_pnl

    mode = read_mode(config=config, audit_path=audit_path)

    # Sprint 46C: real per-broker balances. Best-effort — a broker
    # not being configured, or a transient network error, must never
    # break the whole /api/state response (the dashboard still needs
    # positions/P&L even if a balance call fails).
    binance_bal, binance_src = _get_binance_balance()
    alpaca_bal, alpaca_src = _get_alpaca_balance()

    return StateSnapshot(
        mode=mode,
        # Legacy field, kept for API back-compat: mirrors whichever
        # broker actually has a live balance, preferring binance.us
        # since that's the bot's primary/always-on broker.
        balance_usd=binance_bal if binance_bal is not None else (alpaca_bal or 0.0),
        balance_source=binance_src if binance_bal is not None else alpaca_src,
        binance_balance_usd=binance_bal,
        binance_balance_source=binance_src,
        alpaca_balance_usd=alpaca_bal,
        alpaca_balance_source=alpaca_src,
        positions=summaries,
        open_count=len(summaries),
        total_unrealized_usd=total_unrealized,
        total_unrealized_pct=total_unrealized_pct,
        total_exposure_usd=total_exposure,
        daily_realized_pnl_usd=daily_pnl,
        total_realized_pnl_usd=total_pnl,
        last_update_ts=time.time(),
    )


def build_audit(
    limit: int = 100,
    after: Optional[float] = None,
    event_type: Optional[str] = None,
    audit_path: str = "audit/audit.jsonl",
) -> List[AuditEvent]:
    """Return recent audit events, newest first.

    Args:
        limit: cap on returned events (default 100, max 1000).
        after: only events with `ts >= after` (for live tailing).
        event_type: only events of this type (optional filter).
    """
    limit = max(1, min(int(limit), 1000))
    audit = AuditLedger(path=audit_path)
    if after is not None:
        rows = audit.read_since(after)
    else:
        rows = audit.read_all()
    if event_type is not None:
        rows = [r for r in rows if r.get("event_type") == event_type]
    rows = sorted(rows, key=lambda r: r.get("ts", 0.0), reverse=True)
    rows = rows[:limit]
    return [AuditEvent.from_row(r) for r in rows]


def close_position(
    position_id: str,
    audit_path: str = "audit/audit.jsonl",
    positions_path: str = "data_store/positions.json",
) -> Optional[Dict]:
    """Close an open position at its entry price (best-effort fallback).

    Real price discovery happens in the bot's PositionMonitor. This
    endpoint is for MANUAL operator-initiated closes from the
    dashboard. We use the entry price as a fallback because the
    dashboard doesn't have a live price feed as authoritative as the
    bot's. The trade-off: the operator gets an audit trail of the
    manual action, but the realized_pnl is 0.0 until the next bot
    cycle reconciles via PositionMonitor.

    Returns the closed position dict on success, None if the position
    doesn't exist or was already closed.
    """
    repo = PositionRepository(path=positions_path)
    target = None
    for p in repo.open():
        if p.position_id == position_id:
            target = p
            break
    if target is None:
        return None
    closed = repo.close_position(position_id, close_price=target.entry_price, reason="MANUAL_CLOSE_VIA_API")
    if closed is None:
        return None
    audit = AuditLedger(path=audit_path)
    audit.append("MANUAL_CLOSE", {
        "position_id": position_id,
        "asset": closed.asset,
        "direction": closed.direction,
        "entry_price": closed.entry_price,
        "close_price": closed.entry_price,    # fallback; bot will reconcile
        "reason": "MANUAL_CLOSE_VIA_API",
        "via": "api",
    })
    return {
        "position_id": closed.position_id,
        "asset": closed.asset,
        "direction": closed.direction,
        "entry_price": closed.entry_price,
        "close_price": closed.entry_price,
        "realized_pnl_usd": closed.realized_pnl or 0.0,
    }


def close_all_positions(
    audit_path: str = "audit/audit.jsonl",
    positions_path: str = "data_store/positions.json",
) -> List[Dict]:
    """Sprint 46H: bulk version of `close_position` — Carlos's ask:
    "que se puedan detener las entradas que están abiertas, y así
    quede la sesión completamente limpia" before flipping from paper
    to live. Loops the SAME per-position close logic `close_position`
    already uses (repo-only close at entry_price, audit-logged as
    MANUAL_CLOSE) — no new order logic, just applied to every open
    position instead of one.

    Same trade-off as `close_position`: this clears the LOCAL repo
    (correct and sufficient in PAPER mode, where paper positions never
    existed on a real exchange anyway). In LIVE mode this does NOT
    place real exchange orders — see `close_position`'s docstring.
    Carlos's stated use case is specifically "mientras esté en paper",
    so the dashboard should only surface this action prominently in
    paper mode (still callable in live, just not the intended flow).

    Returns the list of closed-position dicts (same shape as
    `close_position`'s return value), in the order they were closed.
    """
    repo = PositionRepository(path=positions_path)
    position_ids = [p.position_id for p in repo.open()]
    closed_list: List[Dict] = []
    for pid in position_ids:
        closed = close_position(pid, audit_path=audit_path, positions_path=positions_path)
        if closed is not None:
            closed_list.append(closed)
    return closed_list


# ----------------------------------------------------------------------
# Manual trading pause (Sprint 46H) — dashboard Stop/Start toggle
# ----------------------------------------------------------------------
#
# Carlos: "en el dashboard hay manera de tener como un stop y un start?
# para que mientras esté en paper se puedan detener las entradas que
# están abiertas, y así quede la sesión completamente limpia y a la
# hora de pasarlo a live el sistema pueda correr limpio, sin la
# posibilidad de un bug que el sistema crea que tiene alguna posición
# abierta."
#
# Same override-file pattern as mode_override.json / *_config_override
# .json — but checked EVERY CYCLE (not just at startup), same as
# mode_override.json's mandate_enabled flag: main.py's job_with_monitor
# reads this file each cycle and, if paused, skips ONLY step 2 (new
# entries via the normal workflow). Step 1 (PositionMonitor — SL/TP,
# smart profit-take) ALWAYS runs regardless, exactly like the existing
# drawdown-kill-switch and capital-routing gates — pausing new entries
# must never also pause protection on positions already open.
#
# This does NOT touch the filesystem KillSwitch (src/safety/kill_switch
# .py) on purpose: that one is checked at bot STARTUP too and refuses
# to even boot the trading loop while armed (main.py's kill_switch.
# is_triggered() gate) — a much harder stop than "pause new entries,
# keep monitoring what's open," and arming it while positions are open
# would leave them unprotected across a restart. This pause flag never
# affects startup at all.

def _trading_pause_path(audit_path: Optional[str] = None) -> Path:
    if audit_path is None:
        audit_path = os.getenv("DASHBOARD_AUDIT_PATH", "audit/audit.jsonl")
    return Path(audit_path).parent / "trading_pause.json"


def read_trading_pause(audit_path: Optional[str] = None) -> Dict[str, Any]:
    """Current pause state: {"paused": bool, "paused_at": float|None,
    "paused_by": str|None}. Defaults to not-paused if the file doesn't
    exist or is malformed (fail-open — same rationale as every other
    override file in this codebase: a missing/corrupt override file
    must fall back to normal operation, not an unexpected halt)."""
    path = _trading_pause_path(audit_path)
    if not path.exists():
        return {"paused": False, "paused_at": None, "paused_by": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"paused": False, "paused_at": None, "paused_by": None}
        return {
            "paused": bool(data.get("paused", False)),
            "paused_at": data.get("paused_at"),
            "paused_by": data.get("paused_by"),
        }
    except Exception:
        return {"paused": False, "paused_at": None, "paused_by": None}


def write_trading_pause(
    paused: bool,
    updated_by: str = "dashboard",
    audit_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Set the pause flag. Atomic write (tmp file + `.replace()`), same
    as every other override file here. Takes effect on the bot's NEXT
    cycle — no restart needed, unlike trading_config_override.json /
    risk_config_override.json (which main.py only reads at startup)."""
    path = _trading_pause_path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "paused": bool(paused),
        "paused_at": time.time() if paused else None,
        "paused_by": updated_by,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX
    return read_trading_pause(audit_path)
