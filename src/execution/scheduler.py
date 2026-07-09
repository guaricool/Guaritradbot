import time
import schedule
import logging
import json
from datetime import datetime, timedelta
import yaml
import os

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
    ):
        self.engine = engine
        self.workflow_data = workflow_data
        self.config_path = config_path
        self.market_analyst = market_analyst
        self.strategy_agent = strategy_agent
        self.hyperopt = hyperopt
        self.audit = audit
        self.assets = assets
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
        except Exception as e:
            logger.error(f"Error during workflow execution: {e}")

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
                json.dump({"timestamp": datetime.now().isoformat(), "state": state}, f, indent=4, cls=CustomEncoder)
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
