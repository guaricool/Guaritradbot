"""
Sprint 0+1+2 — main entrypoint.

Sprint 0 fix: lee `trading.*` y propaga los parámetros correctos a
RiskManager (risk_per_trade_pct, atr_stop_multiplier, min_order_usd).

Sprint 1 añade: audit ledger JSONL persistido en `audit/audit.jsonl`,
mandate gate opcional activado desde config.yaml, kill switch
filesystem. Cada evento relevante del bot queda registrado.

Sprint 2 añade: PositionRepository persistido en disco (sobrevive
crashes), PositionMonitor que chequea stops/TPs cada tick ANTES de
generar nuevas señales, take profit ATR-based, max_open_trades
respetado por RiskAgent.
"""
import os
import sys
import time
import json
import argparse
import threading

import schedule
import yaml

from src.workflows.engine import WorkflowEngine
from src.agents.market_analyst import MarketAnalystAgent
from src.agents.strategy_agent import StrategyAgent
from src.agents.risk_agent import RiskManagerAgent
from src.agents.execution_agent import ExecutionAgent
from src.agents.notification_agent import NotificationAgent
from src.core.event_bus import EventBus
from src.execution.execution_node import ExecutionNode
from src.execution.broker import BrokerClient
from src.execution.alpaca_broker import AlpacaBroker
from src.execution.scheduler import EpochScheduler
from src.optimization.hyperopt import HyperoptManager
from src.safety.audit_ledger import AuditLedger
from src.safety.kill_switch import KillSwitch
from src.safety.kelly_drawdown import DrawdownKillSwitch  # Sprint 43 H3
from src.safety.mandate_gate import MandateGate, MandateConfig
from src.data_store.positions import PositionRepository
from src.data_store.position_monitor import PositionMonitor
from src.agents.researchers import DebateAgent


def _audit_path(config: dict) -> str:
    audit_dir = config.get("mandate", {}).get("audit_log_dir", "audit")
    return os.path.join(audit_dir, "audit.jsonl")


def _build_mandate(config: dict, audit, position_repo=None, event_bus=None) -> tuple:
    cfg = config.get("mandate", {})
    if not cfg.get("enabled", False):
        return (None, None)
    mc = MandateConfig(
        enabled=True,
        allowed_symbols=set(cfg.get("allowed_symbols", [])),
        max_position_usd=float(cfg.get("max_position_usd", 20.0)),
        max_daily_loss_usd=float(cfg.get("max_daily_loss_usd", 5.0)),
        max_total_exposure_usd=float(cfg.get("max_total_exposure_usd", 100.0)),
        # Sprint 46J: rate limit on new entries per rolling 24h. 0 (the
        # config.yaml default) = unlimited, unchanged behavior.
        max_daily_trades=int(cfg.get("max_daily_trades", 0)),
    )
    return (MandateGate(mc, audit_ledger=audit, position_repo=position_repo, event_bus=event_bus), mc)


def _asset_class_for(asset: str, brokers_config: dict) -> str:
    """Look up which `brokers.<class>.symbols` list (config.yaml) an
    asset belongs to. Mirrors the mapping ExecutionNode builds for
    order routing (see src/execution/execution_node.py's
    `_asset_to_class`), but kept independent here since main.py needs
    it BEFORE any order exists — to decide which assets are even worth
    generating signals for this cycle (see
    `_get_active_asset_classes` / Sprint 46G below).

    Returns "unknown" for anything not listed — callers should treat
    unknown assets as always-active (don't filter what we can't
    classify), same conservative default used throughout this file.
    """
    for cls_name, cfg in (brokers_config or {}).items():
        if isinstance(cfg, dict) and asset in (cfg.get("symbols") or []):
            return cls_name
    return "unknown"


def _get_active_asset_classes(broker_client, alpaca_broker, min_usd: float = 10.0) -> set:
    """Sprint 46G — capital-aware asset-class routing.

    Carlos: "si tienes dinero en binance, el bot solo podrá trabajar
    con cryptos, y cuando está en alpaca es que podrá trabajar con
    stocks... el sistema debe ser tan inteligente que viendo el
    balance de dinero disponible sepa que camino tomar." Before this,
    the bot always generated signals for the FULL hardcoded asset list
    in trading_loop.yaml (SPY/QQQ/BTC-USD/GLD/USO) regardless of
    whether either broker actually had money — wasting cycles/API
    calls and letting RiskManagerAgent/ExecutionNode discover the
    "insufficient funds" problem only at order time.

    Returns the set of asset classes ("crypto"/"equity") worth
    generating NEW-entry signals for this cycle.

    Fail-OPEN by design: if a broker isn't configured, or its balance
    can't be read right now (network hiccup), that class is left
    ACTIVE — unchanged from the bot's behavior before this feature
    existed. A class is only EXCLUDED when we successfully read its
    balance and it's below `min_usd` (the same floor as
    trading.min_order_usd — no point signaling for a market you can't
    place even the smallest order in).
    """
    active = set()

    if broker_client is None:
        active.add("crypto")
    else:
        try:
            bal = broker_client.get_usdt_balance()
            if bal is None or bal >= min_usd:
                active.add("crypto")
        except Exception:
            active.add("crypto")

    if alpaca_broker is None:
        # Sprint 36 default: equity signals already fail loudly with
        # ALPACA_NOT_CONFIGURED at order time when there's no broker.
        # No point including equity in the active set here either.
        pass
    else:
        try:
            bal = alpaca_broker.get_usd_balance()
            if bal is None or bal >= min_usd:
                active.add("equity")
        except Exception:
            active.add("equity")

    return active


def _is_trading_paused(audit_dir: str = "audit") -> bool:
    """Sprint 46H — dashboard 'Stop trading' toggle.

    Carlos: "en el dashboard hay manera de tener como un stop y un
    start? para que mientras esté en paper se puedan detener las
    entradas que están abiertas, y así quede la sesión completamente
    limpia... a la hora de pasarlo a live el sistema pueda correr
    limpio, sin la posibilidad de un bug que el sistema crea que
    tiene alguna posición abierta."

    Reads `<audit_dir>/trading_pause.json`, written by
    POST /api/trading-pause (see src/api/state.py::write_trading_pause
    for the full rationale, including why this is intentionally
    SEPARATE from the filesystem KillSwitch). Cheap disk read, same
    pattern as `_is_mandate_enabled` in execution_node.py — checked
    EVERY cycle (not just at startup) so a dashboard click takes
    effect on the bot's next cycle, no restart needed.

    Fail-open: a missing or corrupt pause file means NOT paused
    (normal operation) — a broken override file must not become an
    unexplained full stop.
    """
    path = os.path.join(audit_dir, "trading_pause.json")
    try:
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("paused", False))
    except Exception:
        return False


def _fetch_prices_for_open_positions(
    position_repo,
    broker_client=None,
    alpaca_broker=None,
    brokers_config: dict | None = None,
) -> dict:
    """Sprint 46I: shared helper — fetch a price for every asset with a
    currently open position. Extracted so both the fast position-
    monitor loop (every few minutes, see main()'s `fast_monitor_tick`)
    and the hourly drawdown check (job_with_monitor) can each fetch
    their own snapshot independently, instead of threading shared
    mutable state between two differently-timed scheduled jobs.
    Best-effort per-asset: a single failed fetch is skipped, not fatal
    to the whole cycle.

    Sprint 46N (audit A7): this used to fetch yfinance's DAILY-CANDLE
    CLOSE (`interval="1d"`) via `MarketAnalystAgent.fetch_one` as a
    stand-in for "the current price" — up to a full trading day stale,
    sourced from Yahoo's composite index rather than the actual
    exchange order book (Yahoo's "BTC-USD" is NOT binance.us's tape),
    and it recomputed a full technical-indicator set per asset just to
    read one close value. A stop-loss/take-profit comparison against
    that price could trigger a close that never actually happened on
    the exchange, or miss one that did.

    Now routes each asset to the SAME broker that will execute its
    close — `broker_client.get_ticker_price` (ccxt `fetch_ticker`) for
    crypto, `alpaca_broker.get_latest_trade_price` for equities — via
    `_asset_class_for`, the same asset->class lookup used elsewhere in
    this file to decide routing. yfinance/`MarketAnalystAgent` remain
    the source for HISTORICAL data and indicators elsewhere in the bot
    (unaffected by this change) — only this live SL/TP price moved off
    of it. If a broker for an asset's class isn't configured, or the
    live fetch fails, that asset is simply skipped this tick (same
    best-effort contract as before).
    """
    opens = position_repo.open()
    prices: dict = {}
    brokers_config = brokers_config or {}
    for pos in opens:
        try:
            asset_class = _asset_class_for(pos.asset, brokers_config)
            price = None
            if asset_class == "equity":
                if alpaca_broker is not None:
                    price = alpaca_broker.get_latest_trade_price(pos.asset)
            else:
                # crypto or unknown -> same fallback convention as
                # resolve_broker_for_close (src/execution/broker_routing.py):
                # route unmapped assets to the crypto broker rather than
                # skipping them outright, for backward compatibility.
                if broker_client is not None:
                    ccxt_symbol = pos.asset
                    if "-" in ccxt_symbol:
                        ccxt_symbol = ccxt_symbol.replace("-", "/")
                    elif "/" not in ccxt_symbol:
                        ccxt_symbol = f"{ccxt_symbol}/USDT"
                    price = broker_client.get_ticker_price(ccxt_symbol)
            if price is not None and float(price) > 0:
                prices[pos.asset] = float(price)
        except Exception:
            continue
    return prices


