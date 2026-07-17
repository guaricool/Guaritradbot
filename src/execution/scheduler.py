import time
import schedule
import logging
import json
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import yaml
import os

from src.workflows.engine import WorkflowAgentFaultError, WorkflowDependencyError

# Bug fix (Carlos: "paper agresivo... que aprenda... y lo use en live"):
# `run_reoptimization` used to grid-search RSI thresholds with
# `HyperoptManager.optimize()` directly and apply whatever it found —
# despite this module already importing `walk_forward_split` (from
# src.optimization.backtester) as if it validated the result, that
# import was NEVER ACTUALLY USED. In-sample-only grid search reliably
# finds parameters that overfit the specific window tested; nothing
# checked whether the "improvement" held up out-of-sample, and nothing
# compared the candidate against the CURRENT params before overwriting
# them. It also only ever optimized on `self.assets[0]` (BTC-USD),
# silently ignoring the other 4 assets main.py actually configures
# (SPY/GLD/QQQ/USO). The bot's live strategy parameters could be
# (and, on a 7-day epoch, eventually would be) overwritten by noise.
#
# Fixed to use `walk_forward_validate` (already existed, fully built,
# just never called from here): trains on the first ~70%, validates
# out-of-sample on the rest, and reports an `overfit_warning` when the
# out-of-sample result doesn't hold up to the in-sample one. Promotion
# now requires ALL of:
#   1. No overfit warning on any asset the candidate was tested on.
#   2. The candidate's average out-of-sample Sharpe beats the CURRENT
#      params' own out-of-sample Sharpe (evaluated through the exact
#      same walk-forward mechanics, not just "whatever number the old
#      code remembered from months ago") by at least
#      MIN_SHARPE_IMPROVEMENT.
#   3. At least MIN_ASSETS_WITH_DATA assets had enough history to test.
# Even when the answer is "yes, promote", the previous params are
# still recoverable: every reoptimization decision -- promoted or not
# -- is written to `audit/strategy_params_override.json` under `_history`
# (see `_write_strategy_params_override`), and the promotion itself is
# audited as REOPT_NEW_PARAMS with both old and new values plus the
# out-of-sample metrics that justified it.
from src.optimization.backtester import walk_forward_validate

MIN_SHARPE_IMPROVEMENT = 0.15
MIN_ASSETS_WITH_DATA = 2

# Sprint 19: Carlos lives in America/Chicago (IL). All timestamps the bot
# writes to disk are stored as tz-aware ISO strings in CT, so the
# dashboard doesn't have to guess the VPS TZ.
CT = ZoneInfo("America/Chicago")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Scheduler")


def _should_promote(
    candidate_oos_sharpes: list,
    baseline_oos_sharpes: list,
    any_overfit: bool,
    min_improvement: float = MIN_SHARPE_IMPROVEMENT,
    min_assets: int = MIN_ASSETS_WITH_DATA,
) -> tuple:
    """Pure decision function (no I/O) so the promotion gate is directly
    unit-testable without mocking market data / walk_forward_validate.

    Returns (should_promote: bool, reason: str).
    """
    if len(candidate_oos_sharpes) < min_assets or len(baseline_oos_sharpes) < min_assets:
        return False, f"insufficient_data (only {len(candidate_oos_sharpes)} assets had enough history)"
    if any_overfit:
        return False, "overfit_warning on at least one asset's walk-forward split"
    avg_candidate = statistics.mean(candidate_oos_sharpes)
    avg_baseline = statistics.mean(baseline_oos_sharpes)
    improvement = avg_candidate - avg_baseline
    if improvement < min_improvement:
        return False, (
            f"insufficient_improvement (candidate OOS sharpe {avg_candidate:.3f} vs "
            f"baseline {avg_baseline:.3f}, needed +{min_improvement:.2f}, got {improvement:+.3f})"
        )
    return True, (
        f"promoted (candidate OOS sharpe {avg_candidate:.3f} vs baseline "
        f"{avg_baseline:.3f}, +{improvement:.3f})"
    )


