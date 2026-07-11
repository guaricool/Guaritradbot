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
    )
    return (MandateGate(mc, audit_ledger=audit, position_repo=position_repo, event_bus=event_bus), mc)


def _start_api_server(audit_path: str, positions_path: str, config_path: str = "config.yaml") -> None:
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
    trading_cfg = config.get("trading", {})
    risk_per_trade_pct = trading_cfg.get("risk_per_trade_pct", 1.0)
    atr_stop_multiplier = trading_cfg.get("atr_stop_multiplier", 2.0)
    atr_take_profit_multiplier = trading_cfg.get("atr_take_profit_multiplier", 4.0)
    risk_reward_ratio = trading_cfg.get("risk_reward_ratio", 2.0)
    max_capital_per_trade_pct = trading_cfg.get("max_capital_per_trade_pct", 10.0)
    min_order_usd = trading_cfg.get("min_order_usd", 10.0)
    max_open_trades = trading_cfg.get("max_open_trades", 5)
    enable_position_replacement = trading_cfg.get("enable_position_replacement", True)
    replacement_score_threshold = float(trading_cfg.get("replacement_score_threshold", 0.20))
    min_profit_to_protect = float(trading_cfg.get("min_profit_to_protect", 0.0))

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
    drawdown_kill_switch = DrawdownKillSwitch(
        threshold_pct=float(config.get("risk", {}).get("drawdown_kill_threshold_pct", 15.0)),
        cooldown_hours=float(config.get("risk", {}).get("drawdown_cooldown_hours", 24.0)),
    )
    position_repo = PositionRepository("data_store/positions.json")

    # Sprint 46A/B: start the dashboard's HTTP/WebSocket API. Must come
    # after `audit`/`position_repo` exist (it reads the same on-disk
    # state) and before the main loop, so the dashboard has data from
    # the very first cycle.
    _start_api_server(
        audit_path=_audit_path(config),
        positions_path="data_store/positions.json",
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
    )
    position_monitor = PositionMonitor(
        repo=position_repo,
        audit=audit,
        event_bus=event_bus,
        broker=broker_client,
        min_profit_to_protect=min_profit_to_protect,
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
            event_bus=event_bus,
            mandate_gate=mandate_gate,
            audit=audit,
            position_repo=position_repo,
            enable_position_replacement=enable_position_replacement,
            replacement_score_threshold=replacement_score_threshold,
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
    # Initialized lazily on the first job_with_monitor tick so the
    # config["notifications"] block is available at construction time.
    _pupdate_scheduler = None

    def job_with_monitor():
        # Sprint 43 H3 fix: drawdown kill switch check. If the
        # account has dropped more than the configured threshold
        # from its peak equity, the bot is in "revenge trading"
        # territory — it should stop opening new positions until
        # the cooldown elapses. The check uses realized PnL +
        # unrealized PnL of open positions as the current
        # equity, peak is tracked internally.
        try:
            current_equity = position_repo.total_realized_pnl_usd() + sum(
                pos.unrealized_pnl(prices.get(pos.asset, 0.0))
                for pos in position_repo.open()
            ) if (prices and (prices := {p.asset: 0 for p in position_repo.open()})) else position_repo.total_realized_pnl_usd()
            # The above is a one-liner that handles the case where
            # we have no prices — fall back to realized-only equity.
            # For a more accurate drawdown we'd want the price for
            # each open position, but the realized PnL is the
            # conservative input (no unrealized gains count).
            dd_state = drawdown_kill_switch.update(current_equity)
            if dd_state.triggered:
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
                # Skip the workflow this cycle. The position
                # monitor still runs (so SL/TP can still close
                # positions), but no new trades.
                return
        except Exception as e:
            print(f"[DrawdownKill] check failed (continuing): {e}")

        # 1. Monitor: cierra stops/TPs antes que la nueva ronda
        try:
            opens = position_repo.open()
            if opens:
                from src.agents.market_analyst import MarketAnalystAgent as _MA
                ma = _MA()
                prices = {}
                for pos in opens:
                    try:
                        df = ma.fetch_one(pos.asset, interval="1d", period="1mo")
                        if df is not None and len(df) > 0:
                            prices[pos.asset] = float(df["Close"].iloc[-1])
                    except Exception:
                        continue
                if prices:
                    # 1a. SL/TP mechanical check
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

        # 2. Workflow normal
        original_job()

    scheduler.job = job_with_monitor

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