def _should_alert_fast_monitor_blind(consecutive_blind_ticks: int, threshold: int) -> bool:
    """Sprint 46N (audit A6): decide whether THIS blind tick should
    trigger a SYSTEM_ERROR alert, given how many consecutive ticks in
    a row have had no prices at all.

    Extracted as a pure function (no I/O, no closures) so the alert
    cadence is unit-testable in isolation from `fast_monitor_tick`,
    which is a closure defined inside `main()` and can't easily be
    exercised directly in a test.

    Behavior: alert on the tick where the streak FIRST reaches
    `threshold` (e.g. 3 consecutive blind ticks), then again every
    `threshold` ticks after that (6, 9, 12, ...) — a single blind tick
    is treated as a possible transient blip (rate limit, brief network
    hiccup) and doesn't page anyone; a sustained blind streak escalates
    once and then keeps reminding periodically instead of either
    going silent forever after the first alert, or spamming Telegram
    every single tick (every ~2 minutes) for the full duration of a
    longer outage.
    """
    if threshold <= 0:
        # Defensive: a misconfigured threshold shouldn't crash the
        # tick or fire on every single blind tick unboundedly either.
        return consecutive_blind_ticks == 1
    if consecutive_blind_ticks < threshold:
        return False
    return (consecutive_blind_ticks - threshold) % threshold == 0