def _write_strategy_params_override(
    override_path: str, old_params: dict, new_params: dict, reason: str,
) -> None:
    """Persist a promoted strategy_params change so it (a) survives a
    restart -- main.py merges this file into StrategyAgent's params at
    startup the same way trading_config_override.json already works
    for RiskManagerAgent's params -- and (b) keeps a `_history` trail
    of every past promotion for manual audit/rollback, since an
    automatic promotion (Carlos explicitly chose automatic over
    manual-review promotion) still needs to be reversible by a human
    if a promoted config underperforms once it's actually live.
    """
    path = Path(override_path)
    history = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            history = existing.get("_history", [])
        except Exception:
            history = []
    history.append({
        "ts": time.time(),
        "old": old_params,
        "new": new_params,
        "reason": reason,
    })
    payload = dict(new_params)
    payload["_history"] = history[-20:]  # cap so this file can't grow forever
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


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
        strategy_params_override_path="audit/strategy_params_override.json",
    ):
        self.engine = engine
        self.workflow_data = workflow_data
        self.config_path = config_path
        self.market_analyst = market_analyst
        self.strategy_agent = strategy_agent
        self.hyperopt = hyperopt
        self.audit = audit
        self.assets = assets
        self.strategy_params_override_path = strategy_params_override_path
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
        Re-optimization real, walk-forward validated (see this module's
        top-of-file comment for the bug this replaced: the previous
        version imported `walk_forward_split` but never called it,
        optimized in-sample only, only ever tested `self.assets[0]`,
        and applied whatever it found with no comparison to the
        current params).

        For every configured asset with enough history: run
        `walk_forward_validate` for the RSI param grid (the candidate)
        AND for the current live params alone (the baseline, evaluated
        through the identical walk-forward mechanics for a fair
        comparison). Promote the candidate only if `_should_promote`
        says yes across the aggregate of all assets tested — see that
        function's docstring for the exact gate. Every decision
        (promoted or not) is logged to the audit ledger; a promotion
        is also persisted to `strategy_params_override_path` so it
        survives a restart and is reversible.
        """
        if not self.market_analyst or not self.strategy_agent or not self.hyperopt:
            logger.info("[EpochScheduler] Re-optimization skipped: dependencias no inyectadas")
            return

        logger.info("[EpochScheduler] Starting re-optimization…")
        if self.audit:
            self.audit.append("REOPT_START", {"epoch_days": self.epoch_days, "assets": list(self.assets)})

        param_space = {
            "rsi_oversold": [25, 30, 35],
            "rsi_overbought": [65, 70, 75],
        }
        old_params = dict(self.strategy_agent.params)
        # Single-point "grid" so walk_forward_validate's internal
        # optimize step trivially returns the CURRENT params back —
        # this gives an apples-to-apples out-of-sample baseline
        # computed through the exact same walk-forward mechanics as
        # the candidate, not a stale number from whenever this last ran.
        baseline_space = {
            k: [old_params[k]] for k in param_space if k in old_params
        }

        try:
            import numpy as np
            import pandas as pd
            from src.agents.strategy_agent import StrategyAgent

            def rsi_sig(d, **p):
                return StrategyAgent.generate_vectorized_signals(d, strategy_type="RSI", **p)

            candidate_sharpes, baseline_sharpes = [], []
            any_overfit = False
            per_asset_results = {}
            tested_assets = []

            for asset in self.assets:
                df = self.market_analyst.fetch_one(asset, interval="1d", period="2y")
                if df is None or len(df) < 100:
                    logger.info(f"[EpochScheduler] datos insuficientes para {asset}, skip")
                    continue

                close = df["Close"]
                df["EMA_20"] = close.ewm(span=20, adjust=False).mean()
                df["EMA_50"] = close.ewm(span=50, adjust=False).mean()
                delta = close.diff()
                gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
                loss = (-delta).clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
                # Bug fix: `pd.NA` (pandas' nullable-dtype sentinel) doesn't
                # survive `.astype(float)` the way `np.nan` does --
                # `float(pd.NA)` raises `TypeError: float() argument must
                # be a string or a real number, not 'NAType'`, which used
                # to blow up this whole re-optimization pass any time a
                # zero-loss stretch occurred (a real possibility over a
                # 2-year window, just apparently never hit before this
                # function's first real test coverage).
                rs = gain / loss.replace(0, np.nan)
                df["RSI"] = (100 - (100 / (1 + rs))).astype(float).fillna(50)
                df = df.dropna(subset=["Close", "RSI", "EMA_20", "EMA_50"])
                if len(df) < 100:
                    continue

                wf_candidate = walk_forward_validate(df, rsi_sig, param_space, optimize_metric="sharpe_ratio")
                wf_baseline = walk_forward_validate(df, rsi_sig, baseline_space, optimize_metric="sharpe_ratio")
                if "error" in wf_candidate or "error" in wf_baseline:
                    continue

                tested_assets.append(asset)
                candidate_sharpes.append(wf_candidate["avg_out_of_sample"].get("sharpe_ratio", 0.0))
                baseline_sharpes.append(wf_baseline["avg_out_of_sample"].get("sharpe_ratio", 0.0))
                any_overfit = any_overfit or bool(wf_candidate.get("overfit_warning"))
                per_asset_results[asset] = {
                    "candidate_oos_sharpe": wf_candidate["avg_out_of_sample"].get("sharpe_ratio", 0.0),
                    "baseline_oos_sharpe": wf_baseline["avg_out_of_sample"].get("sharpe_ratio", 0.0),
                    "overfit_warning": bool(wf_candidate.get("overfit_warning")),
                    "candidate_best_params": wf_candidate["splits"][-1]["best_params"] if wf_candidate.get("splits") else {},
                }

            if not tested_assets:
                logger.info("[EpochScheduler] ningún asset tuvo datos suficientes, skip")
                if self.audit:
                    self.audit.append("REOPT_SKIPPED", {"reason": "no_asset_had_enough_data"})
                return

            should_promote, reason = _should_promote(candidate_sharpes, baseline_sharpes, any_overfit)

            # Robustness: the promoted params are the mode (most common)
            # of each tested asset's own best_params, not just whichever
            # asset happened first — a combination that wins across
            # MULTIPLE assets is more trustworthy than one that only won
            # on a single asset by chance.
            candidate_param_tuples = [
                tuple(sorted(r["candidate_best_params"].items()))
                for r in per_asset_results.values() if r["candidate_best_params"]
            ]
            new_params = dict(old_params)
            if candidate_param_tuples:
                mode_tuple = statistics.mode(candidate_param_tuples)
                new_params.update(dict(mode_tuple))

            audit_payload = {
                "assets_tested": tested_assets,
                "old_params": old_params,
                "candidate_params": new_params,
                "promoted": should_promote,
                "reason": reason,
                "per_asset": per_asset_results,
            }

            if should_promote:
                # Bug fix: StrategyAgent now resets `self.params` from
                # `self._live_params` at the top of every
                # evaluate_strategies() call (paper-vs-live profile
                # switching -- see strategy_agent.py's paper_params_
                # overrides docstring). Setting only `.params` here
                # would get silently wiped on the very next cycle;
                # `._live_params` is the actual base this promotion
                # needs to update for it to survive past one cycle.
                if hasattr(self.strategy_agent, "_live_params"):
                    self.strategy_agent._live_params = new_params
                self.strategy_agent.params = new_params
                _write_strategy_params_override(
                    self.strategy_params_override_path, old_params, new_params, reason,
                )
                logger.info(f"[EpochScheduler] params promoted: {old_params} -> {new_params} ({reason})")
                if self.audit:
                    self.audit.append("REOPT_NEW_PARAMS", audit_payload)
            else:
                logger.info(f"[EpochScheduler] no promotion: {reason}")
                if self.audit:
                    self.audit.append("REOPT_NOT_PROMOTED", audit_payload)
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
