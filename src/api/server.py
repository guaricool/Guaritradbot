"""
Sprint 46A — FastAPI HTTP + WebSocket server for the bot dashboard.

This is the BACKEND half of the dashboard refactor (replacing the
Streamlit monolith). The bot state lives on disk in
`data_store/positions.json` and `audit/audit.jsonl`; the API layer
reads it via `state.py` and exposes it as REST + WebSocket.

Endpoints
---------
Public (no auth):
  GET  /api/health
  GET  /api/state
  GET  /api/positions
  GET  /api/positions/{id}
  GET  /api/positions/{id}/candles
  GET  /api/audit
  GET  /api/signals              (heuristic scan of audit; not perfect)
  GET  /api/stats                (alias of /api/state; for Streamlit compat)
  GET  /api/mode
  GET  /api/allocation           (from src/data/asset_allocation.py)
  GET  /api/risk/stress
  GET  /api/risk/correlation
  GET  /api/risk/cvar
  GET  /api/equity               (equity curve, downsampled)

Authenticated (Bearer token in Authorization header):
  POST /api/auth/login           (password -> token)
  POST /api/mode                 (set LIVE/PAPER)
  POST /api/positions/{id}/close (manual close)
  POST /api/restart              (graceful bot restart)

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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api import auth
from src.api.state import (
    AuditEvent,
    ModeInfo,
    PositionSummary,
    StateSnapshot,
    build_audit,
    build_state_snapshot,
    close_position,
    invalidate_price_cache,
    read_current_prices,
    read_mode,
    write_mode,
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


# ----------------------------------------------------------------------
# Lifespan: load config once, share with requests
# ----------------------------------------------------------------------

APP_STATE: Dict[str, Any] = {}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    APP_STATE["config"] = _load_config()
    APP_STATE["started_at"] = time.time()
    APP_STATE["ws_clients"]: Set[WebSocket] = set()
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

# CORS: by default allow all in dev. In prod, narrow to dashboard's
# public origin via DASHBOARD_CORS_ORIGINS env var (comma-separated).
_CORS_ORIGINS = os.getenv("DASHBOARD_CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
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


# ----------------------------------------------------------------------
# WebSocket hub
# ----------------------------------------------------------------------

async def _broadcast(event: Dict[str, Any]) -> None:
    """Send `event` to all connected WebSocket clients.

    Drops slow clients silently (their buffer fills, send() raises,
    we remove them). This is intentional — better to lose a stale
    client than to block the broadcaster on one slow reader.
    """
    dead: List[WebSocket] = []
    payload = json.dumps(event, default=str)
    for ws in list(APP_STATE.get("ws_clients", set())):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        APP_STATE["ws_clients"].discard(ws)


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
    return {
        "ok": True,
        "ts": time.time(),
        "started_at": APP_STATE.get("started_at"),
        "audit_path": _audit_path(),
        "positions_path": _positions_path(),
        "config_path": _config_path(),
        "ws_clients": len(APP_STATE.get("ws_clients", set())),
    }


# ----------------------------------------------------------------------
# Mode
# ----------------------------------------------------------------------

@app.get("/api/mode", response_model=ModeInfo)
def get_mode() -> ModeInfo:
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
def login(req: LoginRequest) -> LoginResponse:
    try:
        token = auth.issue_token(password=req.password)
    except PermissionError as e:
        # Same response for any auth failure — don't leak whether
        # the password env var is set or not.
        raise HTTPException(status_code=401, detail=f"auth failed: {e}")
    return LoginResponse(
        token=token,
        expires_in_s=auth.TOKEN_TTL_SECONDS,
    )


# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------

@app.get("/api/state", response_model=StateSnapshot)
def get_state() -> StateSnapshot:
    return build_state_snapshot(
        config=APP_STATE.get("config") or {},
        audit_path=_audit_path(),
        positions_path=_positions_path(),
    )


@app.get("/api/config")
def get_trading_config() -> Dict[str, Any]:
    """Sprint 46C: expose the `trading:` section of config.yaml read-only.

    Carlos asked for a way to see, at a glance, how many trades can be
    open simultaneously, how much risk is taken per trade, and the
    minimum order size — these all live in config.yaml's `trading:`
    block (consumed by main.py/RiskManagerAgent at startup) but had no
    way to be viewed from the dashboard. This is intentionally
    READ-ONLY for now (mirrors /api/allocation, /api/risk/* — display
    only, no POST counterpart) — editing config.yaml still requires a
    file change + bot restart, this just answers "what is it set to
    right now, in the config the running bot actually loaded".
    """
    config = APP_STATE.get("config") or {}
    trading_cfg = config.get("trading", {}) or {}
    return {
        "risk_per_trade_pct": trading_cfg.get("risk_per_trade_pct", 1.0),
        "max_open_trades": trading_cfg.get("max_open_trades", 5),
        "min_order_usd": trading_cfg.get("min_order_usd", 10.0),
        "max_capital_per_trade_pct": trading_cfg.get("max_capital_per_trade_pct", 10.0),
        "atr_stop_multiplier": trading_cfg.get("atr_stop_multiplier", 2.0),
        "atr_take_profit_multiplier": trading_cfg.get("atr_take_profit_multiplier", 4.0),
        "risk_reward_ratio": trading_cfg.get("risk_reward_ratio", 2.0),
        "enable_position_replacement": trading_cfg.get("enable_position_replacement", True),
        "replacement_score_threshold": trading_cfg.get("replacement_score_threshold", 0.20),
        "min_profit_to_protect": trading_cfg.get("min_profit_to_protect", 0.0),
    }


@app.get("/api/positions", response_model=List[PositionSummary])
def get_positions() -> List[PositionSummary]:
    snap = build_state_snapshot(
        config=APP_STATE.get("config") or {},
        audit_path=_audit_path(),
        positions_path=_positions_path(),
    )
    return snap.positions


@app.get("/api/positions/{position_id}")
def get_position(position_id: str) -> Dict[str, Any]:
    snap = build_state_snapshot(
        config=APP_STATE.get("config") or {},
        audit_path=_audit_path(),
        positions_path=_positions_path(),
    )
    for p in snap.positions:
        if p.id == position_id:
            return p.model_dump()
    raise HTTPException(status_code=404, detail=f"position {position_id!r} not found or closed")


@app.get("/api/positions/{position_id}/candles")
def get_position_candles(
    position_id: str,
    interval: str = Query("15m", pattern="^(1m|5m|15m|1h|1d)$"),
    window: int = Query(200, ge=10, le=1000),
) -> Dict[str, Any]:
    """Historical candles for the position's asset, used by the chart.

    Returns OHLCV from yfinance (via safe_yf_download). The dashboard
    overlays entry/SL/TP lines on top.
    """
    from src.data_store.positions import PositionRepository
    from src.data.yf_safe import safe_yf_download
    repo = PositionRepository(path=_positions_path())
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
    candles = []
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
    )
    if closed is None:
        raise HTTPException(status_code=404, detail=f"position {position_id!r} not found or already closed")
    return closed


# ----------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------

@app.get("/api/audit", response_model=List[AuditEvent])
def get_audit(
    limit: int = Query(100, ge=1, le=1000),
    after: Optional[float] = Query(None, description="Only events with ts >= after"),
    event_type: Optional[str] = Query(None),
) -> List[AuditEvent]:
    return build_audit(
        limit=limit, after=after, event_type=event_type, audit_path=_audit_path(),
    )


@app.get("/api/signals")
def get_signals(limit: int = Query(20, ge=1, le=200)) -> List[Dict[str, Any]]:
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
def get_stats() -> Dict[str, Any]:
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
def get_allocation() -> Dict[str, Any]:
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
            )
        except Exception:
            policy = DEFAULT_POLICY
    repo = PositionRepository(path=_positions_path())
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
def get_risk_stress() -> Dict[str, Any]:
    """Apply the 3 historical crisis scenarios to the current portfolio."""
    from src.analysis.stress_test import (
        DEFAULT_SCENARIOS, stress_portfolio_all_scenarios, worst_case_drawdown,
    )
    from src.data_store.positions import PositionRepository
    repo = PositionRepository(path=_positions_path())
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
def get_risk_correlation() -> Dict[str, Any]:
    """Asset correlation matrix of the open positions."""
    from src.analysis.asset_correlation import analyze_assets
    from src.data_store.positions import PositionRepository
    repo = PositionRepository(path=_positions_path())
    assets = sorted({p.asset for p in repo.open() if p.asset})
    if len(assets) < 2:
        return {"assets": assets, "matrix": [], "avg_correlation": 0.0, "well_diversified": True, "note": "need >=2 assets"}
    res = analyze_assets(assets)
    return res.to_dict()


@app.get("/api/risk/cvar")
def get_risk_cvar() -> Dict[str, Any]:
    """Portfolio-level CVaR (Expected Shortfall) at 95% and 99%."""
    from src.analysis.tail_risk import compute_portfolio_tail_risk
    from src.data_store.positions import PositionRepository
    repo = PositionRepository(path=_positions_path())
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
def get_equity(window_days: int = Query(30, ge=1, le=365)) -> Dict[str, Any]:
    """Downsampled equity curve from the audit ledger + positions.

    For each day in the last `window_days`, compute total equity =
    balance + sum(open positions at entry) + sum(realized pnl so far).
    Cheap heuristic — good enough for a chart, not for tax purposes.
    """
    from src.safety.audit_ledger import AuditLedger
    from src.data_store.positions import PositionRepository
    audit = AuditLedger(path=_audit_path())
    repo = PositionRepository(path=_positions_path())
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
    return {"window_days": window_days, "series": cumulative}


# ----------------------------------------------------------------------
# Restart (graceful)
# ----------------------------------------------------------------------

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