def _start_api_server(
    audit_path: str,
    positions_path: str,
    config_path: str = "config.yaml",
    broker_client=None,
    alpaca_broker=None,
    position_repo=None,
) -> None:
    """Sprint 46A: start the FastAPI/WebSocket dashboard backend as a
    daemon thread inside the bot process.

    Why here, not a separate service: the API is a thin READ-ONLY
    layer over the same on-disk state the bot already writes
    (audit.jsonl, positions.json) — see src/api/state.py's docstring.
    Sprint 46B's Next.js dashboard was built and wired to call this
    API (docker-compose maps host 8088 -> container 8080 on the
    `guaritradbot` service specifically for this), but nothing ever
    actually started uvicorn — this function was missing entirely,
    which is why the dashboard could deploy successfully and still
    show no live data (every request to NEXT_PUBLIC_API_URL just
    connection-refused).

    Sprint 46C: also registers `broker_client`/`alpaca_broker` (the
    SAME instances the trading loop uses) with `src.api.state` so
    `/api/state` can report real available cash per broker instead of
    the hardcoded 0.0 it always returned before — see
    `src/api/state.py::set_brokers()`.

    Sprint 46N (audit C8): also registers `position_repo` (the SAME
    long-lived PositionRepository instance the trading loop uses) with
    `src.api.state` via `set_position_repo()`, instead of every
    dashboard request building its own disposable disk-backed copy.
    That per-request copy is what let a dashboard "close position"
    action get silently undone the next time the bot's own in-memory
    repo saved — see `set_position_repo`'s docstring for the full
    "resurrected position" bug this fixes.

    Best-effort: any failure here (missing dependency, port already
    bound, etc.) is caught and logged — it must NEVER take down the
    trading loop. The dashboard is observability, not core function.
    """
    os.environ.setdefault("DASHBOARD_AUDIT_PATH", audit_path)
    os.environ.setdefault("DASHBOARD_POSITIONS_PATH", positions_path)
    os.environ.setdefault("DASHBOARD_CONFIG_PATH", config_path)
    try:
        pid_path = os.getenv("DASHBOARD_BOT_PID_FILE", "/tmp/guaritradbot.pid")
        with open(pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        print(f"[API] ⚠️ No se pudo escribir el PID file ({e}); POST /api/restart no funcionará.")
    try:
        import uvicorn
        from src.api.server import app as api_app
        from src.api.state import set_brokers as _api_set_brokers
        from src.api.state import set_position_repo as _api_set_position_repo

        _api_set_brokers(broker_client=broker_client, alpaca_broker=alpaca_broker)
        _api_set_position_repo(position_repo)

        port = int(os.getenv("DASHBOARD_API_PORT", "8080"))

        def _run():
            uvicorn.run(api_app, host="0.0.0.0", port=port, log_level="warning")

        t = threading.Thread(target=_run, name="dashboard-api", daemon=True)
        t.start()
        print(f"[API] 🌐 Dashboard API + WebSocket escuchando en 0.0.0.0:{port}")
    except Exception as e:
        print(
            f"[API] ⚠️ No se pudo iniciar el servidor del dashboard (fastapi/uvicorn): {e}. "
            "El bot sigue operando normalmente; solo el dashboard quedará sin datos en vivo."
        )


def main():
    parser = argparse.ArgumentParser(description="Guaritradbot Epic Multi-Agent Trading")
    parser.add_argument("--once", action="store_true", help="Execute the trading loop only once")
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a one-shot test message to Telegram and exit "
             "(verifies TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are wired correctly)",
    )
    args = parser.parse_args()

    # Sprint 34b: --test-telegram exits before any heavy init (no broker,
    # no workflow engine, no scheduler). Just enough to instantiate
    # NotificationAgent and ping Telegram.
    if args.test_telegram:
        config_path = "config.yaml"
        config = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}
            except Exception as e:
                print(f"[--test-telegram] Warning: could not load config.yaml: {e}")
        try:
            # Sprint 34b fix: removed redundant `from ... import
            # NotificationAgent` here. Python's scoping rule treats ANY
            # import inside a function as a local binding for the whole
            # function, which made the module-level import (line 29)
            # shadowed and the production code at line 341 raised
            # UnboundLocalError on every startup. Reusing the top-level
            # import is enough.
            agent = NotificationAgent(
                event_bus=None,  # no subscriptions needed for smoke test
                config=config,
                mode_override_path="audit/mode_override.json",
            )
            # Force-enable even if config has notifications.enabled=false,
            # because the test's whole point is to verify the wiring — if
            # Carlos disabled notifications globally, --test-telegram
            # should still try to send (and report the config state).
            agent.enabled = True
            print("[--test-telegram] Sending test message…")
            ok = agent.send_test_message()
            if ok:
                print("[--test-telegram] ✅ Telegram accepted the message. Check your chat.")
                sys.exit(0)
            else:
                print(
                    "[--test-telegram] ❌ Telegram send failed.\n"
                    "  Check that TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in .env\n"
                    "  (Coolify → Resources → guaritradbot → Environment)."
                )
                sys.exit(1)
        except Exception as e:
            print(f"[--test-telegram] ❌ Exception: {e}")
            sys.exit(2)

    print("=== Iniciando Bot Épico (Multi-Agente) ===")

    config_path = "config.yaml"
    config = {}
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}

    execution_mode = config.get("execution_mode", "auto")
    optimize_on_start = config.get("optimize_on_start", False)

    trading_cfg = dict(config.get("trading", {}) or {})
    # Sprint 46D: dashboard-editable trading settings. The dashboard's
    # Settings page can now save changes (max simultaneous trades, risk
    # per trade, min order size, etc.) via POST /api/config, but it
    # never touches config.yaml directly (PyYAML's dump() would wipe
    # every comment in that file — see src/api/state.py's module
    # docstring). Instead it writes to a small JSON override file, same
    # pattern as mode_override.json for the LIVE/PAPER toggle. We merge
    # it in here, BEFORE the individual risk_per_trade_pct/etc. locals
    # are read below — those are only read ONCE at startup and handed
    # to RiskManagerAgent's constructor (not re-read per cycle), so
    # this merge is what makes a saved dashboard change actually apply
    # on the bot's next restart.
    _trading_override_path = os.path.join(
        config.get("mandate", {}).get("audit_log_dir", "audit"),
        "trading_config_override.json",
    )
    if os.path.exists(_trading_override_path):
        try:
            with open(_trading_override_path, "r", encoding="utf-8") as f:
                _trading_overrides = json.load(f)
            if isinstance(_trading_overrides, dict):
                _applied = {k: v for k, v in _trading_overrides.items() if not k.startswith("_")}
                trading_cfg.update(_applied)
                if _applied:
                    print(f"[Init] Trading config override applied from {_trading_override_path}: {_applied}")
        except Exception as e:
            print(f"[Init] trading_config_override.json parse error (ignored): {e}")

    # Sprint 46F: dashboard-editable risk/mandate settings — the
    # drawdown kill-switch threshold/cooldown, the mandate's allowed-
    # symbols list, and the portfolio-risk gate caps (asset-class
    # concentration, correlation, CVaR, stress-test). Same override-
    # file pattern as trading_config_override.json above: never
    # touches config.yaml directly, mutates `config["risk"]` /
    # `config["mandate"]["allowed_symbols"]` in place BEFORE anything
    # below reads them (DrawdownKillSwitch, _build_mandate,
    # RiskManagerAgent's construction all read from `config` further
    # down in this function).
    _risk_override_path = os.path.join(
        config.get("mandate", {}).get("audit_log_dir", "audit"),
        "risk_config_override.json",
    )
    if os.path.exists(_risk_override_path):
        try:
            with open(_risk_override_path, "r", encoding="utf-8") as f:
                _risk_overrides = json.load(f)
            if isinstance(_risk_overrides, dict):
                _risk_applied = {k: v for k, v in _risk_overrides.items() if not k.startswith("_")}
                if _risk_applied:
                    config.setdefault("risk", {})
                    config.setdefault("mandate", {})
                    for _k, _v in _risk_applied.items():
                        if _k == "mandate_allowed_symbols":
                            config["mandate"]["allowed_symbols"] = _v
                        elif _k == "max_daily_trades":
                            # Sprint 46J: mandate.* field, same special-
                            # case treatment as allowed_symbols above —
                            # everything else in this loop is a risk.*
                            # field (see RISK_CONFIG_DEFAULTS).
                            config["mandate"]["max_daily_trades"] = _v
                        else:
                            config["risk"][_k] = _v
                    print(f"[Init] Risk/mandate config override applied from {_risk_override_path}: {_risk_applied}")
        except Exception as e:
            print(f"[Init] risk_config_override.json parse error (ignored): {e}")

    risk_per_trade_pct = trading_cfg.get("risk_per_trade_pct", 1.0)
    atr_stop_multiplier = trading_cfg.get("atr_stop_multiplier", 2.0)
    atr_take_profit_multiplier = trading_cfg.get("atr_take_profit_multiplier", 4.0)
    risk_reward_ratio = trading_cfg.get("risk_reward_ratio", 2.0)
    max_capital_per_trade_pct = trading_cfg.get("max_capital_per_trade_pct", 10.0)
    min_order_usd = trading_cfg.get("min_order_usd", 10.0)
    # Sprint 46N (audit A2): see RiskManagerAgent's constructor docstring
    # / config.yaml's comment for the full rationale.
    max_auto_adjust_risk_multiplier = float(trading_cfg.get("max_auto_adjust_risk_multiplier", 2.0))
    max_open_trades = trading_cfg.get("max_open_trades", 5)
    enable_position_replacement = trading_cfg.get("enable_position_replacement", True)
    replacement_score_threshold = float(trading_cfg.get("replacement_score_threshold", 0.20))
    min_profit_to_protect = float(trading_cfg.get("min_profit_to_protect", 0.0))
    # Sprint 46J: real binance.us taker fee (ONE-WAY, as a fraction —
    # e.g. 0.001 = 0.1%), charged on BOTH the entry and exit notional
    # when a crypto position closes (PositionRepository.close_position's
    # `fee_pct` docstring). Alpaca equities are commission-free, so they
    # always get 0.0 regardless of this setting — see
    # `_fee_pct_for_asset` below. Verify your actual tier at
    # https://www.binance.us/fee-schedule; this default is a reasonable
    # placeholder, not a guarantee of your account's real rate.
    crypto_taker_fee_pct = float(trading_cfg.get("crypto_taker_fee_pct", 0.001))

    # Sprint 46M: binance.us spot has no margin/borrow, so "short" crypto
    # signals were never real exchange shorts — see config.yaml's
    # allow_crypto_short comment for the live incident (repeated
    # simultaneous BTC-USD long+short pairs, CLOSE_FAILED "insufficient
    # balance") that surfaced this. Keep off unless real margin/futures
    # trading is wired in.
    allow_crypto_short = bool(trading_cfg.get("allow_crypto_short", False))

    broker_client = None
    exchange_cfg = config.get("exchange", {})
    if exchange_cfg:
        try:
            broker_client = BrokerClient(
                exchange_name=exchange_cfg.get("name", "binance"),
                use_testnet=exchange_cfg.get("use_testnet", True),
            )
        except Exception as e:
            print(f"[Broker] Error al inicializar: {e}. Modo paper-only.")

    # Sprint 43 C2 fix: initialize `audit` and `override_path` BEFORE
    # the Alpaca broker block. The previous order declared
    # `override_path` (line 194) AFTER it was used in
    # `AlpacaBroker(mode_override_path=override_path, ...)` (line 172),
    # which made Python treat it as a local variable and raise
    # UnboundLocalError on every init where ALPACA_* env vars were set.
    # The try/except at the old line 167-178 caught the error and made
    # the failure look like a credentials/network issue. Moving these
    # initializations up restores the multi-broker routing.
    audit = AuditLedger(_audit_path(config))
    override_path = str(audit.path.parent / "mode_override.json")

    # Sprint 45 fix (N1): `event_bus` was constructed at its old location
    # (originally right before `ExecutionNode(...)`, ~40 lines further
    # down) but was already referenced above that point — first by
    # `_build_mandate(..., event_bus=event_bus)` and then by the
    # kill-switch SYSTEM_ERROR publish block. Same bug class as the
    # original C2 (name used before assignment inside the same
    # function scope -> Python treats it as local for the whole
    # function body -> UnboundLocalError on every single startup,
    # unconditionally, since this path always runs). Constructing it
    # here — right alongside `audit`, which nothing else depends on —
    # makes it available to every consumer below, including
    # `_build_mandate` and the kill-switch block.
    event_bus = EventBus()

    # Sprint 36: Alpaca broker for equities/ETFs. OPTIONAL — bot
    # degrades gracefully to single-broker (crypto-only) if env vars
    # are missing. Only construct if BOTH keys are present, so the
    # absence of one doesn't half-init and fail later.
    #
    # Sprint 36.1: pass `mode_override_path` so the broker can read
    # the runtime `alpaca_paper` flag on every call. The dashboard's
    # Paper/Live toggle writes BOTH `mandate_enabled` and `alpaca_paper`
    # together, so one click switches both the B033 paper gate AND
    # the Alpaca endpoint in lockstep.
    alpaca_broker = None
    _alpaca_key = os.getenv("ALPACA_API_KEY")
    _alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    if _alpaca_key and _alpaca_secret:
        try:
            alpaca_broker = AlpacaBroker(
                api_key=_alpaca_key,
                secret_key=_alpaca_secret,
                paper=True,        # legacy, ignored at runtime
                mode_override_path=override_path,
            )
            _bal = alpaca_broker.get_usd_balance()
            print(f"[Init] Alpaca broker armado. Balance USD: ${_bal:.2f} (endpoint = runtime-driven por alpaca_paper en mode_override.json)")
        except Exception as e:
            print(f"[Init] ⚠️ Alpaca broker falló al inicializar: {e}. Sigo solo con crypto.")
            alpaca_broker = None
    else:
        print("[Init] ALPACA_API_KEY/ALPACA_SECRET_KEY no configuradas. Bot en modo crypto-only (equity signals fallarán con ALPACA_NOT_CONFIGURED).")

    brokers_config = config.get("brokers", {}) or {}

    # Sprint 12: mode_override.json takes precedence over config.yaml.
    # The dashboard writes here when the user toggles PAPER/LIVE.
    # This lets you flip modes WITHOUT editing config.yaml or restarting manually.
    #
    # NOTE: `audit` and `override_path` were originally assigned here
    # (after the Alpaca block), which caused an UnboundLocalError when
    # AlpacaBroker tried to use `override_path` (Sprint 43 C2). They
    # are now initialized above the Alpaca block. The mode_override.json
    # content-loading logic below stays here.
    if os.path.exists(override_path):
        try:
            with open(override_path, "r", encoding="utf-8") as f:
                mode_override = json.load(f)
            if "mandate_enabled" in mode_override:
                if "mandate" not in config:
                    config["mandate"] = {}
                config["mandate"]["enabled"] = bool(mode_override["mandate_enabled"])
                print(f"[Init] Mode override applied: mandate.enabled = {mode_override['mandate_enabled']} "
                      f"(set at {mode_override.get('switched_at', '?')})")
                audit.append("MODE_OVERRIDE_APPLIED", {
                    "mandate_enabled": mode_override["mandate_enabled"],
                    "switched_at": mode_override.get("switched_at"),
                    "switched_by": mode_override.get("switched_by", "?"),
                })
        except Exception as e:
            print(f"[Init] mode_override.json parse error (ignored): {e}")

    kill_switch = KillSwitch(config.get("mandate", {}).get("kill_switch_file", "/tmp/GUARITRADBOT_KILL"))
    # Sprint 43 H3 fix: instantiate DrawdownKillSwitch and wire it
    # into the trading loop. The class has been in
    # src/safety/kelly_drawdown.py since Sprint 30 with full
    # tests, but was never instantiated in main.py — so the
    # "revenge trading" safety net it provides was dormant.
    # The threshold and cooldown are config-driven (with safe
    # defaults) so the operator can tighten them per-strategy.
    _risk_cfg = config.get("risk", {}) or {}
    # Sprint 46N (audit A1): load persisted peak_equity/triggered/
    # triggered_at state (if any) instead of always constructing a
    # fresh switch — a bot restart must not silently forget an active
    # kill switch or reset the peak back to 0. threshold_pct/
    # cooldown_hours always come from the CURRENT config (never from
    # the persisted file) — see DrawdownKillSwitch.load()'s docstring.
    _drawdown_state_path = "data_store/drawdown_kill_state.json"
    drawdown_kill_switch = DrawdownKillSwitch.load(
        _drawdown_state_path,
        threshold_pct=float(_risk_cfg.get("drawdown_kill_threshold_pct", 15.0)),
        cooldown_hours=float(_risk_cfg.get("drawdown_cooldown_hours", 24.0)),
    )
    # Sprint 46F: these 4 portfolio-risk gate caps have existed as
    # RiskManagerAgent constructor params since Sprint 44/45, but
    # main.py never actually read them from config.yaml — they always
    # silently used the class's hard-coded defaults (60/75/20/70).
    # Reading them here (same `risk:` section as the drawdown settings
    # above, now with the Sprint 46F override already merged in) means
    # both config.yaml AND the dashboard's Settings page can actually
    # change them.
    max_asset_class_concentration_pct = float(_risk_cfg.get("max_asset_class_concentration_pct", 60.0))
    max_avg_correlation_pct = float(_risk_cfg.get("max_avg_correlation_pct", 75.0))
    max_cvar_95_pct = float(_risk_cfg.get("max_cvar_95_pct", 20.0))
    max_stress_drawdown_pct = float(_risk_cfg.get("max_stress_drawdown_pct", 70.0))

    # Sprint 46E: startup self-test for the drawdown kill switch. This
    # exact safety mechanism was completely dead (an UnboundLocalError
    # in job_with_monitor(), silently swallowed every cycle) for its
    # entire life before this sprint's fix — a startup self-test like
    # this would have caught it in seconds instead of an audit finding
    # it later. Runs against a THROWAWAY DrawdownKillSwitch instance
    # (never `drawdown_kill_switch` itself), so it can't corrupt real
    # equity tracking. Best-effort: logs loudly on failure but never
    # blocks startup (see src/safety/selftest.py's module docstring).
    from src.safety.selftest import run_startup_selftests
    run_startup_selftests(audit=audit, event_bus=event_bus)

    position_repo = PositionRepository("data_store/positions.json")

    # Sprint 46N (audit C7): a corrupt positions.json is quarantined
    # (not silently wiped) by PositionRepository itself — see its
    # `_quarantine_corrupt_file` docstring — but startup still needs to
    # make noise about it here, otherwise "not silently wiped on disk"
    # would still be "silently ignored in practice" if nothing tells
    # Carlos to go check whether he actually had open positions.
    if position_repo.load_error:
        _corrupt_msg = (
            f"⚠️ positions.json estaba corrupto al arrancar "
            f"({position_repo.load_error}). Copia de seguridad en "
            f"{position_repo.quarantined_path}. El bot arrancó con 0 "
            f"posiciones registradas — si tenías posiciones abiertas, "
            f"revisa la copia de seguridad y el estado real en el "
            f"broker ANTES de dejar operar al bot."
        )
        print(f"[Init] {_corrupt_msg}")
        audit.append("POSITIONS_FILE_CORRUPT", {
            "error": position_repo.load_error,
            "quarantined_path": str(position_repo.quarantined_path)
            if position_repo.quarantined_path else None,
        })
        if event_bus is not None:
            try:
                event_bus.publish("SYSTEM_ERROR", {
                    "kind": "POSITIONS_FILE_CORRUPT",
                    "error": position_repo.load_error,
                    "quarantined_path": str(position_repo.quarantined_path)
                    if position_repo.quarantined_path else None,
                })
            except Exception as e:
                print(f"[Init] No se pudo publicar SYSTEM_ERROR de POSITIONS_FILE_CORRUPT: {e}")

    # Sprint 46A/B: start the dashboard's HTTP/WebSocket API. Must come
    # after `audit`/`position_repo` exist (it reads the same on-disk
    # state) and before the main loop, so the dashboard has data from
    # the very first cycle.
    #
    # Sprint 46N (audit C8): pass `position_repo` too, so the dashboard
    # shares this EXACT instance instead of building its own disposable
    # copy per request — see _start_api_server's docstring / set_
    # position_repo's docstring for the "resurrected position" bug
    # this fixes.
    _start_api_server(
        audit_path=_audit_path(config),
        positions_path="data_store/positions.json",
        broker_client=broker_client,
        alpaca_broker=alpaca_broker,
        position_repo=position_repo,
    )

    # Sprint 25 fix: ALWAYS show paper position count at startup.
    # Carlos: "cuando cambio a live no me dice nada de las entradas en paper"
    # → The bot was silent about open paper positions. Now it always prints.
    #
    # B031 fix: previous expression was inverted — it called
    # `len(_open_paper) if isinstance(_open_paper, int)`, but
    # `count_open()` returns int, so the call became `len(5)` → TypeError.
    # Now we just use the int directly (count_open() always returns int).
    _open_paper = position_repo.count_open()
    if _open_paper > 0:
        print(
            f"\n⚠️  {_open_paper} paper position(s) detected in repo:"
        )
        for _p in position_repo.open():
            print(
                f"   • {_p.asset} {_p.direction.upper()} qty={_p.qty} @ ${_p.entry_price:.2f} "
                f"({_p.position_id[:24]})"
            )
        print(
            "   These exist in the LOCAL REPO only — they do NOT exist on the live exchange.\n"
            "   Run 'Clean Paper Positions' from the dashboard sidebar, or wait for the\n"
            "   pre-flight checklist (Sprint 22) to handle them automatically.\n"
        )

    # Sprint 22 + 25 fix: Paper→Live Transition Safety Check
    # Triggered when EITHER:
    #   (a) mandate.enabled=true AND exchange.use_testnet=false (canonical live)
    #   (b) mandate.enabled=true AND there are open paper positions
    #     (even if use_testnet=true, ghost positions are a problem)
    mandate_being_enabled = bool(config.get("mandate", {}).get("enabled", False))
    exchange_use_testnet = bool(config.get("exchange", {}).get("use_testnet", True))
    has_paper_positions = position_repo.count_open() > 0
    is_live_attempt = mandate_being_enabled and (
        not exchange_use_testnet or has_paper_positions
    )

    if is_live_attempt:
        from src.safety.paper_to_live import PaperToLiveChecklist
        interactive = sys.stdin.isatty() if hasattr(sys, "stdin") else False
        if not exchange_use_testnet:
            print(
                "\n🚀 Live mode detected (mandate.enabled=true, use_testnet=false).\n"
                "   Running pre-flight checklist...\n"
            )
        elif has_paper_positions:
            print(
                f"\n⚠️  {position_repo.count_open()} paper position(s) detected with mandate.enabled=true.\n"
                "   Running pre-flight checklist to handle them safely...\n"
            )
        checklist = PaperToLiveChecklist(
            position_repo=position_repo,
            audit=audit,
            broker=broker_client,
            interactive=interactive,
            auto_action=config.get("live_transition", {}).get("auto_action", "abort"),
            min_order_qty=config.get("live_transition", {}).get("dry_run_qty", 0.00001),
        )
        decision = checklist.run(dry_run=True)
        print(f"\n[Pre-flight] Decision: {decision}")
        if not decision.proceed:
            print(
                f"\n⛔ Pre-flight check BLOCKED the transition: {decision.reason}\n"
                "   Forcing mandate.enabled=false for safety.\n"
                "   Clean paper positions from the dashboard, then re-enable.\n"
            )
            audit.append("LIVE_TRANSITION_BLOCKED", {
                "reason": decision.reason,
                "forced_back_to_paper": True,
                "had_paper_positions": has_paper_positions,
            })
            config["mandate"]["enabled"] = False
            # Also write the override so the dashboard reflects it
            try:
                _override_path = "audit/mode_override.json"
                if os.path.exists(_override_path):
                    with open(_override_path, "r", encoding="utf-8") as _of:
                        _ov = json.load(_of)
                    _ov["mandate_enabled"] = False
                    _ov["forced_back_at"] = time.time()
                    with open(_override_path, "w", encoding="utf-8") as _of:
                        json.dump(_ov, _of, indent=2)
            except Exception as _ov_err:
                print(f"[Pre-flight] Could not update mode_override.json: {_ov_err}")
        else:
            print(f"\n✅ Pre-flight passed. Live mode is GO.")

    mandate_gate, mandate_cfg = _build_mandate(config, audit, position_repo=position_repo, event_bus=event_bus)

    if kill_switch.is_triggered():
        audit.append("BOT_START_BLOCKED_KILLSWITCH", {"reason": "kill_file_present"})
        # Sprint 43 C6 fix: the bot is refusing to start because someone
        # (probably Carlos) dropped a kill file. This is a critical state
        # that needs to be visible — if the bot dies silently Carlos may
        # not know it's been killed. Publish SYSTEM_ERROR.
        if event_bus is not None:
            try:
                event_bus.publish("SYSTEM_ERROR", {
                    "kind": "BOT_START_BLOCKED_KILLSWITCH",
                    "kill_switch_file": config.get("mandate", {}).get(
                        "kill_switch_file", "/tmp/GUARITRADBOT_KILL"
                    ),
                    "error": "⛔ Bot startup BLOQUEADO por kill-switch (kill file presente)",
                })
            except Exception as e:
                print(f"[Main] ⚠️ No se pudo publicar SYSTEM_ERROR en startup: {e}")
        print("⛔ Kill switch armado al startup — bot no arranca.")
        return

    audit.append(
        "BOT_START",
        {
            "execution_mode": execution_mode,
            "risk_per_trade_pct": risk_per_trade_pct,
            "mandate_enabled": mandate_cfg is not None,
            "open_positions_at_start": position_repo.count_open(),
        },
    )

    print(
        f"[Init] {position_repo.count_open()} posiciones abiertas cargadas "
        f"(realized PnL total ${position_repo.total_realized_pnl_usd():.4f})"
    )

    execution_node = ExecutionNode(
        event_bus,
        execution_mode=execution_mode,
        broker_client=broker_client,
        alpaca_broker=alpaca_broker,         # Sprint 36
        brokers_config=brokers_config,        # Sprint 36
        kill_switch=kill_switch,
        audit=audit,
        mode_override_path=override_path,  # B033: paper-mode gate
        position_repo=position_repo,         # Sprint 43 C5: persist on confirmed fill
        # Sprint 46I: real OCO stop-loss/take-profit orders on binance.us
        # for crypto longs, opt-in via config.yaml (off by default —
        # see src/execution/broker.py's OCO methods for the testing
        # caveat before enabling this in live mode).
        use_native_crypto_stops=bool(trading_cfg.get("use_native_crypto_stops", False)),
    )
    def _fee_pct_for_asset(asset: str) -> float:
        """Sprint 46J: crypto assets (binance.us) get the real taker
        fee; Alpaca equities are commission-free, so anything NOT
        classified as "crypto" by `_asset_class_for` (equity, or
        "unknown" — conservative default) gets 0.0. See
        `crypto_taker_fee_pct`'s comment above for the config source
        and PositionMonitor's docstring for how this gets used.
        """
        return crypto_taker_fee_pct if _asset_class_for(asset, brokers_config) == "crypto" else 0.0

    position_monitor = PositionMonitor(
        repo=position_repo,
        audit=audit,
        event_bus=event_bus,
        broker=broker_client,
        min_profit_to_protect=min_profit_to_protect,
        fee_pct_for_asset=_fee_pct_for_asset,
        # Sprint 46N (audit C1/C2): route closes by asset class + never
        # send a real order in paper mode.
        alpaca_broker=alpaca_broker,
        brokers_config=brokers_config,
        mode_override_path=override_path,
    )

    strategy_params = None
    if optimize_on_start:
        print("[Optimizador] Iniciando Grid Search de parámetros...")
        try:
            from test_hyperopt import create_dummy_data
            df_hist = create_dummy_data()
            hyperopt = HyperoptManager()

            def rsi_sig(data, **p):
                return StrategyAgent.generate_vectorized_signals(data, strategy_type="RSI", **p)

            param_space = {"rsi_oversold": [25, 30, 35], "rsi_overbought": [65, 70, 75]}
            best_p = hyperopt.optimize("RSI_MeanReversion", df_hist, param_space, rsi_sig)
            if best_p:
                strategy_params = best_p
        except Exception as e:
            print(f"[Optimizador] Error durante la optimización: {e}")

    registry = {
        "MarketAnalystAgent": MarketAnalystAgent(event_bus=event_bus, audit=audit),
        "StrategyAgent": StrategyAgent(strategy_params=strategy_params, audit=audit),
        "RiskManagerAgent": RiskManagerAgent(
            broker_client=broker_client,
            risk_per_trade_pct=risk_per_trade_pct,
            max_capital_per_trade_pct=max_capital_per_trade_pct,
            atr_stop_multiplier=atr_stop_multiplier,
            atr_take_profit_multiplier=atr_take_profit_multiplier,
            risk_reward_ratio=risk_reward_ratio,
            max_open_trades=max_open_trades,
            min_order_usd=min_order_usd,
            # Sprint 46N (audit A2).
            max_auto_adjust_risk_multiplier=max_auto_adjust_risk_multiplier,
            event_bus=event_bus,
            mandate_gate=mandate_gate,
            audit=audit,
            position_repo=position_repo,
            enable_position_replacement=enable_position_replacement,
            replacement_score_threshold=replacement_score_threshold,
            # Sprint 46F: previously always used this constructor's
            # hard-coded defaults regardless of config.yaml — now
            # config-driven (and dashboard-editable via
            # risk_config_override.json, see the Sprint 46F block
            # near the top of main()).
            max_asset_class_concentration_pct=max_asset_class_concentration_pct,
            max_avg_correlation_pct=max_avg_correlation_pct,
            max_cvar_95_pct=max_cvar_95_pct,
            max_stress_drawdown_pct=max_stress_drawdown_pct,
            allow_crypto_short=allow_crypto_short,
            # Sprint 46N (audit C1/C2): route replacement-closes by
            # asset class + never send a real order in paper mode.
            alpaca_broker=alpaca_broker,
            brokers_config=brokers_config,
            mode_override_path=override_path,
        ),
        "DebateAgent": DebateAgent(position_repo=position_repo, audit=audit),
        "ExecutionAgent": ExecutionAgent(event_bus=event_bus),
        "NotificationAgent": NotificationAgent(event_bus=event_bus, config=config),
    }

    engine = WorkflowEngine(registry)
    workflow_path = os.path.join("src", "workflows", "trading_loop.yaml")
    if not os.path.exists(workflow_path):
        print(f"Error: {workflow_path} no encontrado.")
        return
    workflow_data = engine.load_workflow(workflow_path)

    # Sprint 46G: capture the FULL configured asset universe once, at
    # startup, from the analyze_market step's `inputs.assets` (trading_
    # loop.yaml). job_with_monitor() re-filters THIS list every cycle
    # against live broker balances (see _get_active_asset_classes) and
    # writes the filtered result back into workflow_data's step —
    # engine.run() reads it fresh every call since it's the same dict
    # object EpochScheduler holds. Keeping the untouched original here
    # means a broker that regains funds later gets its assets back
    # immediately, instead of the universe permanently shrinking.
    _analyze_market_step = next(
        (s for s in workflow_data.get("steps", []) if s.get("id") == "analyze_market"),
        None,
    )
    _full_trading_assets = list(
        (_analyze_market_step or {}).get("inputs", {}).get("assets", [])
    )

    # Workflow customizado: insertamos un paso de PositionMonitor antes de
    # que se ejecute la estrategia, para que stops/TPs se cierren primero.
    # Si el monitor cierra una posición, queda registrada antes de la
    # nueva ronda de señales.

    # Sprint 5: epoch re-optimization real (antes era placeholder).
    # Construimos HyperoptManager y se lo pasamos al scheduler.
    hyperopt = HyperoptManager()
    scheduler = EpochScheduler(
        engine,
        workflow_data,
        config_path,
        market_analyst=registry["MarketAnalystAgent"],
        strategy_agent=registry["StrategyAgent"],
        hyperopt=hyperopt,
        audit=audit,
        assets=("BTC-USD", "SPY", "GLD", "QQQ", "USO"),
        event_bus=event_bus,  # Sprint 45 fix (N6/H11): alert on aborted cycles
    )

    # Sprint 23: Live Equity Tracker
    # Carlos wanted to see cents-level P&L in real time, especially with
    # $10 balance. Tracker initializes with the broker's actual balance.
    # Falls back to $10 (paper default) if broker is unreachable.
    from src.safety.equity_tracker import (
        EquityTracker, persist_tracker, load_tracker,
    )
    _equity_state_path = "data_store/equity_state.json"
    try:
        # Sprint 24: try to load persisted state first (crash-only)
        if os.path.exists(_equity_state_path):
            equity_tracker = load_tracker(
                _equity_state_path,
                position_repo=position_repo,
                audit=audit,
            )
            print(f"[EquityTracker] loaded from disk: ${equity_tracker.starting_balance:.4f} "
                  f"({len(equity_tracker.history)} snapshots)")
        else:
            # First time: use broker balance or $10 fallback
            try:
                _initial_balance = broker_client.get_usdt_balance() if broker_client else 10.0
                if _initial_balance is None or _initial_balance <= 0:
                    _initial_balance = 10.0
            except Exception:
                _initial_balance = 10.0
            equity_tracker = EquityTracker(
                starting_balance=_initial_balance,
                position_repo=position_repo,
                audit=audit,
                history_size=200,
            )
            print(f"[EquityTracker] initialized with ${_initial_balance:.4f}")
    except Exception as _eq_init_err:
        # Fallback: simple in-memory tracker, no persistence
        print(f"[EquityTracker] init fallback: {_eq_init_err}")
        equity_tracker = EquityTracker(
            starting_balance=10.0,
            position_repo=position_repo,
            audit=audit,
            history_size=200,
        )

    # Monkey-patch el scheduler.job para correr el monitor antes
    original_job = scheduler.job

    # Sprint 34: per-position P&L update scheduler (hourly Telegram updates).
    # Initialized lazily on the first fast_monitor_tick so the
    # config["notifications"] block is available at construction time.
    _pupdate_scheduler = None

    # Sprint 46N (audit A6): count consecutive fast_monitor_tick runs
    # where price fetching returned NOTHING while positions were open
    # ("flying blind" — SL/TP protection can't run without a current
    # price). The hourly cycle already alerts on a total historical-
    # data-feed failure (MarketAnalystAgent's MARKET_DATA_TOTAL_FAILURE,
    # Sprint 43 C6), but that's a different code path (fetch_one for
    # indicators) and never covered this fast, live-price-only tick —
    # a data-provider outage here could silently leave every open
    # position unprotected for as long as the outage lasts, with
    # nothing telling Carlos to go check manually.
    _blind_tick_count = 0
    _FAST_MONITOR_BLIND_ALERT_THRESHOLD = 3

    def fast_monitor_tick():
        """Sprint 46I — decoupled, fast-cadence position protection.

        Carlos's concern (verbatim): "si es cada hora puede perder la
        opcion de vender/comprar... y si en ese espacio entre un
        analisis y otro pierde una gran oportunidad?" He was right —
        before this, stop-loss/take-profit checks ran on the SAME
        hourly cadence as the full multi-agent analysis cycle, so a
        position could blow through its stop and sit unprotected for
        up to an hour before the bot even looked at it again.

        This function is scheduled independently (see
        config.yaml's schedule.fast_monitor_interval_minutes, default
        2). Sprint 46N (audit A5): it now runs on its own dedicated
        daemon thread with its own timer (see the
        `_fast_monitor_loop`/`_fast_monitor_thread` block near the end
        of main()) instead of being registered on the same global
        `schedule` instance / single-threaded while-loop that also
        drives job_with_monitor — see that block's comment for the
        full rationale. It runs ONLY the position-protection half of
        what job_with_monitor used to do inline: SL/TP polling, smart
        profit-take, OCO reconciliation (for crypto positions using
        Sprint 46I's native_oco protection — see position_monitor.py),
        equity tracking, and per-position P&L update notifications. It
        never touches new-entry generation (StrategyAgent/DebateAgent/
        RiskManagerAgent) — that stays on the hourly cycle in
        job_with_monitor, which is heavier (yfinance fetches across the
        full asset universe, GP/hyperopt-adjacent work) and doesn't
        need sub-hour freshness the way protecting an already-open
        position does.
        """
        nonlocal _blind_tick_count
        opens = position_repo.open()
        if not opens:
            # Nothing to protect right now — reset so a real blind
            # streak later starts counting from zero, not from
            # whatever it was before the book emptied out.
            _blind_tick_count = 0
            return
        prices = _fetch_prices_for_open_positions(
            position_repo,
            broker_client=broker_client,
            alpaca_broker=alpaca_broker,
            brokers_config=brokers_config,
        )
        if not prices:
            # Sprint 46N (audit A6): every price fetch failed this tick
            # while positions are open — SL/TP protection did NOT run.
            # Track + alert instead of silently returning (previous
            # behavior). A single blind tick can be a transient blip
            # (rate limit, brief network hiccup); alert once the streak
            # crosses the threshold, then repeat the alert every
            # `threshold` ticks after that so it isn't a one-time
            # notice Carlos could miss, but also isn't Telegram spam
            # every 2 minutes for the entire duration of a longer outage.
            _blind_tick_count += 1
            assets_at_risk = sorted({p.asset for p in opens})
            print(
                f"[FastMonitor] ⚠️ SIN PRECIOS este ciclo ({_blind_tick_count} "
                f"consecutivos) — {len(opens)} posición(es) abierta(s) sin "
                f"protección SL/TP este ciclo: {assets_at_risk}"
            )
            if audit:
                audit.append("FAST_MONITOR_BLIND", {
                    "consecutive_blind_ticks": _blind_tick_count,
                    "open_positions": len(opens),
                    "assets": assets_at_risk,
                })
            should_alert = _should_alert_fast_monitor_blind(
                _blind_tick_count, _FAST_MONITOR_BLIND_ALERT_THRESHOLD
            )
            if should_alert and event_bus is not None:
                event_bus.publish("SYSTEM_ERROR", {
                    "kind": "FAST_MONITOR_BLIND",
                    "consecutive_blind_ticks": _blind_tick_count,
                    "open_positions": len(opens),
                    "assets": assets_at_risk,
                    "error": (
                        f"📉 fast_monitor_tick lleva {_blind_tick_count} ciclos "
                        f"consecutivos SIN PRECIOS para {len(opens)} posición(es) "
                        f"abierta(s) ({', '.join(assets_at_risk)}). La protección "
                        f"SL/TP no puede evaluarse sin precio actual — verificar "
                        f"el proveedor de datos (yfinance/broker)."
                    ),
                })
            return
        # Got at least one price this tick — the blind streak (if any) is over.
        _blind_tick_count = 0
        try:
            # 1a. SL/TP mechanical check (+ OCO reconciliation for
            # native_oco positions — see position_monitor.py).
            closed = position_monitor.check(prices)
            if closed:
                print(f"[PositionMonitor] {len(closed)} posiciones cerradas por stops/TPs")

            # 1b. Sprint 18: smart profit-take on reversal signals.
            # Read latest HYPOTHESIS_GENERATED from audit (last 1h) and
            # close any open position in profit that has a strong
            # opposite signal.
            try:
                import time as _t
                recent_hyps = audit.read_since(_t.time() - 3600)
                signals = [
                    h for h in recent_hyps
                    if h.get("event_type") == "HYPOTHESIS_GENERATED"
                ]
                if signals:
                    early_closed = position_monitor.check_with_signals(
                        current_prices=prices,
                        signals=signals,
                        signal_min_strength=0.6,
                    )
                    if early_closed:
                        print(
                            f"[PositionMonitor] {len(early_closed)} posiciones "
                            f"cerradas por SMART_PROFIT_TAKE (reversal)"
                        )
            except Exception as e2:
                print(f"[PositionMonitor] smart-profit-take falló: {e2}")

            # 1c. Refresh RiskAgent's current_prices view so position
            # replacement scoring uses live prices.
            try:
                rm = registry.get("RiskManagerAgent")
                if rm is not None:
                    rm.current_prices = prices
            except Exception:
                pass

            # Sprint 23: update equity tracker with current prices
            try:
                snap = equity_tracker.update(prices)
                from src.safety.equity_tracker import format_equity_line
                print(f"  [Equity] {format_equity_line(snap, precision=4)}")
                # Sprint 24: persist to disk (crash-only)
                try:
                    persist_tracker(equity_tracker, _equity_state_path)
                except Exception as _persist_err:
                    print(f"  [Equity] persist falló: {_persist_err}")
            except Exception as eqe:
                print(f"  [Equity] tracker update falló: {eqe}")

            # Sprint 34: hourly P&L update scheduler. Emits
            # POSITION_UPDATE events at the configured cadence
            # (default 60 min) per position. Skips silently if no
            # open positions or no prices available. The
            # NotificationAgent subscribed to POSITION_UPDATE
            # sends the actual Telegram message.
            try:
                from src.notifications.position_update_scheduler import (
                    PositionUpdateScheduler,
                )
                nonlocal _pupdate_scheduler
                if _pupdate_scheduler is None:
                    _pupdate_interval = int(
                        config.get("notifications", {}).get(
                            "position_update_minutes", 60
                        )
                    )
                    _pupdate_min_pnl = float(
                        config.get("notifications", {}).get(
                            "position_update_min_pnl_usd", 0.0
                        )
                    )
                    _pupdate_scheduler = PositionUpdateScheduler(
                        position_repo=position_repo,
                        event_bus=event_bus,
                        interval_minutes=_pupdate_interval,
                        min_pnl_usd=_pupdate_min_pnl,
                    )
                    print(
                        f"  [PosUpdate] scheduler armed: "
                        f"interval={_pupdate_interval}m, "
                        f"min_pnl=${_pupdate_min_pnl:.2f}"
                    )
                n_emitted = _pupdate_scheduler.tick(prices)
                if n_emitted:
                    print(f"  [PosUpdate] emitted {n_emitted} update(s)")
                # Drop any closed positions from the cadence map
                open_ids = {p.position_id for p in position_repo.open()}
                for pid in list(_pupdate_scheduler._last_update.keys()):
                    if pid not in open_ids:
                        _pupdate_scheduler.clear_position(pid)
            except Exception as _pue:
                print(f"  [PosUpdate] scheduler falló: {_pue}")
        except Exception as e:
            print(f"[PositionMonitor] check falló: {e}")

    def job_with_monitor():
        # Sprint 46I: position protection (stop-loss/take-profit,
        # smart profit-take, OCO reconciliation, equity tracking) no
        # longer lives here — it runs independently on a faster
        # cadence via `fast_monitor_tick` (see above and the
        # dedicated fast-monitor-thread block below main()). This
        # function now only does the drawdown check + capital routing
        # + manual pause gates, then the full hourly analysis cycle
        # (original_job). It still needs its OWN price snapshot for
        # the drawdown equity calc below — fetched independently from
        # fast_monitor_tick's (different cadence, simplest to keep
        # them decoupled rather than share mutable state between two
        # differently-timed scheduled jobs).
        # Sprint 43 H3 (fixed for real in 46D; equity source fixed in
        # 46N audit A1): drawdown kill switch check. If the account
        # has dropped more than the configured threshold from its peak
        # equity, the bot is in "revenge trading" territory — it
        # should stop opening NEW positions until the cooldown
        # elapses.
        #
        # Sprint 46D fix: previously this did `return` immediately on
        # trigger, which — despite the comment directly above it
        # claiming otherwise — skipped the position-monitor block
        # entirely (it's later in the same function). That meant a
        # drawdown pause would ALSO freeze SL/TP protection on already-
        # open positions, the opposite of what you want during a
        # drawdown. Now we only set a flag that skips step 2 (the
        # normal workflow / new entries) at the very end; step 1
        # (monitor) always runs below regardless.
        #
        # Sprint 46N (audit A1) fix: this used to build its own
        # "current_equity" as `position_repo.total_realized_pnl_usd()
        # + sum(pos.unrealized_pnl(prices.get(pos.asset, 0.0)) for pos
        # in opens)` — two compounding bugs in that one expression:
        #   1. `prices.get(pos.asset, 0.0)` defaulted a MISSING price
        #      (e.g. one failed yfinance fetch for a single asset) to
        #      $0.0, which `unrealized_pnl` then treats as "this asset
        #      is now worth nothing" — a long position's unrealized
        #      P&L becomes `-entry_price * qty`, i.e. a fabricated
        #      ~100% loss on that ONE position from a data hiccup, not
        #      a real market move. This produced the impossible
        #      -264%/-212% drawdown alerts Carlos saw.
        #   2. The "equity" base was pure cumulative P&L (starting
        #      near 0), not real account equity (starting balance +
        #      P&L) — so drawdown_pct was computed relative to a
        #      tiny/zero peak, wildly exaggerating the percentage for
        #      the same dollar move.
        # `equity_tracker` (constructed above, updated every
        # fast_monitor_tick with live prices) already computes this
        # correctly: its equity base is `starting_balance + realized +
        # unrealized`, and its per-position loop skips any asset with
        # no current price entirely (contributes $0, not "-100%") —
        # see EquityTracker.update()'s `if price is not None` guard.
        # Reusing its latest snapshot fixes both problems by
        # construction and avoids a second, redundant yfinance fetch
        # this function no longer needs.
        dd_triggered = False
        try:
            current_equity = equity_tracker.latest().total_equity
            dd_state = drawdown_kill_switch.update(current_equity)
            # Sprint 46N (audit A1): persist peak_equity/triggered/
            # triggered_at after every update so a bot restart can't
            # silently forget an active kill switch or reset the peak
            # back to 0 — see DrawdownKillSwitch.persist()'s docstring.
            try:
                drawdown_kill_switch.persist(_drawdown_state_path)
            except Exception as _dd_persist_err:
                print(f"[DrawdownKill] persist falló (continuando): {_dd_persist_err}")
            if dd_state.triggered:
                dd_triggered = True
                if audit:
                    audit.append("BOT_DRAWDOWN_KILL_ACTIVE", {
                        "drawdown_pct": round(dd_state.drawdown_pct, 3),
                        "peak_equity": dd_state.peak_equity,
                        "current_equity": current_equity,
                        "cooldown_remaining_hours": round(dd_state.cooldown_remaining_hours, 2),
                    })
                if event_bus:
                    event_bus.publish("SYSTEM_ERROR", {
                        "kind": "DRAWDOWN_KILL_ACTIVE",
                        "drawdown_pct": round(dd_state.drawdown_pct, 3),
                        "error": (f"🛑 Drawdown kill switch ACTIVO: "
                                  f"{dd_state.drawdown_pct:.2f}% desde peak. "
                                  f"Bot NO abre nuevas posiciones por "
                                  f"{dd_state.cooldown_remaining_hours:.1f}h."),
                    })
                # Skip step 2 (new entries) this cycle — step 1 (monitor,
                # right below) still runs so SL/TP can still close.
        except Exception as e:
            print(f"[DrawdownKill] check failed (continuing): {e}")
            # Sprint 46D fix: a crashing safety check must be as loud as
            # a triggered one — previously this was print-only, so a
            # broken check and a passing check looked identical in the
            # logs/dashboard. Now it's visible in the audit trail too.
            if audit:
                audit.append("DRAWDOWN_CHECK_ERROR", {"error": str(e)[:300]})
            if event_bus:
                try:
                    event_bus.publish("SYSTEM_ERROR", {
                        "kind": "DRAWDOWN_CHECK_ERROR",
                        "error": f"⚠️ Drawdown kill-switch check falló (bot sigue operando): {e}",
                    })
                except Exception:
                    pass

        # Sprint 46G: capital-aware asset routing. Re-check broker
        # balances every cycle and narrow the analyze_market step's
        # asset list to only the classes that currently have money —
        # crypto-only if just binance is funded, equity-only if just
        # Alpaca is funded, both if both are (even at the $10 minimum).
        # See _get_active_asset_classes' docstring for the fail-open
        # rationale.
        capital_blocked = False
        try:
            if _analyze_market_step is not None and _full_trading_assets:
                _active_classes = _get_active_asset_classes(
                    broker_client, alpaca_broker, min_usd=min_order_usd
                )
                _filtered_assets = [
                    a for a in _full_trading_assets
                    if _asset_class_for(a, brokers_config) in _active_classes
                    or _asset_class_for(a, brokers_config) == "unknown"
                ]
                if not _filtered_assets:
                    capital_blocked = True
                    if audit:
                        audit.append("CAPITAL_ROUTING_BLOCKED", {
                            "active_classes": sorted(_active_classes),
                            "full_assets": _full_trading_assets,
                        })
                    print(
                        "[CapitalRouting] ⛔ Ningún broker tiene balance suficiente "
                        f"(${min_order_usd:.2f} mínimo) — ciclo de nuevas entradas SALTADO."
                    )
                else:
                    if set(_filtered_assets) != set(_full_trading_assets):
                        if audit:
                            audit.append("CAPITAL_ROUTING_APPLIED", {
                                "active_classes": sorted(_active_classes),
                                "assets_used": _filtered_assets,
                                "assets_full": _full_trading_assets,
                            })
                        print(
                            f"[CapitalRouting] Universo de assets ajustado a "
                            f"{_filtered_assets} (clases activas: {sorted(_active_classes)})"
                        )
                    _analyze_market_step["inputs"]["assets"] = _filtered_assets
        except Exception as e:
            print(f"[CapitalRouting] check falló (continuando sin filtrar): {e}")

        # Sprint 46H: manual Stop/Start toggle from the dashboard.
        # Checked every cycle (see _is_trading_paused's docstring) so a
        # dashboard click takes effect on the NEXT cycle — no restart
        # needed. Only gates step 2 (new entries) below, same as the
        # drawdown/capital gates above; step 1 (monitor) already ran.
        manual_paused = False
        try:
            manual_paused = _is_trading_paused(
                config.get("mandate", {}).get("audit_log_dir", "audit")
            )
        except Exception as e:
            print(f"[TradingPause] check falló (continuando sin pausar): {e}")

        # 2. Workflow normal — skipped while the drawdown kill switch is
        # active (dd_triggered), while no broker has enough capital to
        # trade (capital_blocked), OR while manually paused from the
        # dashboard (manual_paused). Step 1 (monitor) already ran
        # unconditionally, so SL/TP protection on existing positions is
        # never paused by any of these three gates.
        if not dd_triggered and not capital_blocked and not manual_paused:
            original_job()
        elif dd_triggered:
            print("[DrawdownKill] Ciclo de nuevas entradas SALTADO (cooldown activo). Monitor de SL/TP sigue corriendo normalmente.")
        elif capital_blocked:
            print("[CapitalRouting] Ciclo de nuevas entradas SALTADO (sin capital disponible). Monitor de SL/TP sigue corriendo normalmente.")
        else:
            print("[TradingPause] Ciclo de nuevas entradas SALTADO (pausado manualmente desde el dashboard). Monitor de SL/TP sigue corriendo normalmente.")

    scheduler.job = job_with_monitor

    _fast_monitor_minutes = float(
        config.get("schedule", {}).get("fast_monitor_interval_minutes", 2)
    )

    # Run once immediately at startup (both --once and daemon modes), so
    # open positions are protected right away instead of waiting up to
    # fast_monitor_interval_minutes for the first tick.
    try:
        fast_monitor_tick()
    except Exception as e:
        print(f"[Init] fast_monitor_tick inicial falló (continuando): {e}")

    # Sprint 46N (audit A5): fast_monitor_tick now runs on its OWN daemon
    # thread with its own timer, instead of being registered on the SAME
    # global `schedule` instance / single-threaded `while True:
    # schedule.run_pending()` loop that also drives the hourly
    # job_with_monitor cycle (EpochScheduler.start(),
    # src/execution/scheduler.py:216-221). Both jobs used to run
    # sequentially on that one thread — a slow hourly cycle (15 yfinance
    # downloads with retries, plus per-hypothesis portfolio-risk-gate
    # yfinance calls, which could together take 10-20 minutes under Yahoo
    # rate limiting) could starve fast_monitor_tick for that entire
    # duration, suspending SL/TP protection on open positions exactly when
    # the bot is busiest. This thread is fully independent: its own
    # sleep/tick timer, never blocked on (or blocking) job_with_monitor.
    #
    # Thread-safety (verified, not just assumed): PositionRepository
    # guards every read/write with a threading.RLock (Sprint 46 C8) and
    # close_position() is idempotent — it checks is_open inside the lock
    # and returns None if already closed — so a race between this
    # thread's SL/TP close and job_with_monitor's replacement-close on
    # the same position resolves safely by construction.
    # AuditLedger.append() serializes via fcntl.flock per write.
    # EventBus.publish() only iterates `subscribers`, which is mutated
    # only at startup (subscribe()), so concurrent publish() calls are
    # safe. EquityTracker.history is a deque with a single writer (this
    # thread) and a single reader (job_with_monitor) — safe under
    # CPython's GIL without an extra lock.
    #
    # Only started in daemon mode: `--once` never enters the long-lived
    # while loop at all (scheduler.start(run_once_for_test=True) runs one
    # cycle and returns before the process exits), so a background timer
    # thread would never get a chance to fire — the synchronous call
    # above already covers --once.
    _fast_monitor_lock = threading.Lock()
    _fast_monitor_stop_event = threading.Event()

    def _fast_monitor_loop():
        interval_seconds = max(_fast_monitor_minutes * 60.0, 1.0)
        while not _fast_monitor_stop_event.wait(interval_seconds):
            # Non-blocking acquire: if a previous tick is still running
            # (slower than the configured interval — e.g. a broker/
            # exchange hiccup), skip this tick rather than queuing up
            # overlapping runs against the same broker clients/repo.
            if not _fast_monitor_lock.acquire(blocking=False):
                print("[FastMonitor] tick anterior aún en curso; salteando este tick.")
                continue
            try:
                fast_monitor_tick()
            except Exception as e:
                print(f"[FastMonitor] tick falló (continuando): {e}")
            finally:
                _fast_monitor_lock.release()

    if not args.once:
        _fast_monitor_thread = threading.Thread(
            target=_fast_monitor_loop,
            name="fast-monitor-thread",
            daemon=True,
        )
        _fast_monitor_thread.start()
        print(
            f"[Init] Fast position monitor armado en su propio hilo: cada "
            f"{_fast_monitor_minutes} min (independiente del ciclo de análisis "
            f"de {config.get('schedule', {}).get('run_interval_hours', 1)}h y "
            f"del scheduler de un solo hilo que lo corre — Sprint 46N audit A5)"
        )
    else:
        print("[Init] Fast position monitor: modo --once, no se arma hilo en "
              "background (ya corrió una vez arriba).")

    try:
        if args.once:
            print("[System] Corriendo en modo UNA SOLA VEZ (--once)")
            audit.append("WORKFLOW_START", {"mode": "once"})
            scheduler.start(run_once_for_test=True)
            audit.append("WORKFLOW_END", {"mode": "once"})
            print("\n=== Ciclo Único Completado ===")
            summary = audit.summary()
            print(f"📒 Audit: {summary['total_events']} events, {len(summary['by_type'])} types")
            print(f"   Audit file: {audit.path}")
            print(f"📊 Posiciones: {position_repo.count_open()} abiertas, "
                  f"${position_repo.total_realized_pnl_usd():.4f} realized PnL total")
        else:
            print("[System] Iniciando Demonio (Modo Épocas)...")
            audit.append("WORKFLOW_START", {"mode": "daemon"})
            scheduler.start(run_once_for_test=False)
    except KeyboardInterrupt:
        audit.append("BOT_STOP_KEYBOARDINT", {})
        print("\nBot detenido por el usuario (Ctrl+C).")
    except Exception as e:
        audit.append("BOT_STOP_EXCEPTION", {"error": str(e)})
        raise


if __name__ == "__main__":
    main()
