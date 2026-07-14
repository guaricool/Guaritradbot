"""
Sprint 46A — FastAPI HTTP + WebSocket server for the bot dashboard.

This is the BACKEND half of the dashboard refactor (replacing the
Streamlit monolith). The bot state lives on disk in
`data_store/positions.json` and `audit/audit.jsonl`; the API layer
reads it via `state.py` and exposes it as REST + WebSocket.

Endpoints
---------
Public (no auth):
  GET  /api/health                (Docker/Coolify healthcheck; no
                                    sensitive data, must work without
                                    a token since the healthcheck
                                    process can't log in)
  POST /api/auth/login            (password -> token)

Authenticated (Bearer token in Authorization header):
  GET  /api/state
  GET  /api/positions
  GET  /api/positions/{id}
  GET  /api/positions/{id}/candles
  GET  /api/audit
  GET  /api/signals              (heuristic scan of audit; not perfect)
  GET  /api/stats                (alias of /api/state; for Streamlit compat)
  GET  /api/mode
  GET  /api/config
  GET  /api/risk-config
  GET  /api/trading-pause
  GET  /api/allocation           (from src/data/asset_allocation.py)
  GET  /api/risk/stress
  GET  /api/risk/correlation
  GET  /api/risk/cvar
  GET  /api/equity               (equity curve, downsampled)
  POST /api/mode                 (set LIVE/PAPER)
  POST /api/config
  POST /api/risk-config
  POST /api/trading-pause
  POST /api/positions/{id}/close (manual close)
  POST /api/positions/close-all
  POST /api/restart              (graceful bot restart)

Sprint 46N (audit C5): every read endpoint used to be public — the
entire trading state (open positions, entry/SL/TP prices, realized
P&L, the audit ledger, risk/mandate config, even the LIVE/PAPER mode)
was readable by anyone who found the API's URL on the open internet,
no token required. Only the mutating endpoints were ever gated. Now
every endpoint that exposes account or trading data requires the same
Bearer token as the mutating endpoints; only /api/health (used by the
container healthcheck, which never sends a token) and
POST /api/auth/login (which HANDS OUT the token) remain public.

WebSocket:
  WS   /ws/live?token=...        (audit event tail + position P&L updates)

Run
---
    # Dev:
    uvicorn src.api.server:app --host 0.0.0.0 --port 8080 --reload

    # In the bot's main.py, run uvicorn in a daemon thread:
    import uvicorn
    from src.api.server import app
    t = threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info"),
        daemon=True,
    )
    t.start()
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from src.api import auth
from src.api.state import (
    AuditEvent,
    ModeInfo,
    PositionSummary,
    StateSnapshot,
    build_audit,
    build_state_snapshot,
    close_all_positions,
    close_position,
    get_position_repo,
    invalidate_price_cache,
    read_current_prices,
    read_mode,
    read_risk_config,
    read_trading_config,
    read_trading_pause,
    write_mode,
    write_risk_config,
    write_trading_config,
    write_trading_pause,
)

# ----------------------------------------------------------------------
# Paths and config
# ----------------------------------------------------------------------

# Default paths are computed at import time but each request resolves
# the live env var via the helpers below. This lets tests override
# the env vars between requests without re-importing the module.
DEFAULT_AUDIT_PATH = "audit/audit.jsonl"
DEFAULT_POSITIONS_PATH = "data_store/positions.json"
DEFAULT_CONFIG_PATH = "config.yaml"

def _audit_path() -> str:
    return os.getenv("DASHBOARD_AUDIT_PATH", DEFAULT_AUDIT_PATH)

def _positions_path() -> str:
    return os.getenv("DASHBOARD_POSITIONS_PATH", DEFAULT_POSITIONS_PATH)

def _config_path() -> str:
    return os.getenv("DASHBOARD_CONFIG_PATH", DEFAULT_CONFIG_PATH)

def _load_config() -> dict:
    """Load config.yaml if present; return {} on any error.

    We don't fail-fast because the API should still serve a degraded
    state (no allocation policy, no risk metrics) rather than refuse
    to start if config.yaml is missing or malformed.
    """
    p = Path(_config_path())
    if not p.exists():
        return {}
    try:
        import yaml  # local; not all deployments need it
        with open(p, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def _fee_pct_for_asset(asset: str) -> float:
    """Sprint 46N (audit M2): the same real binance.us round-trip fee
    the bot itself charges on SL/TP, smart-profit-take, and position-
    replacement closes (`trading.crypto_taker_fee_pct` in config.yaml,
    default 0.001 = 0.1%; see main.py's `_fee_pct_for_asset` closure
    for the bot-side twin of this function), applied here so a manual
    dashboard close records the same realistic cost instead of a
    fee-free 0.0. Crypto only -- Alpaca equities are commission-free,
    so anything `get_asset_class` doesn't classify as CRYPTO gets 0.0
    (same conservative default used everywhere else in the bot).
    Best-effort: any error loading config.yaml just means 0.0 (never
    block a manual close over a config read failure).
    """
    try:
        from src.data.asset_class import get_asset_class, AssetClass
        if get_asset_class(asset) != AssetClass.CRYPTO:
            return 0.0
        cfg = _load_config()
        return float((cfg.get("trading", {}) or {}).get("crypto_taker_fee_pct", 0.001))
    except Exception:
        return 0.0

# ----------------------------------------------------------------------
# Lifespan: load config once, share with requests
# ----------------------------------------------------------------------

APP_STATE: Dict[str, Any] = {}

@asynccontextmanager
async def _lifespan(app: FastAPI):
    APP_STATE["config"] = _load_config()
    APP_STATE["started_at"] = time.time()
    APP_STATE["ws_clients"]: Set[WebSocket] = set()
    # Sprint 57: parallel set of SSE client queues, fed by the
    # same `_broadcast` function. asyncio.Queue per client so a
    # slow consumer doesn't wedge the broadcaster.
    APP_STATE["sse_clients"]: Set[asyncio.Queue] = set()
    APP_STATE["last_audit_ts"] = 0.0
    APP_STATE["last_audit_poll"] = 0.0
    APP_STATE["audit_poll_interval_s"] = float(os.getenv("DASHBOARD_WS_POLL_INTERVAL_S", "1.0"))
    yield
    APP_STATE.clear()

# ----------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------

app = FastAPI(
    title="Guaritradbot Dashboard API",
    version="0.1.0",
    description="Sprint 46A — replaces the Streamlit dashboard with a real REST/WS backend.",
    lifespan=_lifespan,
)

# CORS (Sprint 46N — audit A9): this used to default to `"*"` (allow
# ANY origin) with `allow_credentials=True` — a browser tab open to
# any random website could make authenticated-looking cross-origin
# requests against this API. Fails CLOSED now: with no
# `DASHBOARD_CORS_ORIGINS` set, NO browser origin is allowed (the
# dashboard simply won't load data until the operator sets this to
# the dashboard's actual public URL, e.g.
# `DASHBOARD_CORS_ORIGINS=http://13.140.181.29:3050` per this repo's
# docker-compose.yml, or the Coolify-assigned hostname once TLS is
# added per A10) — same "must configure to unlock" pattern
# `DASHBOARD_PASSWORD` already uses elsewhere in this file.
#
# `allow_credentials` is now tied to whether any origin is actually
# configured: this API authenticates via a Bearer token the frontend
# attaches explicitly to each request (see dashboard/src/lib/api.ts),
# NOT via cookies, so there was never a need for
# `allow_credentials=True` in the first place — it only matters for
# cookie-based auth. Leaving it False removes an unnecessary CORS
# privilege regardless of how `DASHBOARD_CORS_ORIGINS` ends up
# configured.
_CORS_ORIGINS = [o.strip() for o in os.getenv("DASHBOARD_CORS_ORIGINS", "").split(",") if o.strip()]
if not _CORS_ORIGINS:
    print(
        "[server] ⚠️  DASHBOARD_CORS_ORIGINS is not set — no browser origin "
        "is allowed to call this API (fails closed). Set it to the "
        "dashboard's public URL (comma-separated if more than one) to "
        "unlock the dashboard, e.g. DASHBOARD_CORS_ORIGINS=http://13.140.181.29:3050"
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ----------------------------------------------------------------------
# Auth schemas
# ----------------------------------------------------------------------

class LoginRequest(BaseModel):
    password: str

class LoginResponse(BaseModel):
    token: str
    expires_in_s: int
    token_type: str = "Bearer"

class SetModeRequest(BaseModel):
    mode: str  # "live" | "paper"
    switched_by: Optional[str] = "api"

class SetModeResponse(BaseModel):
    mode: ModeInfo
    note: str = Field(
        default="",
        description=(
            "If the bot is currently running, the change takes effect within "
            "~1 cycle for the broker (B033 paper-mode gate reads the file on "
            "each call). For the main loop's mandate check, the bot must "
            "restart to re-read the override (Sprint 45 N1 / main.py init). "
            "If you need an immediate restart, use POST /api/restart."
        ),
    )

# Sprint 46D: dashboard-editable trading settings. Every field is
# Optional so the dashboard can PATCH just the one field the user
# changed rather than resend the whole form. Bounds mirror what
# RiskManagerAgent/the broker actually enforce or what's operationally
# sane — these are NOT arbitrary: e.g. `min_order_usd` has a floor of
# $10 because that's Binance.US's real exchange minimum (Carlos's own
# words: "lo minimo para una entrada es $10") — letting the dashboard
# save a lower value would just produce orders the exchange rejects.
class UpdateTradingConfigRequest(BaseModel):
    risk_per_trade_pct: Optional[float] = Field(default=None, gt=0, le=100)
    max_open_trades: Optional[int] = Field(default=None, ge=1, le=50)
    min_order_usd: Optional[float] = Field(default=None, ge=10.0)
    max_capital_per_trade_pct: Optional[float] = Field(default=None, gt=0, le=100)
    atr_stop_multiplier: Optional[float] = Field(default=None, gt=0)
    atr_take_profit_multiplier: Optional[float] = Field(default=None, gt=0)
    risk_reward_ratio: Optional[float] = Field(default=None, gt=0)
    enable_position_replacement: Optional[bool] = None
    replacement_score_threshold: Optional[float] = Field(default=None, ge=0, le=1)
    min_profit_to_protect: Optional[float] = Field(default=None, ge=0)
    updated_by: Optional[str] = "dashboard"

# Sprint 46F: dashboard-editable risk/mandate safety gates — the
# drawdown kill-switch threshold/cooldown, the mandate's symbol
# allow-list, and the Sprint 44/45 portfolio-risk gate caps
# (concentration/correlation/CVaR/stress). Bounds are generous but
# not unlimited — e.g. drawdown_cooldown_hours capped at 168 (1 week)
# so a typo can't accidentally pause the bot for a year.
class UpdateRiskConfigRequest(BaseModel):
    drawdown_kill_threshold_pct: Optional[float] = Field(default=None, gt=0, le=100)
    drawdown_cooldown_hours: Optional[float] = Field(default=None, gt=0, le=168)
    max_asset_class_concentration_pct: Optional[float] = Field(default=None, gt=0, le=100)
    max_avg_correlation_pct: Optional[float] = Field(default=None, gt=0, le=100)
    max_cvar_95_pct: Optional[float] = Field(default=None, gt=0, le=100)
    max_stress_drawdown_pct: Optional[float] = Field(default=None, gt=0, le=100)
    mandate_allowed_symbols: Optional[List[str]] = None
    # Sprint 46J: rolling-24h new-entry rate limit. 0 = unlimited.
    max_daily_trades: Optional[int] = Field(default=None, ge=0, le=1000)
    updated_by: Optional[str] = "dashboard"

# ----------------------------------------------------------------------
# WebSocket hub
# ----------------------------------------------------------------------

async def _broadcast(event: Dict[str, Any]) -> None:
    """Send `event` to all connected WebSocket clients AND all
    connected SSE clients. The two transports are kept in parallel
    (not migrated) so existing WebSocket clients keep working while
    new clients can opt into SSE via /api/events.

    Sprint 57: added the SSE fan-out because Traefik (Coolify's
    reverse proxy on this VPS) refuses HTTP/1.1 upgrade requests
    with 403 Forbidden -- the WebSocket path is broken at the proxy
    layer and is not fixable from the bot side. SSE is plain
    HTTP/1.1 chunked transfer, no upgrade, works with any proxy.
    The dashboard's use-live.ts now uses EventSource against
    /api/events; the WebSocket handler at /ws/live stays in
    place for back-compat (and to keep the option open for a
    non-Traefik deployment in the future).

    Slow clients (both transports) are dropped silently. Better
    to lose a stale client than to block the broadcaster on one
    slow reader.
    """
    dead_ws: List[WebSocket] = []
    payload = json.dumps(event, default=str)
    for ws in list(APP_STATE.get("ws_clients", set())):
        try:
            await ws.send_text(payload)
        except Exception:
            dead_ws.append(ws)
    for ws in dead_ws:
        APP_STATE["ws_clients"].discard(ws)

    # SSE fan-out. Each SSE client has its own asyncio.Queue; the
    # broadcaster puts the same payload string on every queue. The
    # SSE handler loop reads from its queue and writes
    # `data: <payload>\n\n` to the response. A full queue would
    # block the broadcaster; the per-client queue size is small
    # (16) so a slow consumer gets disconnected and reconnected
    # rather than wedging the broadcast loop.
    dead_sse: List[asyncio.Queue] = []
    for q in list(APP_STATE.get("sse_clients", set())):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead_sse.append(q)
    for q in dead_sse:
        try:
            APP_STATE["sse_clients"].discard(q)
        except Exception:
            pass

async def _audit_tail_loop() -> None:
    """Background task: poll the audit.jsonl and broadcast new events to WS.

    Started on FastAPI startup (lifespan). Polling at 1s is fine for
    a 7-trade-per-day bot; for higher cadence, switch to inotify or
    a real event-bus integration. The WebSocket clients only need
    sub-second updates for the position P&L (handled by the snapshot
    poll below) — audit events are fine at 1s.
    """
    last_ts = APP_STATE.get("last_audit_ts", 0.0)
    interval = APP_STATE.get("audit_poll_interval_s", 1.0)
    while True:
        try:
            events = build_audit(limit=50, after=last_ts, audit_path=_audit_path())
            for ev in events:
                last_ts = max(last_ts, ev.ts)
                await _broadcast({"type": "audit", "event": ev.model_dump()})
            APP_STATE["last_audit_ts"] = last_ts
        except Exception:
            pass  # best-effort; dashboard reconnect handles gaps
        await asyncio.sleep(interval)

async def _position_snapshot_loop() -> None:
    """Background task: every 2s, broadcast a fresh positions snapshot.

    The dashboard's "live P&L" use case doesn't need every audit
    event — it needs the up-to-date unrealized P&L per position. We
    compute the snapshot from the same on-disk state the bot writes
    and push it. 2s feels live without hammering the disk.
    """
    interval = 2.0
    while True:
        try:
            config = APP_STATE.get("config") or {}
            snap = build_state_snapshot(
                config=config, audit_path=_audit_path(), positions_path=_positions_path(),
            )
            await _broadcast({
                "type": "positions",
                "positions": [p.model_dump() for p in snap.positions],
                "total_unrealized_usd": snap.total_unrealized_usd,
                "total_unrealized_pct": snap.total_unrealized_pct,
                "open_count": snap.open_count,
                "ts": snap.last_update_ts,
            })
        except Exception:
            pass
        await asyncio.sleep(interval)

@app.on_event("startup")
async def _start_background_tasks() -> None:
    APP_STATE.setdefault("ws_clients", set())
    asyncio.create_task(_audit_tail_loop())
    asyncio.create_task(_position_snapshot_loop())

# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------

@app.get("/api/health")
def health() -> Dict[str, Any]:
    """Sprint 46R audit M11.3: real health check, not a liveness ping.

    Pre-46R the endpoint just returned {"ok": True, ...}, which a
    Docker healthcheck would happily accept even if the bot's main
    loop had deadlocked hours ago — exactly the audit's concern
    ("el healthcheck del bot es pgrep — pasa aunque el bot esté
    colgado"). The endpoint now actively reports:

      1. last_analysis_cycle_at   - when the hourly analysis cycle
         last completed (set by main.py's job_with_monitor wrapper).
         If > 2× the configured run_interval_hours, the bot is
         effectively dead and we return 503 so Docker restarts it.
      2. last_fast_monitor_at     - same idea for the 2-minute
         position-protect tick. A dead fast_monitor means SL/TP is
         unprotected for open positions (audit A5 was the original
         concern that motivated the separate thread).
      3. audit_writable           - we actually try to append a
         test event to audit.jsonl, then remove it. A read-only
         filesystem (e.g. bot_audit volume full, or perms wrong)
         would otherwise surface only when Carlos tried to read
         the ledger and saw it was hours stale.

    Returns 200 if everything green, 503 if any check failed.
    The body always has the per-check booleans + last-* timestamps
    so the operator (or dashboard) can see WHAT failed without
    scraping logs.
    """
    from fastapi.responses import JSONResponse

    now = time.time()
    cfg = APP_STATE.get("config") or {}
    sched = cfg.get("schedule", {}) if isinstance(cfg, dict) else {}
    run_interval_h = float(sched.get("run_interval_hours", 1))
    fast_interval_min = float(sched.get("fast_monitor_interval_minutes", 2))

    last_analysis = float(APP_STATE.get("last_analysis_cycle_at") or 0.0)
    last_fast = float(APP_STATE.get("last_fast_monitor_at") or 0.0)

    # Tolerate one missed cycle: 2x the interval. If run_interval=1h,
    # the bot is allowed to be up to 2h late before we say "stuck".
    # Below that, transient slowness (a 15yfinance cycle that
    # hits retries, a manual pause) shouldn't trigger a restart.
    analysis_threshold_s = run_interval_h * 3600.0 * 2.0
    fast_threshold_s = fast_interval_min * 60.0 * 2.0

    analysis_ok = (last_analysis <= 0) or ((now - last_analysis) <= analysis_threshold_s)
    fast_ok = (last_fast <= 0) or ((now - last_fast) <= fast_threshold_s)

    audit_path = _audit_path()
    audit_writable = False
    audit_writable_error = None
    try:
        # Append a sentinel event, then immediately remove the line
        # we just wrote. The atomic_write_text helper isn't needed
        # here — this is a 1-shot test that the OS allows us to
        # open(append), and on any failure (full disk, perms,
        # missing dir) we report the error verbatim.
        os.makedirs(os.path.dirname(audit_path) or ".", exist_ok=True)
        sentinel = json.dumps({
            "ts": now,
            "iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "event_type": "HEALTH_CHECK_SENTINEL",
            "payload": {},
        }, ensure_ascii=False) + "\n"
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(sentinel)
        # Remove the last line. Use a simple "truncate to before last \n"
        # so we don't need to read + parse the full ledger.
        with open(audit_path, "rb+") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            if end > 0:
                # Walk backwards to the previous newline
                pos = end - 1
                while pos > 0:
                    f.seek(pos)
                    if f.read(1) == b"\n":
                        break
                    pos -= 1
                f.seek(pos + 1 if pos > 0 else 0)
                f.truncate()
        audit_writable = True
    except Exception as e:
        audit_writable_error = f"{type(e).__name__}: {e}"

    overall_ok = analysis_ok and fast_ok and audit_writable
    # Sprint 46R audit M11.4: surface the dead-man's switch state too.
    # We deliberately don't fail the healthcheck on a stale OOB ping —
    # healthchecks.io being unreachable is an OOB problem, not a
    # bot problem. We just include the info for the operator.
    dms_state: Dict[str, Any] = {}
    try:
        from src.observability.dead_mans_switch import get_ping_state
        dms_state = get_ping_state()
    except Exception:
        dms_state = {"error": "dead_mans_switch module not importable"}

    body = {
        "ok": overall_ok,
        "ts": now,
        "started_at": APP_STATE.get("started_at"),
        "audit_path": audit_path,
        "positions_path": _positions_path(),
        "config_path": _config_path(),
        "ws_clients": len(APP_STATE.get("ws_clients", set())),
        "checks": {
            "analysis_cycle": {
                "ok": analysis_ok,
                "last_at": last_analysis or None,
                "age_s": (now - last_analysis) if last_analysis else None,
                "threshold_s": analysis_threshold_s,
                "interval_h": run_interval_h,
            },
            "fast_monitor": {
                "ok": fast_ok,
                "last_at": last_fast or None,
                "age_s": (now - last_fast) if last_fast else None,
                "threshold_s": fast_threshold_s,
                "interval_min": fast_interval_min,
            },
            "audit_writable": {
                "ok": audit_writable,
                "error": audit_writable_error,
            },
            "dead_mans_switch": dms_state,
        },
    }
    return JSONResponse(
        status_code=200 if overall_ok else 503,
        content=body,
    )

# ----------------------------------------------------------------------
# Mode
# ----------------------------------------------------------------------

@app.get("/api/mode", response_model=ModeInfo)
def get_mode(_: None = Depends(auth.require_auth)) -> ModeInfo:
    return read_mode(
        config=APP_STATE.get("config") or {},
        audit_path=_audit_path(),
    )

@app.post("/api/mode", response_model=SetModeResponse)
def set_mode(req: SetModeRequest, _: None = Depends(auth.require_auth)) -> SetModeResponse:
    if req.mode not in ("live", "paper"):
        raise HTTPException(status_code=400, detail=f"mode must be 'live' or 'paper', got {req.mode!r}")
    mandate_enabled = (req.mode == "live")
    new_mode = write_mode(
        mandate_enabled=mandate_enabled,
        switched_by=req.switched_by or "api",
        audit_path=_audit_path(),
    )
    return SetModeResponse(mode=new_mode)

# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------

@app.post("/api/auth/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request) -> LoginResponse:
    """Password -> token.

    Sprint 46N (audit A9): this endpoint used to have NO throttling at
    all — unlimited password guesses, zero backoff. Now gated by
    `auth.login_rate_limiter`, keyed by client IP: after
    `DASHBOARD_LOGIN_MAX_ATTEMPTS` (default 5) failures within
    `DASHBOARD_LOGIN_WINDOW_SECONDS` (default 15 min), that IP is
    locked out for `DASHBOARD_LOGIN_LOCKOUT_SECONDS` (default 15 min)
    and gets 429 instead of even attempting the password check. A
    successful login clears that IP's history.
    """
    client_key = request.client.host if request.client else "unknown"
    allowed, retry_after = auth.login_rate_limiter.check(client_key)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"too many failed login attempts; try again in {int(retry_after) + 1}s",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    try:
        token = auth.issue_token(password=req.password)
    except PermissionError as e:
        auth.login_rate_limiter.record_failure(client_key)
        # Same response for any auth failure — don't leak whether
        # the password env var is set or not.
        raise HTTPException(status_code=401, detail=f"auth failed: {e}")
    auth.login_rate_limiter.record_success(client_key)
    return LoginResponse(
        token=token,
        expires_in_s=auth.TOKEN_TTL_SECONDS,
    )

# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------

@app.get("/api/state", response_model=StateSnapshot)
def get_state(_: None = Depends(auth.require_auth)) -> StateSnapshot:
    return build_state_snapshot(
        config=APP_STATE.get("config") or {},
        audit_path=_audit_path(),
        positions_path=_positions_path(),
    )

def _trading_config_response(effective: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
    """Shape `state.read_trading_config()`'s output for the API: strip
    the internal `_override_*` bookkeeping keys and compute
    `pending_restart` — True when a dashboard save happened AFTER this
    process started, meaning the RUNNING bot hasn't picked it up yet
    (main.py only reads trading config once at startup; see
    state.py's Sprint 46D docstring for why).
    """
    updated_at = effective.get("_override_updated_at")
    started_at = APP_STATE.get("started_at")
    pending_restart = bool(updated_at and started_at and updated_at > started_at)
    out = {k: v for k, v in effective.items() if not k.startswith("_")}
    out["pending_restart"] = pending_restart
    out["updated_at"] = updated_at
    out["updated_by"] = effective.get("_override_updated_by")
    if note:
        out["note"] = note
    return out

@app.get("/api/config")
def get_trading_config(_: None = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Sprint 46C/D: the *effective* trading config — config.yaml's
    `trading:` section with any dashboard-saved override layered on
    top (see state.read_trading_config). Carlos asked for a way to
    see, at a glance, how many trades can be open simultaneously, how
    much risk is taken per trade, and the minimum order size; Sprint
    46D made these editable too (POST below), so this now also
    reports whether a saved change is still waiting for a bot restart
    to take effect (`pending_restart`).
    """
    effective = read_trading_config(config=APP_STATE.get("config") or {}, audit_path=_audit_path())
    return _trading_config_response(effective)

@app.post("/api/config")
def post_trading_config(
    req: UpdateTradingConfigRequest,
    _: None = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Sprint 46D: save a partial trading-config change from the
    dashboard. Only fields the client actually set are applied
    (everything else keeps its current effective value) — writes to
    `audit/trading_config_override.json`, NEVER to config.yaml itself
    (see state.py's module docstring for why: PyYAML isn't
    comment-safe for round-trip edits).

    IMPORTANT: this does NOT take effect immediately. `main.py` reads
    trading config once at startup and hands the values to
    RiskManagerAgent's constructor — it isn't re-read per cycle. The
    response's `note` says so explicitly, and `pending_restart` will
    be true until POST /api/restart (or a manual restart) picks up
    the new values.
    """
    updates = req.model_dump(exclude_unset=True, exclude={"updated_by"})
    if not updates:
        raise HTTPException(status_code=400, detail="no fields provided to update")
    effective = write_trading_config(
        updates=updates,
        updated_by=req.updated_by or "dashboard",
        config=APP_STATE.get("config") or {},
        audit_path=_audit_path(),
    )
    return _trading_config_response(
        effective,
        note=(
            "Saved. These changes take effect after the bot restarts "
            "(main.py reads trading config once at startup). Use "
            "POST /api/restart to apply now (~10-30s downtime), or "
            "they'll apply automatically on the next deploy/restart."
        ),
    )

def _risk_config_response(effective: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
    """Same shaping as `_trading_config_response`, for risk config."""
    updated_at = effective.get("_override_updated_at")
    started_at = APP_STATE.get("started_at")
    pending_restart = bool(updated_at and started_at and updated_at > started_at)
    out = {k: v for k, v in effective.items() if not k.startswith("_")}
    out["pending_restart"] = pending_restart
    out["updated_at"] = updated_at
    out["updated_by"] = effective.get("_override_updated_by")
    if note:
        out["note"] = note
    return out

@app.get("/api/risk-config")
def get_risk_config(_: None = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Sprint 46F: effective risk/mandate safety config — drawdown
    kill-switch threshold/cooldown, mandate's allowed-symbols list,
    and the portfolio-risk gate caps (concentration/correlation/
    CVaR/stress). See state.read_risk_config for the merge logic.
    """
    effective = read_risk_config(config=APP_STATE.get("config") or {}, audit_path=_audit_path())
    return _risk_config_response(effective)

@app.post("/api/risk-config")
def post_risk_config(
    req: UpdateRiskConfigRequest,
    _: None = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Sprint 46F: save a partial risk/mandate config change from the
    dashboard. Same restart-required caveat as POST /api/config — see
    that endpoint's docstring.
    """
    updates = req.model_dump(exclude_unset=True, exclude={"updated_by"})
    if not updates:
        raise HTTPException(status_code=400, detail="no fields provided to update")
    effective = write_risk_config(
        updates=updates,
        updated_by=req.updated_by or "dashboard",
        config=APP_STATE.get("config") or {},
        audit_path=_audit_path(),
    )
    return _risk_config_response(
        effective,
        note=(
            "Saved. These changes take effect after the bot restarts "
            "(main.py reads risk/mandate config once at startup). Use "
            "POST /api/restart to apply now (~10-30s downtime), or "
            "they'll apply automatically on the next deploy/restart."
        ),
    )

@app.get("/api/positions", response_model=List[PositionSummary])
def get_positions(_: None = Depends(auth.require_auth)) -> List[PositionSummary]:
    snap = build_state_snapshot(
        config=APP_STATE.get("config") or {},
        audit_path=_audit_path(),
        positions_path=_positions_path(),
    )
    return snap.positions

# ----------------------------------------------------------------------
# Sprint 58: Dashboard richer data views
# ----------------------------------------------------------------------
#
# Two new endpoints that complete the dashboard:
#  - /api/candles         historical OHLCV for any asset (no position
#                          required). Used by the /charts page.
#  - /api/positions/history closed-position ledger with date/asset/
#                          direction filters. Used by the /history page.
#
# Both reuse the existing position-repository + yfinance wrappers so
# there is exactly one place that talks to yfinance (safe_yf_download).
#
# NOTE on route order: `/api/positions/history` MUST be declared
# before `/api/positions/{position_id}` -- FastAPI matches routes
# in declaration order, and the path-param route would otherwise
# capture "history" as a position_id and return 404. See the
# comment at the {position_id} route below for the full rationale.

def _candles_impl(asset: str, interval: str, limit: int) -> Dict[str, Any]:
    """Pure logic for /api/candles — no FastAPI Query defaults, so
    it can be called directly from tests AND from the FastAPI
    endpoint. The endpoint is a thin wrapper that does the
    parameter parsing via Query().
    """
    from src.data.yf_safe import safe_yf_download
    period_map = {"1m": "5d", "5m": "30d", "15m": "60d", "1h": "60d", "1d": "2y"}
    period = period_map.get(interval, "60d")
    try:
        df = safe_yf_download(asset, period=period, interval=interval)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"price fetch failed: {e}")
    if df is None or df.empty:
        return {"asset": asset, "interval": interval, "candles": []}
    df = df.tail(limit)
    return {
        "asset": asset,
        "interval": interval,
        "candles": _df_to_candles(df),
    }


def _history_impl(
    from_ts: Optional[float],
    to_ts: Optional[float],
    asset_class: Optional[str],
    direction: Optional[str],
    asset: Optional[str],
    limit: int,
) -> Dict[str, Any]:
    """Pure logic for /api/positions/history — same split as
    `_candles_impl` above. Pydantic validation is the endpoint's
    job; the test layer just needs to drive the logic with raw
    values."""
    from src.data_store.positions import PositionRepository
    repo = PositionRepository(_positions_path())
    closed = [p for p in repo.all() if p.closed_ts is not None]
    if from_ts is not None:
        closed = [p for p in closed if (p.closed_ts or 0) >= from_ts]
    if to_ts is not None:
        closed = [p for p in closed if (p.closed_ts or 0) <= to_ts]
    if asset_class is not None:
        closed = [p for p in closed if _ASSET_CLASS_MAP.get(p.asset) == asset_class]
    if direction is not None:
        closed = [p for p in closed if p.direction == direction]
    if asset is not None:
        closed = [p for p in closed if p.asset == asset]
    closed.sort(key=lambda p: p.closed_ts or 0.0, reverse=True)
    closed = closed[:limit]
    rows = []
    win = loss = be = 0
    total_pnl = 0.0
    total_fees = 0.0
    for p in closed:
        pnl = p.realized_pnl or 0.0
        total_pnl += pnl
        total_fees += p.fees_paid_usd or 0.0
        if pnl > 0.0001:
            win += 1
        elif pnl < -0.0001:
            loss += 1
        else:
            be += 1
        dur_h = None
        if p.entry_ts and p.closed_ts:
            dur_h = round((p.closed_ts - p.entry_ts) / 3600.0, 1)
        rows.append({
            "id": p.position_id,
            "asset": p.asset,
            "asset_class": _ASSET_CLASS_MAP.get(p.asset, "other"),
            "direction": p.direction,
            "entry_price": p.entry_price,
            "entry_ts": p.entry_ts,
            "closed_price": p.closed_price,
            "closed_ts": p.closed_ts,
            "close_reason": p.close_reason or "UNKNOWN",
            "qty": p.qty,
            "notional_usd": p.notional_usd,
            "realized_pnl_usd": pnl,
            "fees_paid_usd": p.fees_paid_usd or 0.0,
            "duration_hours": dur_h,
            "strategy": p.strategy,
        })
    total = win + loss + be
    return {
        "positions": rows,
        "summary": {
            "total_trades": total,
            "win_count": win,
            "loss_count": loss,
            "breakeven_count": be,
            "win_rate_pct": round(100.0 * win / total, 1) if total else 0.0,
            "total_pnl_usd": round(total_pnl, 6),
            "total_fees_usd": round(total_fees, 6),
        },
    }


def _df_to_candles(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Convert a yfinance DataFrame (columns: Open/High/Low/Close/Volume)
    into the JSON shape the dashboard's candlestick component wants.

    Shared between the position-scoped /api/positions/{id}/candles
    and the new asset-scoped /api/candles (Sprint 58) so the wire
    format is identical. Datetimes are serialized to unix seconds.
    Volume is optional -- some yfinance intervals don't return it.
    """
    candles: List[Dict[str, Any]] = []
    for idx, row in df.iterrows():
        try:
            ts = int(idx.timestamp())
        except Exception:
            ts = 0
        candles.append({
            "ts": ts,
            "open": float(row.get("Open", 0.0)),
            "high": float(row.get("High", 0.0)),
            "low": float(row.get("Low", 0.0)),
            "close": float(row.get("Close", 0.0)),
            "volume": float(row.get("Volume", 0.0)) if "Volume" in row else 0.0,
        })
    return candles

@app.get("/api/candles")
def get_candles(
    asset: str = Query(..., min_length=1, max_length=20),
    interval: str = Query("1h", pattern="^(1m|5m|15m|1h|1d)$"),
    limit: int = Query(200, ge=10, le=1000),
    _: None = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Historical OHLCV for `asset` at `interval`.

    Sprint 58: companion to the position-scoped `/api/positions/{id}/candles`
    -- this one doesn't need a position_id, so the dashboard's /charts
    page can show BTC/ETH/SOL/SPY/QQQ/GLD/USO even when no position
    is open. Same wire format (`{ts, open, high, low, close, volume}`)
    so the chart component is identical.

    `limit` is capped at 1000 to keep responses reasonable. yfinance's
    1m interval is rate-limited to 7 days of history; longer
    timeframes return up to `period=` days. We pass period based on
    interval+limit so yfinance has enough history.

    Pure logic lives in `_candles_impl` so the test layer can drive
    it without going through FastAPI's Query defaults (which
    explode when the function is called directly with positional
    args -- the default `Query(...)` value isn't a string, it's a
    Query marker object).
    """
    return _candles_impl(asset, interval, limit)

# Mapping used by the history endpoint to bucket each asset into
# crypto / equity. Mirrors src/data/asset_class.py's get_asset_class
# but is duplicated here so the API surface doesn't depend on the
# data-layer module (which the dashboard doesn't need to know about).
_ASSET_CLASS_MAP = {
    "BTC-USD": "crypto", "ETH-USD": "crypto", "SOL-USD": "crypto",
    "SPY": "equity", "QQQ": "equity", "GLD": "equity", "USO": "equity",
}

# MUST be declared before /api/positions/{position_id} -- see
# the Sprint 58 comment above.
@app.get("/api/positions/history")
def get_positions_history(
    # All filters are optional; missing = "no filter on this field".
    # `from_ts` / `to_ts` are unix seconds. If both are absent we
    # return the full history (capped at `limit`).
    from_ts: Optional[float] = Query(None, alias="from"),
    to_ts: Optional[float] = Query(None, alias="to"),
    asset_class: Optional[str] = Query(None, pattern="^(crypto|equity)$"),
    direction: Optional[str] = Query(None, pattern="^(long|short)$"),
    asset: Optional[str] = Query(None, max_length=20),
    limit: int = Query(500, ge=1, le=5000),
    _: None = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Closed-position history with date/asset/direction filters.

    Sprint 58: the bot already persists every position (open and
    closed) to `data_store/positions.json` via PositionRepository
    (Sprint 46I introduced the close-tracker; the close_reason
    field was added in Sprint 46J). This endpoint reads the
    repository, filters closed positions, and returns them sorted
    by closed_ts desc so the dashboard's /history page can show
    them newest-first with date/direction/asset filters.

    Filter semantics:
      * from_ts / to_ts: filter on `closed_ts`. Missing = unbounded.
      * asset_class: crypto (BTC/ETH/SOL) or equity (SPY/QQQ/GLD/USO).
      * direction: long or short.
      * asset: exact match (e.g. "BTC-USD").

    Response:
      {
        "positions": [ClosedPositionRow, ...],
        "summary": {total_trades, win_count, loss_count, breakeven_count,
                    total_pnl_usd, total_fees_usd, win_rate_pct}
      }

    Pure logic lives in `_history_impl` so the test layer can drive
    it with raw values (FastAPI's `Query` defaults explode when the
    function is called directly with positional args).
    """
    return _history_impl(from_ts, to_ts, asset_class, direction, asset, limit)

@app.get("/api/positions/{position_id}")
def get_position(position_id: str, _: None = Depends(auth.require_auth)) -> Dict[str, Any]:
    snap = build_state_snapshot(
        config=APP_STATE.get("config") or {},
        audit_path=_audit_path(),
        positions_path=_positions_path(),
    )
    for p in snap.positions:
        if p.id == position_id:
            return p.model_dump()
    raise HTTPException(status_code=404, detail=f"position {position_id!r} not found or closed")

# NOTE: `/api/positions/history` (Sprint 58) is intentionally
# declared EARLIER (search for "Sprint 58: Dashboard richer data
# views" above) -- FastAPI matches routes in declaration order,
# and the path-param route `{position_id}` would otherwise capture
# `history` as a position_id (404 "position 'history' not found").
# The new endpoint has to win the match.

@app.get("/api/positions/{position_id}/candles")
def get_position_candles(
    position_id: str,
    interval: str = Query("15m", pattern="^(1m|5m|15m|1h|1d)$"),
    window: int = Query(200, ge=10, le=1000),
    _: None = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Historical candles for the position's asset, used by the chart.

    Returns OHLCV from yfinance (via safe_yf_download). The dashboard
    overlays entry/SL/TP lines on top.
    """
    from src.data_store.positions import PositionRepository
    from src.data.yf_safe import safe_yf_download
    repo = get_position_repo(_positions_path())
    asset = None
    entry = None
    sl = None
    tp = None
    for p in repo.open():
        if p.position_id == position_id:
            asset = p.asset
            entry = p.entry_price
            sl = p.stop_loss
            tp = p.take_profit
            break
    if asset is None:
        raise HTTPException(status_code=404, detail=f"position {position_id!r} not found")
    try:
        df = safe_yf_download(asset, period=f"{max(60, window)}d", interval=interval)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"price fetch failed: {e}")
    if df is None or df.empty:
        return {"asset": asset, "candles": [], "entry": entry, "stop_loss": sl, "take_profit": tp}
    df = df.tail(window)
    candles = _df_to_candles(df)
    return {
        "asset": asset,
        "interval": interval,
        "candles": candles,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
    }

@app.post("/api/positions/{position_id}/close")
def post_close_position(
    position_id: str,
    _: None = Depends(auth.require_auth),
) -> Dict[str, Any]:
    closed = close_position(
        position_id=position_id,
        audit_path=_audit_path(),
        positions_path=_positions_path(),
        # Sprint 46N (audit M2): same real binance.us round-trip fee
        # PositionMonitor/RiskManagerAgent closes already account for.
        fee_pct_for_asset=_fee_pct_for_asset,
    )
    if closed is None:
        raise HTTPException(status_code=404, detail=f"position {position_id!r} not found or already closed")
    return closed

@app.post("/api/positions/close-all")
def post_close_all_positions(_: None = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Sprint 46H: flatten every open position in one click. Carlos's
    ask: a way to make sure NO positions are left open while in paper
    mode, so the transition to live can't be corrupted by a "ghost"
    position the bot thinks it has. See state.close_all_positions'
    docstring for the repo-only-close trade-off (same as the single
    close endpoint above, just applied to all open positions).
    """
    closed = close_all_positions(
        audit_path=_audit_path(), positions_path=_positions_path(),
        fee_pct_for_asset=_fee_pct_for_asset,
    )
    return {"closed_count": len(closed), "closed": closed}

# ----------------------------------------------------------------------
# Manual trading pause (Sprint 46H) — dashboard Stop/Start toggle
# ----------------------------------------------------------------------

class TradingPauseRequest(BaseModel):
    paused: bool
    updated_by: Optional[str] = "dashboard"

@app.get("/api/trading-pause")
def get_trading_pause(_: None = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Current Stop/Start state. `paused=true` means main.py's
    job_with_monitor() is skipping NEW entries every cycle — existing
    open positions keep their SL/TP protection regardless (see
    state.read_trading_pause's docstring for why this is a separate,
    softer mechanism than the filesystem KillSwitch)."""
    return read_trading_pause(audit_path=_audit_path())

@app.post("/api/trading-pause")
def post_trading_pause(
    req: TradingPauseRequest,
    _: None = Depends(auth.require_auth),
) -> Dict[str, Any]:
    return write_trading_pause(
        paused=req.paused,
        updated_by=req.updated_by or "dashboard",
        audit_path=_audit_path(),
    )

# ----------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------

@app.get("/api/audit", response_model=List[AuditEvent])
def get_audit(
    limit: int = Query(100, ge=1, le=1000),
    after: Optional[float] = Query(None, description="Only events with ts >= after"),
    event_type: Optional[str] = Query(None),
    _: None = Depends(auth.require_auth),
) -> List[AuditEvent]:
    return build_audit(
        limit=limit, after=after, event_type=event_type, audit_path=_audit_path(),
    )

@app.get("/api/signals")
def get_signals(
    limit: int = Query(20, ge=1, le=200),
    _: None = Depends(auth.require_auth),
) -> List[Dict[str, Any]]:
    """Recent HYPOTHESIS_GENERATED and TRADE_APPROVED events.

    Heuristic: the bot doesn't persist signals as a dedicated table,
    so we surface them via the audit ledger. Not perfect (depends on
    what the bot actually logs), but good enough for the dashboard.
    """
    rows = build_audit(limit=500, audit_path=_audit_path())
    sig = [r.model_dump() for r in rows if r.event_type in (
        "HYPOTHESIS_GENERATED", "TRADE_APPROVED", "TRADE_REJECTED",
    )]
    return sig[:limit]

@app.get("/api/stats")
def get_stats(_: None = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Compact summary suitable for the KPI cards at the top of the dashboard."""
    snap = build_state_snapshot(
        config=APP_STATE.get("config") or {},
        audit_path=_audit_path(),
        positions_path=_positions_path(),
    )
    return {
        "mode": snap.mode.mode,
        "open_count": snap.open_count,
        "total_exposure_usd": snap.total_exposure_usd,
        "total_unrealized_usd": snap.total_unrealized_usd,
        "total_unrealized_pct": snap.total_unrealized_pct,
        "daily_realized_pnl_usd": snap.daily_realized_pnl_usd,
        "total_realized_pnl_usd": snap.total_realized_pnl_usd,
        "ts": snap.last_update_ts,
    }
# ----------------------------------------------------------------------
# Allocation + risk metrics (Sprint 44A/44B modules surfaced over HTTP)
# ----------------------------------------------------------------------

@app.get("/api/allocation")
def get_allocation(_: None = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Current actual allocation weights vs policy targets.

    Uses Sprint 44B's AllocationPolicy from src/data/asset_allocation.py.
    """
    from src.data.asset_allocation import (
        DEFAULT_POLICY, compute_drift, current_actual_weights,
    )
    from src.data_store.positions import PositionRepository
    config = APP_STATE.get("config") or {}
    # If config has allocation_policy, use it; else DEFAULT_POLICY.
    policy_cfg = config.get("allocation_policy") or {}
    if not policy_cfg.get("enabled", False):
        # Fall back to the dataclass default
        policy = DEFAULT_POLICY
    else:
        from src.data.asset_allocation import AllocationPolicy
        try:
            policy = AllocationPolicy(
                targets=policy_cfg.get("targets") or DEFAULT_POLICY.targets,
                drift_tolerance_pct=float(policy_cfg.get("drift_tolerance_pct", 10.0)),
                enabled=True,
                # Sprint 47A (audit M15 Option B): small-account
                # bypass threshold, read from config so the
                # dashboard can tune it. 0 = always enforce the
                # drift policy (legacy behavior).
                small_account_threshold_usd=float(
                    policy_cfg.get("small_account_threshold_usd", 50.0)
                ),
            )
        except Exception:
            policy = DEFAULT_POLICY
    repo = get_position_repo(_positions_path())
    opens = repo.open()
    actual = current_actual_weights(opens)
    drift = compute_drift(actual, policy)
    return {
        "actual_weights": drift.actual_weights,
        "target_weights": drift.target_weights,
        "drifts": drift.drifts,
        "max_abs_drift_pct": drift.max_abs_drift_pct,
        "within_tolerance": drift.within_tolerance,
        "classes_over_cap": drift.classes_over_cap,
        "classes_under_floor": drift.classes_under_floor,
    }

@app.get("/api/risk/stress")
def get_risk_stress(_: None = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Apply the 3 historical crisis scenarios to the current portfolio."""
    from src.analysis.stress_test import (
        DEFAULT_SCENARIOS, stress_portfolio_all_scenarios, worst_case_drawdown,
    )
    from src.data_store.positions import PositionRepository
    repo = get_position_repo(_positions_path())
    opens = repo.open()
    if not opens:
        return {"scenarios": [], "worst_case": None, "note": "no open positions"}
    results = stress_portfolio_all_scenarios(opens)
    worst = worst_case_drawdown(results)
    return {
        "scenarios": [r.to_dict() for r in results],
        "worst_case": worst.to_dict(),
    }

@app.get("/api/risk/correlation")
def get_risk_correlation(_: None = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Asset correlation matrix of the open positions."""
    from src.analysis.asset_correlation import analyze_assets
    from src.data_store.positions import PositionRepository
    repo = get_position_repo(_positions_path())
    assets = sorted({p.asset for p in repo.open() if p.asset})
    if len(assets) < 2:
        return {"assets": assets, "matrix": [], "avg_correlation": 0.0, "well_diversified": True, "note": "need >=2 assets"}
    res = analyze_assets(assets)
    return res.to_dict()

@app.get("/api/risk/cvar")
def get_risk_cvar(_: None = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Portfolio-level CVaR (Expected Shortfall) at 95% and 99%."""
    from src.analysis.tail_risk import compute_portfolio_tail_risk
    from src.data_store.positions import PositionRepository
    repo = get_position_repo(_positions_path())
    weights: Dict[str, float] = {}
    for p in repo.open():
        weights[p.asset] = weights.get(p.asset, 0.0) + p.notional_usd
    if not weights:
        return {"note": "no open positions"}
    res = compute_portfolio_tail_risk(weights)
    return res.to_dict()

# ----------------------------------------------------------------------
# Equity curve
# ----------------------------------------------------------------------

@app.get("/api/equity")
def get_equity(
    window_days: int = Query(30, ge=1, le=365),
    _: None = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Downsampled equity curve from the audit ledger + positions.

    For each day in the last `window_days`, compute total equity =
    balance + sum(open positions at entry) + sum(realized pnl so far).
    Cheap heuristic — good enough for a chart, not for tax purposes.
    """
    from src.safety.audit_ledger import AuditLedger
    from src.data_store.positions import PositionRepository
    audit = AuditLedger(path=_audit_path())
    repo = get_position_repo(_positions_path())
    now = time.time()
    cutoff = now - window_days * 24 * 3600
    rows = audit.read_since(cutoff)
    daily: Dict[str, float] = {}
    for r in rows:
        d = str(r.get("iso", ""))[:10]
        if not d:
            continue
        et = r.get("event_type", "")
        pnl = r.get("realized_pnl_usd")
        if et in ("TRADE_CLOSED", "POSITION_REPLACED") and pnl is not None:
            try:
                daily[d] = daily.get(d, 0.0) + float(pnl)
            except (TypeError, ValueError):
                pass
    series = [{"date": d, "realized_pnl_usd": round(v, 4)} for d, v in sorted(daily.items())]
    cumulative = []
    running = 0.0
    for s in series:
        running += s["realized_pnl_usd"]
        cumulative.append({**s, "cumulative_usd": round(running, 4)})
    # Sprint 46K fix: with only realized-PnL events in `daily`, a NEW
    # account (or one with just a single closed trade this window)
    # produces a series with 0 or 1 points — recharts can't draw a
    # line/area through a single point, so the dashboard showed one
    # floating dot instead of a curve. Prepend an explicit $0 baseline
    # at the window's start date so there's always a real "start of
    # window" reference to draw a line from, whenever we have at least
    # one actual event. (If there are zero events, the frontend already
    # shows its own "no closed trades" empty state — leave that alone.)
    if cumulative:
        window_start_date = time.strftime("%Y-%m-%d", time.gmtime(cutoff))
        if cumulative[0]["date"] != window_start_date:
            cumulative.insert(0, {
                "date": window_start_date,
                "realized_pnl_usd": 0.0,
                "cumulative_usd": 0.0,
            })
    return {"window_days": window_days, "series": cumulative}

# ----------------------------------------------------------------------
# Restart (graceful)
# ----------------------------------------------------------------------

# Sprint 46N (audit A10): a single leaked/shared bearer token (12h TTL,
# lives in the dashboard's localStorage) could otherwise trigger
# POST /api/restart in a loop — a cheap DoS that also nukes the
# drawdown kill switch's in-memory cooldown state on every bounce (see
# A1). This module-level timestamp + env-configurable cooldown adds a
# floor between accepted restarts. Deliberately in-memory (not
# persisted): the API process itself restarts alongside the bot in
# this deployment (same container, see main.py's `_start_api_server`),
# so persisting across restarts would require surviving the very
# restart it's meant to throttle — a disk-based cooldown belongs in
# main.py's own startup path if that's ever needed. This still closes
# the actual loop-DoS: within one running process, restarts are capped.
_last_restart_ts: Optional[float] = None
_RESTART_COOLDOWN_SECONDS = float(os.getenv("DASHBOARD_RESTART_COOLDOWN_SECONDS", "60"))

@app.post("/api/restart")
def post_restart(_: None = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Best-effort graceful restart of the bot process.

    We send SIGTERM to the bot's PID (read from a PID file the bot
    writes at startup, conventionally /tmp/guaritradbot.pid). The
    Docker restart policy (restart: unless-stopped) brings the
    container back up with the new config / mode.

    Returns 202 Accepted with a note that the actual restart is
    asynchronous (the bot will be down for ~10-30s).
    """
    global _last_restart_ts
    now = time.time()
    if _last_restart_ts is not None:
        elapsed = now - _last_restart_ts
        if elapsed < _RESTART_COOLDOWN_SECONDS:
            retry_after = _RESTART_COOLDOWN_SECONDS - elapsed
            print(
                f"[server] ⚠️  POST /api/restart blocked by cooldown — a restart "
                f"was requested {elapsed:.0f}s ago (cooldown={_RESTART_COOLDOWN_SECONDS:.0f}s). "
                f"If this wasn't you, the shared dashboard token may be compromised."
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    f"restart cooldown active; try again in {int(retry_after) + 1}s "
                    f"(prevents a leaked/shared token from looping /api/restart, audit A10)"
                ),
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
    pid_path = os.getenv("DASHBOARD_BOT_PID_FILE", "/tmp/guaritradbot.pid")
    if not os.path.exists(pid_path):
        raise HTTPException(
            status_code=404,
            detail=f"bot pid file not found at {pid_path}; the bot may not be running, or this API may be on a different host",
        )
    try:
        pid = int(Path(pid_path).read_text(encoding="utf-8").strip())
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"failed to read pid file: {e}")
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        raise HTTPException(status_code=404, detail=f"pid {pid} not found; bot already stopped?")
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"no permission to signal pid {pid}")
    _last_restart_ts = now
    print(f"[server] 🔁 POST /api/restart accepted — sent SIGTERM to pid {pid}.")
    return {
        "ok": True,
        "pid": pid,
        "signal": "SIGTERM",
        "note": "Bot will restart via the container's `restart: unless-stopped` policy. Expect 10-30s of downtime.",
    }

# ----------------------------------------------------------------------
# WebSocket: live updates
# ----------------------------------------------------------------------

@app.websocket("/ws/live")
async def ws_live(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None),
):
    """Live update stream.

    Auth: `?token=<bearer>` query param. The dashboard's WebSocket
    client (browser EventSource or hand-rolled WebSocket) passes the
    token obtained from POST /api/auth/login.

    Message format (server -> client):
      {
        "type": "positions",
        "positions": [PositionSummary, ...],
        "total_unrealized_usd": float,
        "total_unrealized_pct": float,
        "open_count": int,
        "ts": float,
      }
      OR
      {
        "type": "audit",
        "event": AuditEvent,
      }
      OR
      {
        "type": "hello",
        "started_at": float,
        "ts": float,
      }
    """
    ok, reason = auth.verify_token(token or "")
    if not ok:
        await websocket.close(code=4401, reason=reason)
        return
    await websocket.accept()
    APP_STATE.setdefault("ws_clients", set()).add(websocket)
    try:
        await websocket.send_text(json.dumps({
            "type": "hello",
            "started_at": APP_STATE.get("started_at"),
            "ts": time.time(),
        }))
        # Keep the connection open. The client can also send pings
        # (we don't need to read them — uvicorn handles protocol-level
        # pings — but we should drain the receive buffer so the
        # WebSocket doesn't fill up).
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                # Echo back pings as pongs for liveness checks.
                if msg.strip() == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # No message in 30s — send a heartbeat.
                try:
                    await websocket.send_text(json.dumps({"type": "heartbeat", "ts": time.time()}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        APP_STATE["ws_clients"].discard(websocket)

# ----------------------------------------------------------------------
# Sprint 57: Server-Sent Events live stream
# ----------------------------------------------------------------------
#
# Traefik (the reverse proxy fronting this bot in Coolify on this
# VPS) rejects every HTTP/1.1 upgrade request with 403 Forbidden,
# which kills the WebSocket above. SSE is plain HTTP/1.1 chunked
# transfer -- no upgrade, no special headers, works with any
# proxy. The dashboard's use-live.ts now uses the browser's
# EventSource against /api/events; the WebSocket at /ws/live stays
# for back-compat (and for non-Traefik deployments).
#
# Wire format: standard SSE -- each event is a block of
#   `data: <json>\n\n`
# emitted whenever `_broadcast` publishes (audit events,
# position snapshots, hello on connect, heartbeats every 30s
# for proxies that close idle connections). The browser's
# EventSource re-connects automatically on disconnect; the
# `Last-Event-ID` header on reconnect lets us resume (not yet
# implemented -- the audit-tail loop is the source of truth).

@app.get("/api/events")
async def sse_events(
    token: Optional[str] = Query(default=None),
):
    """SSE live update stream. Auth via `?token=<bearer>` query param,
    same as the WebSocket handler.

    The response is a `text/event-stream` (the SSE MIME type) that
    stays open as long as the client is connected. Events come from
    the same `_broadcast` fan-out that feeds the WebSocket clients
    -- SSE and WebSocket clients see the same messages, in order.
    """
    import json as _json  # noqa: F401 -- already imported at module top
    from starlette.responses import StreamingResponse as _SR  # noqa: F401

    ok, reason = auth.verify_token(token or "")
    if not ok:
        raise HTTPException(status_code=401, detail=reason)

    queue: asyncio.Queue = asyncio.Queue(maxsize=16)
    APP_STATE.setdefault("sse_clients", set()).add(queue)
    # Send a `hello` event immediately on connect so the dashboard
    # knows it's live. (Same shape as the WebSocket `hello` event.)
    try:
        queue.put_nowait(_json.dumps({
            "type": "hello",
            "started_at": APP_STATE.get("started_at"),
            "ts": time.time(),
        }, default=str))
    except asyncio.QueueFull:
        pass  # extremely unlikely with maxsize=16; if it happens
              # the very first event will be a heartbeat instead

    async def event_stream():
        # 30s keep-alive: SSE doesn't have a standard keep-alive
        # but the spec allows `:` comment lines which most proxies
        # (including Traefik) treat as heartbeats. Without this,
        # an idle proxy would close the connection after ~60s.
        last_heartbeat = time.time()
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    # Send a heartbeat so the proxy keeps the
                    # connection alive. EventSource ignores comment
                    # lines, so this is invisible to the client.
                    yield ": keepalive\n\n"
                    last_heartbeat = time.time()
        except asyncio.CancelledError:
            # Client disconnected (browser closed the EventSource).
            pass
        finally:
            try:
                APP_STATE["sse_clients"].discard(queue)
            except Exception:
                pass

    return _SR(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # SSE-friendly headers. `X-Accel-Buffering: no` tells
            # nginx-style proxies to flush immediately rather than
            # buffer the response. Traefik doesn't strictly need
            # this, but it's a no-cost belt-and-suspenders.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
