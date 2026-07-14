"""BotRuntime — extracted from main.py for testability (Sprint 46T / audit M6).

Before this refactor, main.py was a ~1727-line god-file with the two most
critical money-protecting code paths (`fast_monitor_tick` and
`job_with_monitor`) defined as nested closures inside `main()` capturing
~15 local variables — entirely untestable in isolation. The audit's
exact complaint: "Ni uno de los 39 archivos de test cubre
`fast_monitor_tick` o `job_with_monitor`, los dos paths más críticos
para el dinero."

`BotRuntime` takes every dependency as a constructor arg (no hidden
captures), exposes the two critical paths as plain methods, and adds
`run()` as the long-lived orchestrator that previously lived in the
last ~300 lines of main(). The behavioral contract is identical: every
comment, every audit event, every print message preserved verbatim
from the original main() so an operator reading logs sees no change.

Why a class and not just module-level functions? Two reasons:

1. State continuity. `fast_monitor_tick` increments `_blind_tick_count`
   across calls (the A6 streak counter), and `job_with_monitor` shares
   the same `_pupdate_scheduler` instance. These are per-runtime state
   — they need a place to live, and the `__init__` makes that explicit
   and inspectable in tests.
2. Threading ergonomics. The fast-monitor daemon thread holds a
   `threading.Lock` + `threading.Event` that need to stay alive for
   the process lifetime. Bundling them in the class (and the
   `_fast_monitor_thread` reference for the `join(timeout=...)` style
   shutdown tests) keeps that contract visible.

The constructor is intentionally long (~20 args). The alternative was
a `BotRuntimeDeps` dataclass, which would be cleaner architecturally
but adds a layer of indirection with no testability win — tests just
pass the same mocks as keyword args. We can collapse to a dataclass
in a future sprint if a third caller appears; until then, explicitness
wins.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class BotRuntime:
    """Long-lived runtime that owns the two scheduled jobs.

    Lifecycle:
        BotRuntime(...).run()  # blocks until KeyboardInterrupt
        BotRuntime(...).run_once()  # single cycle, used by --once + tests
    """

    def __init__(
        self,
        *,
        # ---- Identity / mode ----
        config: dict,
        once: bool,
        # ---- Brokers ----
        broker_client: Any,
        alpaca_broker: Any,
        brokers_config: dict,
        # ---- Audit / eventing ----
        audit: Any,
        event_bus: Any,
        # ---- Repos / monitor ----
        position_repo: Any,
        position_monitor: Any,
        # ---- Equity / safety ----
        equity_tracker: Any,
        drawdown_kill_switch: Any,
        drawdown_state_path: str,
        mandate_gate: Any,
        kill_switch: Any,
        # ---- Workflow engine ----
        engine: Any,
        registry: dict,
        scheduler: Any,
        workflow_data: dict,
        analyze_market_step: Any,
        full_trading_assets: list,
        # ---- Trading config ----
        trading_cfg: dict,
        crypto_taker_fee_pct: float,
        min_order_usd: float,
        # ---- Helpers (closures from main.py) ----
        fee_pct_for_asset: Callable[[str], float],
        get_active_asset_classes: Callable,
        asset_class_for: Callable,
        is_trading_paused: Callable[[str], bool],
        fetch_prices_for_open_positions: Callable,
        should_alert_fast_monitor_blind: Callable,
        # ---- Original scheduler job (the analysis cycle) ----
        original_job: Callable,
        # ---- Paths ----
        equity_state_path: str,
        # ---- Misc ----
        max_auto_adjust_risk_multiplier: float = 2.0,
    ):
        # Identity
        self.config = config
        self.once = once
        # Brokers
        self.broker_client = broker_client
        self.alpaca_broker = alpaca_broker
        self.brokers_config = brokers_config
        # Audit / eventing
        self.audit = audit
        self.event_bus = event_bus
        # Repos / monitor
        self.position_repo = position_repo
        self.position_monitor = position_monitor
        # Equity / safety
        self.equity_tracker = equity_tracker
        self.drawdown_kill_switch = drawdown_kill_switch
        self.drawdown_state_path = drawdown_state_path
        self.mandate_gate = mandate_gate
        self.kill_switch = kill_switch
        # Workflow
        self.engine = engine
        self.registry = registry
        self.scheduler = scheduler
        self.workflow_data = workflow_data
        self.analyze_market_step = analyze_market_step
        self.full_trading_assets = full_trading_assets
        # Trading config
        self.trading_cfg = trading_cfg
        self.crypto_taker_fee_pct = crypto_taker_fee_pct
        self.min_order_usd = min_order_usd
        # Helpers
        self.fee_pct_for_asset = fee_pct_for_asset
        self.get_active_asset_classes = get_active_asset_classes
        self.asset_class_for = asset_class_for
        self.is_trading_paused = is_trading_paused
        self.fetch_prices_for_open_positions = fetch_prices_for_open_positions
        self.should_alert_fast_monitor_blind = should_alert_fast_monitor_blind
        # Original analysis job
        self.original_job = original_job
        # Paths
        self.equity_state_path = equity_state_path
        # Misc
        self.max_auto_adjust_risk_multiplier = max_auto_adjust_risk_multiplier

        # ---- Per-runtime state (was `nonlocal` in the old closures) ----
        # Sprint 46N (audit A6): counts consecutive fast_monitor_tick
        # runs where price fetching returned NOTHING while positions
        # were open ("flying blind"). Reset to 0 the moment we get any
        # price back, so a real blind streak starts counting from zero.
        self._blind_tick_count: int = 0
        # 3 ticks = 6 minutes at the 2-min default cadence. Single-tick
        # blips (rate limit, network hiccup) don't fire; longer outages
        # do.
        self._fast_monitor_blind_alert_threshold: int = 3
        # Sprint 34: per-position P&L update scheduler. Initialized
        # lazily on the first fast_monitor_tick so the config["notifications"]
        # block is available at construction time.
        self._pupdate_scheduler: Any = None
        # Sprint 46N (audit A5): fast_monitor_tick runs on its own
        # daemon thread with its own timer. Lock = non-blocking acquire
        # so a slow tick can never queue up overlapping runs against
        # the same broker clients/repo. Event = graceful shutdown
        # signal (used by tests).
        self._fast_monitor_lock = threading.Lock()
        self._fast_monitor_stop_event = threading.Event()
        self._fast_monitor_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Long-lived daemon: starts fast monitor thread + scheduler loop.

        Blocks until KeyboardInterrupt (which the scheduler catches at
        its top level). `--once` mode bypasses this and uses
        `run_once()` directly.
        """
        # Run once immediately at startup (both --once and daemon
        # modes), so open positions are protected right away instead of
        # waiting up to fast_monitor_interval_minutes for the first tick.
        try:
            self.fast_monitor_tick()
        except Exception as e:
            logger.warning(f"[Init] fast_monitor_tick inicial falló (continuando): {e}")

        if not self.once:
            self._start_fast_monitor_thread()
        else:
            logger.info(
                "[Init] Fast position monitor: modo --once, no se arma hilo en "
                "background (ya corrió una vez arriba)."
            )

        if self.once:
            self.run_once()
        else:
            self._run_daemon()

    def run_once(self) -> None:
        """Single analysis cycle, then return. Used by --once and tests."""
        logger.info("[System] Corriendo en modo UNA SOLA VEZ (--once)")
        self.audit.append("WORKFLOW_START", {"mode": "once"})
        self.scheduler.start(run_once_for_test=True)
        self.audit.append("WORKFLOW_END", {"mode": "once"})
        logger.info("\n=== Ciclo Único Completado ===")
        summary = self.audit.summary()
        logger.info(
            f"📒 Audit: {summary['total_events']} events, "
            f"{len(summary['by_type'])} types"
        )
        logger.info(f"   Audit file: {self.audit.path}")
        logger.info(
            f"📊 Posiciones: {self.position_repo.count_open()} abiertas, "
            f"${self.position_repo.total_realized_pnl_usd():.4f} realized PnL total"
        )

    def stop(self) -> None:
        """Signal the fast monitor thread to exit. Tests call this in
        `tearDown` so the daemon thread doesn't outlive the test
        process. Production code never calls this (KeyboardInterrupt
        terminates the process and the thread dies with it)."""
        self._fast_monitor_stop_event.set()

    # ------------------------------------------------------------------
    # Critical path 1: fast_monitor_tick
    # ------------------------------------------------------------------

    def fast_monitor_tick(self) -> None:
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
        daemon thread with its own timer (see
        `_start_fast_monitor_thread` below) instead of being
        registered on the same global `schedule` instance /
        single-threaded while-loop that also drives job_with_monitor
        — see that block's comment for the full rationale. It runs
        ONLY the position-protection half of what job_with_monitor
        used to do inline: SL/TP polling, smart profit-take, OCO
        reconciliation (for crypto positions using Sprint 46I's
        native_oco protection — see position_monitor.py), equity
        tracking, and per-position P&L update notifications. It never
        touches new-entry generation (StrategyAgent/HypothesisScorer/
        RiskManagerAgent) — that stays on the hourly cycle in
        job_with_monitor, which is heavier (yfinance fetches across
        the full asset universe, GP/hyperopt-adjacent work) and
        doesn't need sub-hour freshness the way protecting an
        already-open position does.
        """
        opens = self.position_repo.open()
        if not opens:
            # Nothing to protect right now — reset so a real blind
            # streak later starts counting from zero, not from
            # whatever it was before the book emptied out.
            self._blind_tick_count = 0
            return
        prices = self.fetch_prices_for_open_positions(
            self.position_repo,
            broker_client=self.broker_client,
            alpaca_broker=self.alpaca_broker,
            brokers_config=self.brokers_config,
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
            self._blind_tick_count += 1
            assets_at_risk = sorted({p.asset for p in opens})
            logger.warning(
                f"[FastMonitor] ⚠️ SIN PRECIOS este ciclo ({self._blind_tick_count} "
                f"consecutivos) — {len(opens)} posición(es) abierta(s) sin "
                f"protección SL/TP este ciclo: {assets_at_risk}"
            )
            if self.audit:
                self.audit.append("FAST_MONITOR_BLIND", {
                    "consecutive_blind_ticks": self._blind_tick_count,
                    "open_positions": len(opens),
                    "assets": assets_at_risk,
                })
            should_alert = self.should_alert_fast_monitor_blind(
                self._blind_tick_count, self._fast_monitor_blind_alert_threshold
            )
            if should_alert and self.event_bus is not None:
                self.event_bus.publish("SYSTEM_ERROR", {
                    "kind": "FAST_MONITOR_BLIND",
                    "consecutive_blind_ticks": self._blind_tick_count,
                    "open_positions": len(opens),
                    "assets": assets_at_risk,
                    "error": (
                        f"📉 fast_monitor_tick lleva {self._blind_tick_count} ciclos "
                        f"consecutivos SIN PRECIOS para {len(opens)} posición(es) "
                        f"abierta(s) ({', '.join(assets_at_risk)}). La protección "
                        f"SL/TP no puede evaluarse sin precio actual — verificar "
                        f"el proveedor de datos (yfinance/broker)."
                    ),
                })
            return
        # Got at least one price this tick — the blind streak (if any) is over.
        self._blind_tick_count = 0
        try:
            # 1a. SL/TP mechanical check (+ OCO reconciliation for
            # native_oco positions — see position_monitor.py).
            closed = self.position_monitor.check(prices)
            if closed:
                logger.info(f"[PositionMonitor] {len(closed)} posiciones cerradas por stops/TPs")

            # 1b. Sprint 18: smart profit-take on reversal signals.
            # Sprint 46R (audit M16): the audit's complaint was that
            # we fed `check_with_signals` signals up to 1h old against
            # fresh prices - "la reversion puede estar ya invalidada".
            # Now we read the last `smart_profit_take_max_signal_age_s`
            # seconds (default 5 min), and also pass that window to
            # `check_with_signals` itself as a defensive filter (in
            # case some other caller in the future passes a wider
            # list). The fast monitor runs every 2 min, so 5 min is
            # "the bot had a recent chance to re-evaluate this signal"
            # - old enough not to miss a fresh reversal, fresh enough
            # not to act on a stale one.
            try:
                _max_age = float(self.trading_cfg.get(
                    "smart_profit_take_max_signal_age_s", 300
                ))
                recent_hyps = self.audit.read_since(time.time() - _max_age)
                signals = [
                    h for h in recent_hyps
                    if h.get("event_type") == "HYPOTHESIS_GENERATED"
                ]
                if signals:
                    early_closed = self.position_monitor.check_with_signals(
                        current_prices=prices,
                        signals=signals,
                        signal_min_strength=float(self.trading_cfg.get(
                            "smart_profit_take_min_signal_strength", 0.6
                        )),
                        max_signal_age_s=_max_age,
                    )
                    if early_closed:
                        logger.info(
                            f"[PositionMonitor] {len(early_closed)} posiciones "
                            f"cerradas por SMART_PROFIT_TAKE (reversal)"
                        )
            except Exception as e2:
                logger.warning(f"[PositionMonitor] smart-profit-take falló: {e2}")

            # 1c. Refresh RiskAgent's current_prices view so position
            # replacement scoring uses live prices.
            try:
                rm = self.registry.get("RiskManagerAgent")
                if rm is not None:
                    rm.current_prices = prices
            except Exception:
                pass

            # Sprint 23: update equity tracker with current prices
            try:
                from src.safety.equity_tracker import format_equity_line
                snap = self.equity_tracker.update(prices)
                logger.info(f"  [Equity] {format_equity_line(snap, precision=4)}")
                # Sprint 24: persist to disk (crash-only)
                try:
                    from src.safety.equity_tracker import persist_tracker
                    persist_tracker(self.equity_tracker, self.equity_state_path)
                except Exception as _persist_err:
                    logger.warning(f"  [Equity] persist falló: {_persist_err}")
            except Exception as eqe:
                logger.warning(f"  [Equity] tracker update falló: {eqe}")

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
                if self._pupdate_scheduler is None:
                    _pupdate_interval = int(
                        self.config.get("notifications", {}).get(
                            "position_update_minutes", 60
                        )
                    )
                    _pupdate_min_pnl = float(
                        self.config.get("notifications", {}).get(
                            "position_update_min_pnl_usd", 0.0
                        )
                    )
                    self._pupdate_scheduler = PositionUpdateScheduler(
                        position_repo=self.position_repo,
                        event_bus=self.event_bus,
                        interval_minutes=_pupdate_interval,
                        min_pnl_usd=_pupdate_min_pnl,
                    )
                    logger.info(
                        f"  [PosUpdate] scheduler armed: "
                        f"interval={_pupdate_interval}m, "
                        f"min_pnl=${_pupdate_min_pnl:.2f}"
                    )
                n_emitted = self._pupdate_scheduler.tick(prices)
                if n_emitted:
                    logger.info(f"  [PosUpdate] emitted {n_emitted} update(s)")
                # Drop any closed positions from the cadence map
                open_ids = {p.position_id for p in self.position_repo.open()}
                for pid in list(self._pupdate_scheduler._last_update.keys()):
                    if pid not in open_ids:
                        self._pupdate_scheduler.clear_position(pid)
            except Exception as _pue:
                logger.warning(f"  [PosUpdate] scheduler falló: {_pue}")
        except Exception as e:
            logger.warning(f"[PositionMonitor] check falló: {e}")

        # Sprint 46R audit M11.3: heartbeat update for the enhanced
        # /api/health endpoint. Pre-46R the healthcheck was just a
        # liveness ping (the FastAPI server was up = healthy), which
        # is what the audit called "pgrep — pasa aunque el bot esté
        # colgado". Now the endpoint reads `APP_STATE["last_fast_monitor_at"]`
        # and returns 503 if it's older than 2x the configured fast
        # interval. Set it on EVERY tick (even when there are 0 open
        # positions and the body of the tick is a no-op) so a stuck
        # fast_monitor_tick shows up as a stuck healthcheck.
        try:
            from src.api.server import APP_STATE as _api_app_state
            _api_app_state["last_fast_monitor_at"] = time.time()
        except Exception:
            # The API server may not be started yet in --once mode
            # or in tests. Best-effort only — no audit, no log spam.
            pass

        # Sprint 46R audit M11.4: dead-man's switch. Best-effort GET
        # against the configured HEALTHCHECKS_PING_URL (healthchecks.io
        # in production). Runs on the 2-min fast tick so an OOB
        # service can detect a dead bot within a couple of minutes —
        # much faster than the 1h analysis cycle. The ping never
        # raises (see ping_dead_mans_switch's docstring); a failure
        # is logged + reflected in the next /api/health body.
        try:
            from src.observability.dead_mans_switch import ping_dead_mans_switch
            ping_dead_mans_switch()
        except Exception as _dms_err:
            # The import could fail in --once mode / older tests where
            # the observability package isn't on sys.path yet. Don't
            # crash the cycle.
            logger.warning(f"[DeadMansSwitch] ping skipped (import failed): {_dms_err}")

    # ------------------------------------------------------------------
    # Critical path 2: job_with_monitor
    # ------------------------------------------------------------------

    def job_with_monitor(self) -> None:
        """Hourly analysis cycle: gates + (optional) workflow run.

        Sprint 46I: position protection (stop-loss/take-profit,
        smart profit-take, OCO reconciliation, equity tracking) no
        longer lives here — it runs independently on a faster
        cadence via `fast_monitor_tick`. This method now only does
        the drawdown check + capital routing + manual pause gates,
        then the full hourly analysis cycle (`original_job`). It
        still needs its OWN price snapshot for the drawdown equity
        calc below — fetched independently from fast_monitor_tick's
        (different cadence, simplest to keep them decoupled rather
        than share mutable state between two differently-timed
        scheduled jobs).
        """
        # Sprint 43 H3 (fixed for real in 46D; equity source fixed in
        # 46N audit A1): drawdown kill switch check. If the account
        # has dropped more than the configured threshold from its
        # peak equity, the bot is in "revenge trading" territory —
        # it should stop opening NEW positions until the cooldown
        # elapses.
        #
        # Sprint 46D fix: previously this did `return` immediately on
        # trigger, which — despite the comment directly above it
        # claiming otherwise — skipped the position-monitor block
        # entirely (it's later in the same function). That meant a
        # drawdown pause would ALSO freeze SL/TP protection on
        # already-open positions, the opposite of what you want
        # during a drawdown. Now we only set a flag that skips step
        # 2 (the normal workflow / new entries) at the very end;
        # step 1 (monitor) always runs below regardless.
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
            current_equity = self.equity_tracker.latest().total_equity
            dd_state = self.drawdown_kill_switch.update(current_equity)
            # Sprint 46N (audit A1): persist peak_equity/triggered/
            # triggered_at after every update so a bot restart can't
            # silently forget an active kill switch or reset the peak
            # back to 0 — see DrawdownKillSwitch.persist()'s docstring.
            try:
                self.drawdown_kill_switch.persist(self.drawdown_state_path)
            except Exception as _dd_persist_err:
                logger.warning(
                    f"[DrawdownKill] persist falló (continuando): {_dd_persist_err}"
                )
            if dd_state.triggered:
                dd_triggered = True
                if self.audit:
                    self.audit.append("BOT_DRAWDOWN_KILL_ACTIVE", {
                        "drawdown_pct": round(dd_state.drawdown_pct, 3),
                        "peak_equity": dd_state.peak_equity,
                        "current_equity": current_equity,
                        "cooldown_remaining_hours": round(dd_state.cooldown_remaining_hours, 2),
                    })
                if self.event_bus:
                    self.event_bus.publish("SYSTEM_ERROR", {
                        "kind": "DRAWDOWN_KILL_ACTIVE",
                        "drawdown_pct": round(dd_state.drawdown_pct, 3),
                        "error": (f"🛑 Drawdown kill switch ACTIVO: "
                                  f"{dd_state.drawdown_pct:.2f}% desde peak. "
                                  f"Bot NO abre nuevas posiciones por "
                                  f"{dd_state.cooldown_remaining_hours:.1f}h."),
                    })
                # Skip step 2 (new entries) this cycle — step 1
                # (monitor, right below) still runs so SL/TP can
                # still close.
        except Exception as e:
            logger.warning(f"[DrawdownKill] check failed (continuing): {e}")
            # Sprint 46D fix: a crashing safety check must be as loud
            # as a triggered one — previously this was print-only, so
            # a broken check and a passing check looked identical in
            # the logs/dashboard. Now it's visible in the audit trail
            # too.
            if self.audit:
                self.audit.append("DRAWDOWN_CHECK_ERROR", {"error": str(e)[:300]})
            if self.event_bus:
                try:
                    self.event_bus.publish("SYSTEM_ERROR", {
                        "kind": "DRAWDOWN_CHECK_ERROR",
                        "error": f"⚠️ Drawdown kill-switch check falló (bot sigue operando): {e}",
                    })
                except Exception:
                    pass

        # Sprint 46S (audit B4): reconcile the equity tracker's
        # expected balance against the broker's real live balance
        # once per hourly cycle, so a manual deposit/withdrawal to
        # binance.us doesn't masquerade as trading P&L.
        # `EquityTracker.reconcile_external_balance` (added in Sprint
        # 46R, commit ef3b83b) already has the math; it was never
        # actually called from anywhere until this — this wiring is
        # that missing piece. Scoped to crypto only, since
        # equity_tracker.starting_balance was itself seeded from
        # broker_client.get_usdt_balance() above (crypto-only balance;
        # Alpaca isn't configured on this account today — see
        # asset_class_for). Best-effort: skip silently (not a
        # SYSTEM_ERROR) if the broker is unreachable this cycle — the
        # next hourly cycle will just try again, and reconciliation
        # drifting by one cycle is harmless compared to a false
        # SYSTEM_ERROR every time yfinance or the broker has a
        # transient hiccup.
        #
        # Sprint 62: skip reconciliation in PAPER mode. The equity
        # tracker's starting_balance is `paper.starting_balance_usd`
        # (virtual, e.g. $1,000), not the broker's real balance
        # ($22.08). Reconciling against the broker would create a
        # fake "deposit/withdrawal" delta every cycle, corrupting the
        # paper equity curve. Only real-mode (mandate.enabled=true)
        # sessions reconcile.
        _is_paper_mode = not bool(
            (self.config.get("mandate") or {}).get("enabled", False)
        )
        # Sprint 62: paper mode skips the broker reconciliation but
        # does NOT short-circuit the function (other post-cycle work
        # follows). The equity tracker's starting balance in paper
        # mode is `paper.starting_balance_usd` (virtual), so the
        # broker's real balance is irrelevant — reconciling would
        # treat every delta as a fake deposit/withdrawal.
        if not _is_paper_mode:
            try:
                if self.broker_client is not None:
                    _crypto_balance = self.broker_client.get_usdt_balance()
                    if _crypto_balance is not None and _crypto_balance >= 0:
                        _crypto_open_notional = sum(
                            p.notional_usd for p in self.position_repo.open()
                            if self.asset_class_for(p.asset, self.brokers_config) == "crypto"
                        )
                        _recon = self.equity_tracker.reconcile_external_balance(
                            broker_balance=_crypto_balance,
                            current_open_position_notional=_crypto_open_notional,
                        )
                        if _recon["deposit_usd"] or _recon["withdrawal_usd"]:
                            logger.info(
                                f"[EquityTracker] reconcile: "
                                f"deposit=${_recon['deposit_usd']:.4f} "
                                f"withdrawal=${_recon['withdrawal_usd']:.4f} "
                                f"new_starting_balance=${_recon['new_starting_balance']:.4f}"
                            )
                            try:
                                from src.safety.equity_tracker import persist_tracker
                                persist_tracker(self.equity_tracker, self.equity_state_path)
                            except Exception as _persist_err:
                                logger.warning(
                                    f"[EquityTracker] reconcile persist falló: {_persist_err}"
                                )
            except Exception as _recon_err:
                logger.warning(
                    f"[EquityTracker] reconcile_external_balance falló (continuando): {_recon_err}"
                )

        # Sprint 46G: capital-aware asset routing. Re-check broker
        # balances every cycle and narrow the analyze_market step's
        # asset list to only the classes that currently have money —
        # crypto-only if just binance is funded, equity-only if just
        # Alpaca is funded, both if both are (even at the $10 minimum).
        # See get_active_asset_classes' docstring for the fail-open
        # rationale.
        capital_blocked = False
        try:
            if self.analyze_market_step is not None and self.full_trading_assets:
                _active_classes = self.get_active_asset_classes(
                    self.broker_client, self.alpaca_broker, min_usd=self.min_order_usd
                )
                _filtered_assets = [
                    a for a in self.full_trading_assets
                    if self.asset_class_for(a, self.brokers_config) in _active_classes
                    or self.asset_class_for(a, self.brokers_config) == "unknown"
                ]
                if not _filtered_assets:
                    capital_blocked = True
                    if self.audit:
                        self.audit.append("CAPITAL_ROUTING_BLOCKED", {
                            "active_classes": sorted(_active_classes),
                            "full_assets": self.full_trading_assets,
                        })
                    logger.warning(
                        "[CapitalRouting] ⛔ Ningún broker tiene balance suficiente "
                        f"(${self.min_order_usd:.2f} mínimo) — ciclo de nuevas entradas SALTADO."
                    )
                else:
                    if set(_filtered_assets) != set(self.full_trading_assets):
                        if self.audit:
                            self.audit.append("CAPITAL_ROUTING_APPLIED", {
                                "active_classes": sorted(_active_classes),
                                "assets_used": _filtered_assets,
                                "assets_full": self.full_trading_assets,
                            })
                        logger.info(
                            f"[CapitalRouting] Universo de assets ajustado a "
                            f"{_filtered_assets} (clases activas: {sorted(_active_classes)})"
                        )
                    self.analyze_market_step["inputs"]["assets"] = _filtered_assets
        except Exception as e:
            logger.warning(f"[CapitalRouting] check falló (continuando sin filtrar): {e}")

        # Sprint 46H: manual Stop/Start toggle from the dashboard.
        # Checked every cycle (see is_trading_paused's docstring) so
        # a dashboard click takes effect on the NEXT cycle — no
        # restart needed. Only gates step 2 (new entries) below, same
        # as the drawdown/capital gates above; step 1 (monitor)
        # already ran.
        manual_paused = False
        try:
            manual_paused = self.is_trading_paused(
                self.config.get("mandate", {}).get("audit_log_dir", "audit")
            )
        except Exception as e:
            logger.warning(f"[TradingPause] check falló (continuando sin pausar): {e}")

        # 2. Workflow normal — skipped while the drawdown kill switch
        # is active (dd_triggered), while no broker has enough capital
        # to trade (capital_blocked), OR while manually paused from
        # the dashboard (manual_paused). Step 1 (monitor) already ran
        # unconditionally, so SL/TP protection on existing positions
        # is never paused by any of these three gates.
        if not dd_triggered and not capital_blocked and not manual_paused:
            self.original_job()
        elif dd_triggered:
            logger.info(
                "[DrawdownKill] Ciclo de nuevas entradas SALTADO (cooldown activo). "
                "Monitor de SL/TP sigue corriendo normalmente."
            )
        elif capital_blocked:
            logger.info(
                "[CapitalRouting] Ciclo de nuevas entradas SALTADO (sin capital disponible). "
                "Monitor de SL/TP sigue corriendo normalmente."
            )
        else:
            logger.info(
                "[TradingPause] Ciclo de nuevas entradas SALTADO (pausado manualmente "
                "desde el dashboard). Monitor de SL/TP sigue corriendo normalmente."
            )

        # Sprint 46R audit M11.3: heartbeat for the enhanced
        # /api/health endpoint. Update on every cycle (gates-skipped
        # or not) so the healthcheck can distinguish "scheduler
        # thread is alive" from "scheduler thread is alive AND the
        # bot is making forward progress". The endpoint returns 503
        # if this is older than 2x the configured
        # run_interval_hours — Docker will then restart the
        # container instead of leaving a zombie alive forever.
        try:
            from src.api.server import APP_STATE as _api_app_state
            _api_app_state["last_analysis_cycle_at"] = time.time()
        except Exception:
            # API may not be started yet in --once mode / tests.
            # Best-effort only — no log spam, no audit.
            pass

    # ------------------------------------------------------------------
    # Internal: fast monitor daemon thread
    # ------------------------------------------------------------------

    def _fast_monitor_loop(self) -> None:
        """Body of the dedicated fast-monitor daemon thread (Sprint 46N audit A5).

        Was previously a closure inside main() capturing `once`,
        `fast_monitor_tick`, and the lock/event. Lives in the class
        now so the threading contract is visible at the type level
        and testable with `stop()` + `join(timeout=...)`.
        """
        _fast_monitor_minutes = float(
            self.config.get("schedule", {}).get("fast_monitor_interval_minutes", 2)
        )
        interval_seconds = max(_fast_monitor_minutes * 60.0, 1.0)
        while not self._fast_monitor_stop_event.wait(interval_seconds):
            # Non-blocking acquire: if a previous tick is still
            # running (slower than the configured interval — e.g. a
            # broker/exchange hiccup), skip this tick rather than
            # queuing up overlapping runs against the same broker
            # clients/repo.
            if not self._fast_monitor_lock.acquire(blocking=False):
                logger.warning(
                    "[FastMonitor] tick anterior aún en curso; salteando este tick."
                )
                continue
            try:
                self.fast_monitor_tick()
            except Exception as e:
                logger.warning(f"[FastMonitor] tick falló (continuando): {e}")
            finally:
                self._fast_monitor_lock.release()

    def _start_fast_monitor_thread(self) -> None:
        """Sprint 46N (audit A5): decouple from the global scheduler.

        Before this, both jobs ran sequentially on a single thread
        driven by `EpochScheduler.start()`'s `while True:
        schedule.run_pending()` loop. A slow hourly cycle (15
        yfinance downloads with retries, plus per-hypothesis
        portfolio-risk-gate yfinance calls, which could together
        take 10-20 minutes under Yahoo rate limiting) could starve
        fast_monitor_tick for that entire duration, suspending
        SL/TP protection on open positions exactly when the bot is
        busiest. This thread is fully independent: its own
        sleep/tick timer, never blocked on (or blocking)
        job_with_monitor.

        Thread-safety (verified, not just assumed): PositionRepository
        guards every read/write with a threading.RLock (Sprint 46 C8)
        and close_position() is idempotent — it checks is_open
        inside the lock and returns None if already closed — so a
        race between this thread's SL/TP close and
        job_with_monitor's replacement-close on the same position
        resolves safely by construction. AuditLedger.append()
        serializes via fcntl.flock per write. EventBus.publish()
        only iterates `subscribers`, which is mutated only at
        startup (subscribe()), so concurrent publish() calls are
        safe. EquityTracker.history is a deque with a single
        writer (this thread) and a single reader (job_with_monitor)
        — safe under CPython's GIL without an extra lock.
        """
        self._fast_monitor_thread = threading.Thread(
            target=self._fast_monitor_loop,
            name="fast-monitor-thread",
            daemon=True,
        )
        self._fast_monitor_thread.start()
        _fast_monitor_minutes = float(
            self.config.get("schedule", {}).get("fast_monitor_interval_minutes", 2)
        )
        logger.info(
            f"[Init] Fast position monitor armado en su propio hilo: cada "
            f"{_fast_monitor_minutes} min (independiente del ciclo de análisis "
            f"de {self.config.get('schedule', {}).get('run_interval_hours', 1)}h y "
            f"del scheduler de un solo hilo que lo corre — Sprint 46N audit A5)"
        )

    def _run_daemon(self) -> None:
        """Long-lived scheduler loop. Blocks until KeyboardInterrupt."""
        logger.info("[System] Iniciando Demonio (Modo Épocas)...")
        self.audit.append("WORKFLOW_START", {"mode": "daemon"})
        self.scheduler.start(run_once_for_test=False)
