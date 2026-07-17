"""
Sprint 46A â€” State snapshot builder for the bot HTTP API.

Pure read-only layer that turns the bot's on-disk state (positions
JSON, audit JSONL, mode_override JSON) + a live price snapshot into
typed Pydantic models the REST/WS endpoints can serve.

Design principle
----------------
The bot writes state to disk (positions, audit, mode). The API
layer does NOT mutate the state â€” it only reads. Mutations (close
position, toggle mode) go through dedicated POST endpoints in
server.py that write to the same on-disk files the bot already reads.
That keeps the bot logic untouched.

What this module does
---------------------
- `build_state_snapshot()` â€” full snapshot for the dashboard's
  initial load: positions + P&L + balance + mode + counts.
- `build_positions()` â€” list of positions with computed current
  P&L (calls yfinance for live prices; falls back to entry price
  if fetch fails, with `current_price_source` flag so the UI can
  show it appropriately).
- `build_audit()` â€” recent audit events (filterable by since/until).
- `read_mode()` â€” current mode from `audit/mode_override.json` or
  config.yaml fallback.
- `write_mode()` â€” toggle mode (writes the override file; the bot
  re-reads it on next cycle via B033 paper-mode gate in the broker,
  and on next startup for the main loop).
- `read_current_prices()` â€” live prices via yfinance with a 30s
  in-process cache so we don't hammer the API.

Threading model
---------------
This module is sync. The FastAPI app can run the heavy lifting
(yfinance fetch) in a thread pool via `run_in_threadpool` from
`fastapi.concurrency`. The state files are read in the request
handler â€” short operations, no need for a worker process.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

import pandas as pd

from src.data_store.positions import PositionRepository, Position
from src.safety.audit_ledger import AuditLedger
from src.core.atomic_write import atomic_write_text
from src.core.logging_setup import get_logger

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# Pydantic response models
# ----------------------------------------------------------------------

# Pydantic models for the API surface. Kept here (not in server.py) so
# the snapshot builder can return them directly and the server can
# just `return` them â€” FastAPI serializes them automatically.
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
    """Full dashboard snapshot â€” what the home page requests once on load."""
    mode: ModeInfo
    balance_usd: float = 0.0
    balance_source: str = "unknown"                 # "broker" | "testnet_sim" | "live"
    # Sprint 46C: per-broker available cash. `balance_usd` above was
    # ALWAYS 0.0/"unknown" â€” nothing ever populated it, so the
    # dashboard never showed real money available, even though both
    # BINANCE_API_KEY/SECRET and ALPACA_API_KEY/SECRET_KEY were set.
    # These two fields are populated from the SAME broker instances
    # main.py already constructed for trading (passed in via
    # `set_brokers()`), so no duplicate exchange connections are made.
    binance_balance_usd: Optional[float] = None
    binance_balance_source: str = "unavailable"      # "live" | "unavailable" | "not_configured"
    alpaca_balance_usd: Optional[float] = None
    alpaca_balance_source: str = "unavailable"       # "live" | "unavailable" | "not_configured"
    # Sprint 62: paper-mode simulation fields. In paper mode, the bot
    # uses a virtual starting balance (config.paper.starting_balance_usd)
    # and accumulates simulated P&L in the equity tracker. The dashboard
    # shows this as the "Effective balance" instead of the real broker
    # balance (which is irrelevant for sizing in paper mode).
    #
    # - `effective_balance_usd`: the number the bot actually uses for
    #   position sizing. In live mode = real broker balance. In paper
    #   mode = paper starting balance + realized P&L from the equity
    #   tracker.
    # - `effective_balance_source`: "broker_live" | "paper_simulated".
    # - `paper_starting_balance_usd`: the configured paper starting
    #   balance (only set in paper mode; null in live mode).
    effective_balance_usd: Optional[float] = None
    effective_balance_source: str = "broker_live"     # "broker_live" | "paper_simulated"
    paper_starting_balance_usd: Optional[float] = None
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


def _load_cache_ttls() -> Dict[str, float]:
    """Sprint 46Y (audit B2 resto): read cache TTLs from config.yaml.

    Used to replace the hard-coded `PRICE_CACHE_TTL_S = 30.0` and
    `BALANCE_CACHE_TTL_S = 15.0` constants. Fail-open: any I/O error
    (missing file, malformed YAML, missing keys) returns the
    pre-46Y defaults — the bot should still boot if config is broken.
    """
    # price_ttl_s lowered from 30s: prices now come from the live broker
    # ticker (ccxt/Alpaca) for the common case, not yfinance, so a much
    # shorter cache still avoids hammering either API while letting the
    # dashboard's P&L visibly move every couple of seconds instead of
    # holding the same number for half a minute.
    defaults = {"price_ttl_s": 3.0, "balance_ttl_s": 15.0}
    try:
        cfg_path = Path("config.yaml")
        if not cfg_path.exists():
            return defaults
        with cfg_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        cache_cfg = cfg.get("cache", {}) or {}
        return {**defaults, **{k: float(v) for k, v in cache_cfg.items() if k in defaults}}
    except Exception:
        return defaults


_CACHE_TTLS: Dict[str, float] = _load_cache_ttls()
PRICE_CACHE_TTL_S: float = _CACHE_TTLS["price_ttl_s"]


def _fetch_one_price(asset: str) -> Tuple[Optional[float], str]:
    """Fetch the freshest price available for one asset.

    Real-time dashboard fix: this used to ALWAYS use yfinance's
    DAILY-CANDLE CLOSE (`interval="1d"`) — the same staleness bug
    `main.py::_fetch_prices_for_open_positions` was fixed for in
    Sprint 46N/A7 (SL/TP triggers), except that fix never touched
    this function, so the dashboard's displayed "current price" and
    unrealized P&L stayed pinned to (at best) today's daily close no
    matter how often the UI polled — the number could not visibly
    move within a session even though the underlying asset was.

    Now tries the SAME live broker feed that will execute the
    position's eventual close, before falling back to yfinance:
      - crypto -> `_BROKER_CLIENT.get_ticker_price` (ccxt `fetch_ticker`
        against binance.us — sub-second last-traded price)
      - equity -> `_ALPACA_BROKER.get_latest_trade_price` (Alpaca's own
        market-data API)
    Falls back to yfinance 1-MINUTE bars (not daily) only when the
    relevant broker isn't configured or the live call fails —
    intraday close is still far fresher than a daily close, and this
    keeps prices flowing (e.g. in paper mode with no live broker keys)
    rather than going blank.

    Returns (price, source) where source is 'live' on a fresh fetch
    and 'fetch_failed' if every path returned no data.
    """
    from src.data.asset_class import get_asset_class, AssetClass

    try:
        if get_asset_class(asset) == AssetClass.CRYPTO:
            if _BROKER_CLIENT is not None:
                ccxt_symbol = asset.replace("-", "/") if "-" in asset else asset
                if "/" not in ccxt_symbol:
                    ccxt_symbol = f"{ccxt_symbol}/USDT"
                price = _BROKER_CLIENT.get_ticker_price(ccxt_symbol)
                if price is not None and float(price) > 0:
                    return float(price), "live"
        else:
            if _ALPACA_BROKER is not None:
                price = _ALPACA_BROKER.get_latest_trade_price(asset)
                if price is not None and float(price) > 0:
                    return float(price), "live"
    except Exception:
        pass  # fall through to the yfinance fallback below

    from src.data.yf_safe import safe_yf_download  # local to avoid import cost at module load
    try:
        df = safe_yf_download(asset, period="1d", interval="1m")
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
    failures are simply omitted from the result â€” the caller can
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
# caller ever set it to anything other than its 0.0 default â€” the API
# never had a reference to the bot's actual broker connections. This
# module registers the SAME BrokerClient/AlpacaBroker instances main.py
# already built for trading (see `set_brokers()`, called once from
# `main.py::_start_api_server`), so balance calls reuse the existing
# authenticated connections instead of opening new ones with duplicate
# credentials/rate-limit budget.
#
# Cached with a short TTL â€” balance doesn't need to be sub-second-live
# and both `get_usdt_balance()`/`get_usd_balance()` are real network
# calls to the exchange/broker.

_BROKER_CLIENT = None      # BrokerClient (binance.us / ccxt) or None
_ALPACA_BROKER = None      # AlpacaBroker or None (not configured if None)

BALANCE_CACHE_TTL_S: float = _CACHE_TTLS["balance_ttl_s"]
_BALANCE_CACHE: Dict[str, Tuple[Optional[float], str, float]] = {}
"""broker_name -> (balance_or_None, source, fetched_at)."""


def set_brokers(broker_client=None, alpaca_broker=None) -> None:
    """Register the bot's live broker instances for balance lookups.

    Called once from `main.py::_start_api_server`, right before the
    uvicorn thread starts, with the exact same `broker_client` /
    `alpaca_broker` objects the trading loop uses. Safe to call with
    both None (e.g. in tests, or if neither broker is configured) â€”
    every consumer below treats a None broker as "not configured"
    rather than raising.
    """
    global _BROKER_CLIENT, _ALPACA_BROKER
    _BROKER_CLIENT = broker_client
    _ALPACA_BROKER = alpaca_broker


def get_broker_client():
    """Return the bot's shared crypto `BrokerClient` (binance.us/ccxt)
    if one was registered via `set_brokers`, else None.

    Used by `set_mode()` in server.py to run `PaperToLiveChecklist`
    against the SAME broker instance the trading loop uses, instead of
    building a throwaway one — same rationale as `get_position_repo`.
    """
    return _BROKER_CLIENT


# Sprint 46N (audit C8): the bot's own live PositionRepository, shared
# with the dashboard API instead of each request building its own
# disk-backed copy. See `set_position_repo`'s docstring for the full
# "resurrected position" bug this fixes.
_POSITION_REPO: Optional[PositionRepository] = None


def set_position_repo(position_repo) -> None:
    """Register the bot's live PositionRepository for the dashboard
    API to share, instead of every request constructing its own
    disk-backed copy.

    Called once from `main.py::_start_api_server`, right before the
    uvicorn thread starts, with the EXACT SAME `position_repo` object
    the trading loop (fast_monitor_tick / job_with_monitor /
    ExecutionNode) uses.

    The bug this fixes ("resurrected" positions): before this, every
    mutating API request (`close_position`, `close_all_positions`)
    built a FRESH `PositionRepository(path=positions_path)` â€” read
    whatever was on disk at that instant, closed the target position
    on THAT throwaway copy, and saved. The bot's own long-lived
    in-memory repo never learned about that close â€” it still held the
    position as OPEN in its `self.positions` list. The next time
    ANYTHING triggered the bot's own `_save()` (opening a new
    position, `fast_monitor_tick` closing a different position, an OCO
    reconciliation, etc.), it overwrote `positions.json` with its
    stale in-memory state â€” silently undoing the dashboard's close and
    bringing the "closed" position back as open on disk.

    Because the dashboard API runs in a background thread INSIDE THE
    SAME PROCESS as the bot (see `main.py::_start_api_server`'s
    docstring), there's no need for cross-process IPC to fix this â€”
    both sides can safely share the literal same Python object. Once
    shared, a dashboard close mutates the EXACT list the bot's own
    scheduler will later save, so there is no second stale copy left
    to resurrect anything from. `PositionRepository`'s internal
    `threading.RLock` (also Sprint 46N C8) makes concurrent access
    from the bot's scheduler thread and uvicorn's request-handling
    thread(s) safe.

    Safe to call with None (e.g. in tests, or the API running without
    a live bot process) â€” every consumer below falls back to
    constructing its own disk-backed instance via `get_position_repo`,
    same behavior as before this existed.
    """
    global _POSITION_REPO
    _POSITION_REPO = position_repo


def get_position_repo(positions_path: str = "data_store/positions.json") -> PositionRepository:
    """Return the bot's shared PositionRepository if one was
    registered via `set_position_repo`, otherwise a fresh disk-backed
    instance. See `set_position_repo`'s docstring for the rationale.
    Used by every PositionRepository consumer in this module and in
    `server.py` so reads/writes are consistent with whichever mode
    (shared live instance vs. standalone disk read) is active.
    """
    if _POSITION_REPO is not None:
        return _POSITION_REPO
    return PositionRepository(path=positions_path)


def _get_binance_balance() -> Tuple[Optional[float], str]:
    """Return (balance, source) for the binance.us broker.

    source is one of: "live" (fresh fetch), "cache" (within TTL),
    "unavailable" (broker configured but the call failed â€” e.g.
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
        # Don't cache failures â€” retry on the next request instead of
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
        # Fallback to entry price â€” P&L is 0, source flagged.
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
    atomic_write_text(override_path, json.dumps(payload, indent=2))  # Sprint 46R (audit B8): fsync before rename
    return read_mode(audit_path=audit_path)


# ----------------------------------------------------------------------
# Trading config (Sprint 46D) â€” dashboard-editable trading settings
# ----------------------------------------------------------------------
#
# Same pattern as mode_override.json above: config.yaml is the file
# Carlos edits by hand (with comments explaining every field), so the
# API never rewrites it directly â€” PyYAML's dump() isn't round-trip
# safe and would silently destroy every comment in the file. Instead,
# dashboard edits go to `audit/trading_config_override.json`, a flat
# JSON of {field: value} that OVERLAYS config.yaml's `trading:` section
# at read time. Whatever's in the override file wins.
#
# IMPORTANT caveat (surfaced to the user via `pending_restart` in the
# API response): `main.py` reads `trading_cfg` (config.yaml merged with
# this override file) ONCE at startup and passes the individual values
# into RiskManagerAgent's constructor â€” they are NOT re-read per cycle.
# So a saved change here only takes effect after the bot restarts
# (POST /api/restart). This mirrors the existing mode-toggle behavior
# for the main loop's mandate check (see SetModeResponse.note in
# server.py) â€” nothing new architecturally, just extended to cover all
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

    Only keys present in TRADING_CONFIG_DEFAULTS are persisted â€”
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
    atomic_write_text(override_path, json.dumps(existing, indent=2))  # Sprint 46R (audit B8): fsync before rename
    return read_trading_config(config=config, audit_path=audit_path)


# ----------------------------------------------------------------------
# Risk / mandate config (Sprint 46F) â€” dashboard-editable safety gates
# ----------------------------------------------------------------------
#
# Same override-file pattern as trading config above, covering the
# other two places safety-relevant numbers live in config.yaml:
#   - `risk:` â€” drawdown kill-switch threshold/cooldown, plus the
#     Sprint 44/45 portfolio-risk gate caps (asset-class concentration,
#     correlation, CVaR, stress-test) that RiskManagerAgent has always
#     supported as constructor params but main.py never actually read
#     from config.yaml at all (they silently used the class's
#     hard-coded defaults, so hand-editing config.yaml wouldn't even
#     have changed them before this sprint).
#   - `mandate.allowed_symbols` â€” the symbol allow-list the MandateGate
#     enforces. Kept separate from the other mandate fields
#     (max_position_usd/max_daily_loss_usd/max_total_exposure_usd) â€”
#     those are intentionally NOT exposed here yet; ask if you want
#     them added too, same mechanism.
#   - `mandate.max_daily_trades` (Sprint 46J) â€” new-entry rate limit,
#     0 = unlimited. Exposed here alongside allowed_symbols using the
#     same "special-case mandate.* field" pattern (see the merge loop
#     in main.py's Sprint 46F risk-override block).
#
# Like trading config, this is READ at startup only â€” a saved change
# needs a bot restart (POST /api/restart) to take effect.

RISK_CONFIG_DEFAULTS: Dict[str, Any] = {
    "drawdown_kill_threshold_pct": 15.0,
    "drawdown_cooldown_hours": 24.0,
    "max_asset_class_concentration_pct": 60.0,
    "max_avg_correlation_pct": 75.0,
    "max_cvar_95_pct": 20.0,
    "max_stress_drawdown_pct": 70.0,
    "mandate_allowed_symbols": [],
    "max_daily_trades": 0,
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
    layered on top. Mirrors `read_trading_config` â€” see that function
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
        "max_daily_trades": int(mandate_cfg.get("max_daily_trades", RISK_CONFIG_DEFAULTS["max_daily_trades"])),
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
    atomic_write_text(override_path, json.dumps(existing, indent=2))  # Sprint 46R (audit B8): fsync before rename
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

    Pure read-only â€” does NOT mutate any state file.
    """
    repo = get_position_repo(positions_path)
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
    is_paper_mode = (mode.mode == "paper")

    # Sprint 62: paper-mode effective balance. In paper mode, the bot
    # uses `config.paper.starting_balance_usd` as the simulated
    # starting balance and adds accumulated realized P&L. The
    # dashboard's "Effective balance" card shows this number, so the
    # user sees a meaningful paper account (e.g. $1,000 minus any
    # simulated losses) instead of the real $22.08 broker balance.
    paper_starting_usd: Optional[float] = None
    effective_balance_usd: Optional[float] = None
    effective_balance_source = "broker_live"
    if is_paper_mode:
        _paper_cfg = (config or {}).get("paper") or {}
        try:
            paper_starting_usd = float(_paper_cfg.get("starting_balance_usd", 1000.0))
            if paper_starting_usd <= 0:
                paper_starting_usd = 1000.0
        except (TypeError, ValueError):
            paper_starting_usd = 1000.0
        # Effective paper balance = starting + realized P&L (not
        # including unrealized — that already lives in its own KPI
        # card on the dashboard). This is the same formula the
        # EquityTracker uses internally.
        effective_balance_usd = paper_starting_usd + total_pnl
        effective_balance_source = "paper_simulated"

    # Sprint 46C: real per-broker balances. Best-effort â€” a broker
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
        # Sprint 62: paper-mode effective balance (see above).
        effective_balance_usd=effective_balance_usd,
        effective_balance_source=effective_balance_source,
        paper_starting_balance_usd=paper_starting_usd,
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
    fee_pct: float = 0.0,
    fee_pct_for_asset=None,
) -> Optional[Dict]:
    """Close an open position at its entry price (best-effort fallback).

    Sprint 46N (audit M2): applies the same real binance.us round-trip
    fee PositionMonitor's SL/TP and smart-profit-take closes already
    account for (Sprint 46J) and RiskManagerAgent's replacement closes
    now account for too. Since this manual close always uses
    close_price == entry_price (no live price feed here), gross_pnl is
    always 0 -- applying a nonzero fee simply records the small
    realized LOSS the fee itself represents, instead of silently
    reporting a perfect break-even that never actually happens on a
    real exchange.

    Two ways to supply the fee (both default to fee-free, the old
    behavior): a flat `fee_pct` (fine when the caller already knows
    this is a single crypto position), or `fee_pct_for_asset` -- an
    optional callable `f(asset: str) -> float`, resolved against THIS
    position's actual asset once it's looked up below. If both are
    given, `fee_pct_for_asset` wins (it's asset-aware; a flat `fee_pct`
    passed alongside it would just be a stale/wrong guess for this
    specific asset).

    Real price discovery happens in the bot's PositionMonitor. This
    endpoint is for MANUAL operator-initiated closes from the
    dashboard. We use the entry price as a fallback because the
    dashboard doesn't have a live price feed as authoritative as the
    bot's. The trade-off: the operator gets an audit trail of the
    manual action, but the realized_pnl is 0.0 until the next bot
    cycle reconciles via PositionMonitor.

    This is a REPO-ONLY close (same as always â€” this endpoint never
    talks to the broker for a fresh market sell, live or paper). That
    trade-off is fine for the documented "clean paper session before
    going live" use case, but Sprint 46I adds one exception: if the
    position has a real resting OCO order (protection_mode ==
    "native_oco"), we CANCEL it here first. Without that, marking the
    position closed in the repo while the exchange still has a live
    OCO order resting would create the mirror-image of the "ghost
    position" bug this session has been fixing all along â€” a phantom
    CLOSE instead of a phantom OPEN: the bot stops tracking it
    (exposure/mandate no longer count it) while the exchange could
    still fill that OCO order later, completely outside the bot's
    view. NOTE: canceling the OCO does NOT sell the underlying asset â€”
    if this was a real LIVE position, the crypto itself is still held
    in the account, just no longer protected or tracked by the bot.

    Returns the closed position dict on success (with an added
    `oco_cancelled` bool and, if applicable, a `note` warning about the
    still-held asset), or None if the position doesn't exist or was
    already closed.
    """
    repo = get_position_repo(positions_path)
    target = None
    for p in repo.open():
        if p.position_id == position_id:
            target = p
            break
    if target is None:
        return None

    oco_cancelled = False
    note = None
    if target.protection_mode == "native_oco" and target.broker_oco_order_id:
        if _BROKER_CLIENT is not None and hasattr(_BROKER_CLIENT, "cancel_oco_order"):
            try:
                symbol = target.asset.replace("-", "/") if "-" in target.asset else target.asset
                _BROKER_CLIENT.cancel_oco_order(symbol, target.broker_oco_order_id)
                oco_cancelled = True
            except Exception:
                pass
        note = (
            "This position had a real exchange-side OCO order. The dashboard's "
            "close action does not place a market sell â€” the underlying asset "
            "may still be held on the exchange. Verify on binance.us directly "
            "if you intended to fully exit the market position."
        )

    effective_fee_pct = fee_pct
    if fee_pct_for_asset is not None:
        try:
            effective_fee_pct = float(fee_pct_for_asset(target.asset) or 0.0)
        except Exception:
            effective_fee_pct = 0.0

    closed = repo.close_position(
        position_id, close_price=target.entry_price, reason="MANUAL_CLOSE_VIA_API",
        fee_pct=effective_fee_pct,
    )
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
        "oco_cancelled": oco_cancelled,
    })
    result = {
        "position_id": closed.position_id,
        "asset": closed.asset,
        "direction": closed.direction,
        "entry_price": closed.entry_price,
        "close_price": closed.entry_price,
        "realized_pnl_usd": closed.realized_pnl or 0.0,
        "oco_cancelled": oco_cancelled,
    }
    if note:
        result["note"] = note
    return result


def close_all_positions(
    audit_path: str = "audit/audit.jsonl",
    positions_path: str = "data_store/positions.json",
    fee_pct_for_asset=None,
) -> List[Dict]:
    """Sprint 46H: bulk version of `close_position` â€” Carlos's ask:
    "que se puedan detener las entradas que estÃ¡n abiertas, y asÃ­
    quede la sesiÃ³n completamente limpia" before flipping from paper
    to live. Loops the SAME per-position close logic `close_position`
    already uses (repo-only close at entry_price, audit-logged as
    MANUAL_CLOSE) â€” no new order logic, just applied to every open
    position instead of one.

    Same trade-off as `close_position`: this clears the LOCAL repo
    (correct and sufficient in PAPER mode, where paper positions never
    existed on a real exchange anyway). In LIVE mode this does NOT
    place real exchange orders â€” see `close_position`'s docstring.
    Carlos's stated use case is specifically "mientras estÃ© en paper",
    so the dashboard should only surface this action prominently in
    paper mode (still callable in live, just not the intended flow).

    Returns the list of closed-position dicts (same shape as
    `close_position`'s return value), in the order they were closed.

    Sprint 46N (audit M2): `fee_pct_for_asset` (optional callable,
    `f(asset: str) -> float`, same contract as PositionMonitor's) lets
    the caller apply a PER-POSITION fee -- unlike `close_position`'s
    flat `fee_pct` (fine for a single asset), a bulk close can mix
    crypto (real taker fee) and equities (commission-free), so a single
    shared rate would either overcharge equities or undercharge crypto.
    """
    repo = get_position_repo(positions_path)
    position_ids = [p.position_id for p in repo.open()]
    closed_list: List[Dict] = []
    for pid in position_ids:
        closed = close_position(
            pid, audit_path=audit_path, positions_path=positions_path,
            fee_pct_for_asset=fee_pct_for_asset,
        )
        if closed is not None:
            closed_list.append(closed)
    return closed_list


# ----------------------------------------------------------------------
# Manual trading pause (Sprint 46H) â€” dashboard Stop/Start toggle
# ----------------------------------------------------------------------
#
# Carlos: "en el dashboard hay manera de tener como un stop y un start?
# para que mientras estÃ© en paper se puedan detener las entradas que
# estÃ¡n abiertas, y asÃ­ quede la sesiÃ³n completamente limpia y a la
# hora de pasarlo a live el sistema pueda correr limpio, sin la
# posibilidad de un bug que el sistema crea que tiene alguna posiciÃ³n
# abierta."
#
# Same override-file pattern as mode_override.json / *_config_override
# .json â€” but checked EVERY CYCLE (not just at startup), same as
# mode_override.json's mandate_enabled flag: main.py's job_with_monitor
# reads this file each cycle and, if paused, skips ONLY step 2 (new
# entries via the normal workflow). Step 1 (PositionMonitor â€” SL/TP,
# smart profit-take) ALWAYS runs regardless, exactly like the existing
# drawdown-kill-switch and capital-routing gates â€” pausing new entries
# must never also pause protection on positions already open.
#
# This does NOT touch the filesystem KillSwitch (src/safety/kill_switch
# .py) on purpose: that one is checked at bot STARTUP too and refuses
# to even boot the trading loop while armed (main.py's kill_switch.
# is_triggered() gate) â€” a much harder stop than "pause new entries,
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
    exist or is malformed (fail-open â€” same rationale as every other
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
    cycle â€” no restart needed, unlike trading_config_override.json /
    risk_config_override.json (which main.py only reads at startup)."""
    path = _trading_pause_path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "paused": bool(paused),
        "paused_at": time.time() if paused else None,
        "paused_by": updated_by,
    }
    # Sprint 46R (audit B8): use the shared atomic_write_text helper
    # so the trading-pause state file is fsync'd before the rename.
    atomic_write_text(path, json.dumps(data, indent=2))
    return read_trading_pause(audit_path)

