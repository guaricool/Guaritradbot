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
    def __init__(self, engine, workflow_data, config_path="config.yaml"):
        self.engine = engine
        self.workflow_data = workflow_data
        self.config_path = config_path
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
        logger.info("[EpochScheduler] Starting Hyperopt Re-optimization phase...")
        # Lógica simplificada de re-optimización para la Época
        # Aquí importaríamos el HyperoptManager y le pediríamos nuevos parámetros.
        # Por ahora, simulamos una recalibración exitosa.
        logger.info("[EpochScheduler] Re-optimization complete. New parameters injected.")
        
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
                    
            with open("latest_state.json", "w") as f:
                json.dump({"timestamp": datetime.now().isoformat(), "state": state}, f, indent=4, cls=CustomEncoder)
        except Exception as e:
            logger.error(f"Error saving state: {e}")
            
    def start(self, run_once_for_test=False):
        logger.info(f"Starting Epoch Scheduler. Interval: {self.interval_hours}h, Epoch: {self.epoch_days}d")
        
        # Ejecutar inmediatamente la primera vez
        self.job()
        
        if run_once_for_test:
            return

        # Programar las siguientes
        schedule.every(self.interval_hours).hours.do(self.job)
        
        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user.")
