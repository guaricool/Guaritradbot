"""
Sprint 0+1 — main entrypoint.

Sprint 0 fix: lee `trading.*` y propaga los parámetros correctos a
RiskManager (risk_per_trade_pct, atr_stop_multiplier, min_order_usd).

Sprint 1 añade: audit ledger JSONL persistido en `audit/audit.jsonl`,
mandate gate opcional activado desde config.yaml, kill switch
filesystem. Cada evento relevante del bot queda registrado para
forensics post-mortem.
"""
import os
import argparse
import time

import pandas as pd
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
from src.execution.scheduler import EpochScheduler
from src.optimization.hyperopt import HyperoptManager
from src.safety.audit_ledger import AuditLedger
from src.safety.kill_switch import KillSwitch
from src.safety.mandate_gate import MandateGate, MandateConfig


def _audit_path(config: dict) -> str:
    audit_dir = config.get("mandate", {}).get("audit_log_dir", "audit")
    return os.path.join(audit_dir, "audit.jsonl")


def _build_mandate(config: dict, audit) -> tuple:
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
    return (MandateGate(mc, audit_ledger=audit), mc)


def main():
    parser = argparse.ArgumentParser(description="Guaritradbot Epic Multi-Agent Trading")
    parser.add_argument("--once", action="store_true", help="Execute the trading loop only once")
    args = parser.parse_args()

    print("=== Iniciando Bot Épico (Multi-Agente) ===")

    # 0. Cargar configuración
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
    max_capital_per_trade_pct = trading_cfg.get("max_capital_per_trade_pct", 10.0)
    min_order_usd = trading_cfg.get("min_order_usd", 10.0)

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

    # 1. Sprint 1: audit ledger, kill switch, mandate gate
    audit = AuditLedger(_audit_path(config))
    kill_switch = KillSwitch(config.get("mandate", {}).get("kill_switch_file", "/tmp/GUARITRADBOT_KILL"))
    mandate_gate, mandate_cfg = _build_mandate(config, audit)
    if kill_switch.is_triggered():
        audit.append("BOT_START_BLOCKED_KILLSWITCH", {"reason": "kill_file_present"})
        print("⛔ Kill switch armado al startup — bot no arranca.")
        return

    audit.append("BOT_START", {
        "execution_mode": execution_mode,
        "risk_per_trade_pct": risk_per_trade_pct,
        "mandate_enabled": mandate_cfg is not None,
        "max_position_usd": mandate_cfg.max_position_usd if mandate_cfg else 0,
    })

    # 2. Core subsystems
    event_bus = EventBus()
    execution_node = ExecutionNode(
        event_bus,
        execution_mode=execution_mode,
        broker_client=broker_client,
        kill_switch=kill_switch,
        audit=audit,
    )

    # 3. Optimizer opcional
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

    # 4. Agents registry
    registry = {
        "MarketAnalystAgent": MarketAnalystAgent(event_bus=event_bus),
        "StrategyAgent": StrategyAgent(strategy_params=strategy_params),
        "RiskManagerAgent": RiskManagerAgent(
            broker_client=broker_client,
            risk_per_trade_pct=risk_per_trade_pct,
            max_capital_per_trade_pct=max_capital_per_trade_pct,
            atr_stop_multiplier=atr_stop_multiplier,
            min_order_usd=min_order_usd,
            event_bus=event_bus,
            mandate_gate=mandate_gate,
            audit=audit,
        ),
        "ExecutionAgent": ExecutionAgent(event_bus=event_bus),
        "NotificationAgent": NotificationAgent(event_bus=event_bus, config=config),
    }

    engine = WorkflowEngine(registry)
    workflow_path = os.path.join("src", "workflows", "trading_loop.yaml")
    if not os.path.exists(workflow_path):
        print(f"Error: {workflow_path} no encontrado.")
        return
    workflow_data = engine.load_workflow(workflow_path)

    scheduler = EpochScheduler(engine, workflow_data, config_path)

    try:
        if args.once:
            print("[System] Corriendo en modo UNA SOLA VEZ (--once)")
            audit.append("WORKFLOW_START", {"mode": "once"})
            scheduler.start(run_once_for_test=True)
            audit.append("WORKFLOW_END", {"mode": "once"})
            print("\n=== Ciclo Único Completado ===")
            summary = audit.summary()
            print(f"📒 Audit summary: {summary['total_events']} events, {len(summary['by_type'])} types")
            print(f"   Audit file: {audit.path}")
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
    finally:
        # Botón de armado/desarmado: el usuario puede tocar el kill switch
        # desde otra terminal y la siguiente iteración del scheduler lo verá.
        # No disarmamos aquí a propósito: el estado armado persiste.
        pass


if __name__ == "__main__":
    main()
