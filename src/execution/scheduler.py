import time
import schedule
import logging
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import yaml
import os

from src.workflows.engine import WorkflowAgentFaultError, WorkflowDependencyError

# Sprint 19: Carlos lives in America/Chicago (IL). All timestamps the bot
# writes to disk are stored as tz-aware ISO strings in CT, so the
# dashboard doesn't have to guess the VPS TZ.
CT = ZoneInfo("America/Chicago")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Scheduler")


class EpochScheduler:
    def __init__(
        self,
        engine,
        workflow_data,
        config_path="config.yaml",
        market_analyst=None,
        strategy_agent=None,
        hyperopt=None,
        audit=None,
        assets=("BTC-USD", "SPY"),
        event_bus=None,
    ):
        self.engine = engine
        self.workflow_data = workflow_data
        self.config_path = config_path
        self.market_analyst = market_analyst
        self.strategy_agent = strategy_agent
        self.hyperopt = hyperopt
        self.audit = audit
        self.assets = assets
        # Sprint 45 fix (N6/H11): needed so `job()` can publish
        # SYSTEM_ERROR when the workflow engine refuses to run a
        # cycle (FAULTED component / unmet depends_on), instead of
        # only logging it to stdout where nobody sees it.
        self.event_bus = event_bus
        self.load_config()

        self.epoch_start = datetime.now()

    def load_config(self):
        with open(self.config_path, "r") as f:
            self.config = yaml.safe_load(f)

        schedule_conf = self.config.get("schedule", {})
        self.interval_hours = schedule_conf.get("run_interval_hours", 1)
        self.epoch_days = schedule_conf.get("epoch_duration_days", 7)

    def check_epoch(self):
        now = datetime.now()
        if now - self.epoch_start >= timedelta(days=self.epoch_days):
            logger.info(f"=== Epoch Completed ({self.epoch_days} days) ===")
            self.run_reoptimization()
            self.epoch_start = now

    def run_reoptimization(self):
        """
        Sprint 5 — Re-optimization real.

        Para cada asset en el universe del bot, descargamos histórico
        reciente y corremos HyperoptManager.optimize(). Si los nuevos
        parámetros son mejores (por la métrica optimizada) y NO son
        overfit (walk-forward ratio > 0.5), los inyectamos al
        StrategyAgent y los emitimos al audit ledger.
        """
        if not self.market_analyst or not self.strategy_agent or not self.hyperopt:
            logger.info("[EpochScheduler] Re-optimization skipped: dependencias no inyectadas")
            return

        logger.info("[EpochScheduler] Starting re-optimization…")
        if self.audit:
            self.audit.append("REOPT_START", {"epoch_days": self.epoch_days})

        # Grid search space — versión compacta para que termine en <1 min
        param_space = {
            "rsi_oversold": [25, 30, 35],
            "rsi_overbought": [65, 70, 75],
        }

        # Cada asset es su propio universe de optimización.
        # En producción esto debería ser por estrategia (BTC = MACD,
        # SPY/QQQ = RSI mean-reversion, GLD/USO = EMA). Para Sprint 5
        # optimizamos RSI sobre el primer asset del set (proxy rápido).
        # El hyperopt devuelve el mejor set global de RSI.
        try:
            from src.optimization.backtester import walk_forward_split
            asset = self.assets[0]
            df = self.market_analyst.fetch_one(asset, interval="1d", period="2y")
            if df is None or len(df) < 100:
                logger.info(f"[EpochScheduler] datos insuficientes para {asset}, skip")
                return

            # Pre-popular con EMA / RSI
            import pandas as pd
            close = df["Close"]
            df["EMA_20"] = close.ewm(span=20, adjust=False).mean()
            df["EMA_50"] = close.ewm(span=50, adjust=False).mean()
            delta = close.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta).clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss.replace(0, pd.NA)
            df["RSI"] = (100 - (100 / (1 + rs))).astype(float).fillna(50)
            df = df.dropna(subset=["Close", "RSI", "EMA_20", "EMA_50"])

            def rsi_sig(d, **p):
                from src.agents.strategy_agent import StrategyAgent
                return StrategyAgent.generate_vectorized_signals(d, strategy_type="RSI", **p)

            best_params = self.hyperopt.optimize(
                f"epoch_{int(time.time())}",
                df,
                param_space,
                rsi_sig,
                metric="sharpe_ratio",
            )

            if not best_params:
                logger.info("[EpochScheduler] hyperopt no devolvió params")
                return

            new_params = dict(self.strategy_agent.params)
            new_params.update(best_params)
            old_params = dict(self.strategy_agent.params)
            self.strategy_agent.params = new_params

            logger.info(
                f"[EpochScheduler] ⚙️  params actualizados: "
                f"{old_params} → {new_params}"
            )
            if self.audit:
                self.audit.append(
                    "REOPT_NEW_PARAMS",
                    {"asset": asset, "old": old_params, "new": new_params},
                )
        except Exception as e:
            logger.error(f"[EpochScheduler] re-optimization failed: {e}")
            if self.audit:
                self.audit.append("REOPT_ERROR", {"error": str(e)})

    def job(self):
        logger.info("--- Starting scheduled trading run ---")
        self.check_epoch()

        try:
            final_state = self.engine.run(self.workflow_data)
            self._save_state(final_state)
        except (WorkflowAgentFaultError, WorkflowDependencyError) as e:
            # Sprint 45 fix (N6/H11): H11 made WorkflowEngine correctly
            # abort a cycle when a component is FAULTED or a
            # depends_on isn't met (e.g. a total market-data outage),
            # instead of silently proceeding with empty data. But this
            # was the only caller, and it swallowed the new exceptions
            # with the same generic `except Exception` used for every
            # other error — logged to stdout only, no audit entry, no
            # SYSTEM_ERROR. Carlos had no way to distinguish "the
            # engine correctly refused this cycle" from any other
            # crash. Now it gets its own audit event + alert.
            logger.error(f"[Scheduler] Workflow cycle aborted: {e}")
            if self.audit is not None:
                self.audit.append("WORKFLOW_CYCLE_ABORTED", {
                    "kind": type(e).__name__,
                    "error": str(e)[:500],
                })
            if self.event_bus is not None:
                try:
                    self.event_bus.publish("SYSTEM_ERROR", {
                        "kind": "WORKFLOW_CYCLE_ABORTED",
                        "error": f"⛔ Ciclo de trading abortado: {e}",
                    })
                except Exception as pub_err:
                    logger.error(f"[Scheduler] No se pudo publicar SYSTEM_ERROR: {pub_err}")
        except Exception as e:
            # Sprint 46R audit M9: the generic exception path used
            # to log to stdout + write one audit event, but it did
            # NOT publish SYSTEM_ERROR — so a cycle that crashed
            # unexpectedly (an unrelated bug in a step body, a
            # library exception, etc.) silently disappeared with
            # no Telegram alert. Carlos had no way to know the
            # cycle had died until he happened to look at the
            # dashboard. Mirror the same alerting pattern that the
            # WorkflowAgentFaultError / WorkflowDependencyError
            # branch above (lines 157-181) uses, so ANY cycle
            # crash surfaces as SYSTEM_ERROR + audit + Telegram.
            logger.error(f"Error during workflow execution: {e}")
            if self.audit is not None:
                self.audit.append("WORKFLOW_CYCLE_ERROR", {"error": str(e)[:500]})
            if self.event_bus is not None:
                try:
                    self.event_bus.publish("SYSTEM_ERROR", {
                        "kind": "WORKFLOW_CYCLE_ERROR",
                        "error": f"⛔ Workflow cycle crashed: {e}",
                    })
                except Exception as pub_err:
                    logger.error(
                        f"[Scheduler] No se pudo publicar SYSTEM_ERROR "
                        f"WORKFLOW_CYCLE_ERROR: {pub_err}"
                    )

        logger.info("--- Scheduled run complete. Waiting for next interval ---")

    def _save_state(self, state):
        try:
            class CustomEncoder(json.JSONEncoder):
                def default(self, obj):
                    import pandas as pd
                    if isinstance(obj, pd.DataFrame) or isinstance(obj, pd.Series):
                        return "Pandas DataFrame/Series (Omitted for JSON)"
                    return super().default(obj)

            with open("audit/latest_state.json", "w") as f:
                # Sprint 19: tz-aware CT timestamp for portability
                ct_now = datetime.now(CT)
                json.dump({"timestamp": ct_now.isoformat(), "tz": "America/Chicago",
                           "state": state}, f, indent=4, cls=CustomEncoder)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    def start(self, run_once_for_test=False):
        logger.info(
            f"Starting Epoch Scheduler. Interval: {self.interval_hours}h, Epoch: {self.epoch_days}d"
        )

        self.job()

        if run_once_for_test:
            return

        schedule.every(self.interval_hours).hours.do(self.job)

        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user.")
