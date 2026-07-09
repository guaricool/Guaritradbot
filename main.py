import os
from src.workflows.engine import WorkflowEngine
from src.agents.market_analyst import MarketAnalystAgent
from src.agents.strategy_agent import StrategyAgent
from src.agents.risk_agent import RiskManagerAgent
from src.agents.execution_agent import ExecutionAgent
from src.agents.notification_agent import NotificationAgent
from src.core.event_bus import EventBus
from src.execution.execution_node import ExecutionNode
import yaml
import pandas as pd
from src.optimization.hyperopt import HyperoptManager
from src.execution.broker import BrokerClient
from src.execution.scheduler import EpochScheduler
import argparse
def main():
    print("=== Iniciando Bot Épico (Multi-Agente) ===")
    
    # 0. Load global configuration
    config_path = "config.yaml"
    execution_mode = "auto"
    optimize_on_start = False
    broker_client = None
    max_capital_per_trade_pct = 10.0
    
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
            execution_mode = config.get("execution_mode", "auto")
            optimize_on_start = config.get("optimize_on_start", False)
            
            # Setup Exchange
            exchange_cfg = config.get("exchange", {})
            if exchange_cfg:
                exchange_name = exchange_cfg.get("name", "binance")
                use_testnet = exchange_cfg.get("use_testnet", True)
                max_capital_per_trade_pct = exchange_cfg.get("max_capital_per_trade_pct", 10.0)
                try:
                    broker_client = BrokerClient(exchange_name=exchange_name, use_testnet=use_testnet)
                except Exception as e:
                    print(f"Error al inicializar el broker: {e}. El bot correrá en modo Simulación Pura.")

    # 1. Instantiate Core Subsystems (Nautilus Architecture)
    event_bus = EventBus()
    execution_node = ExecutionNode(event_bus, execution_mode=execution_mode, broker_client=broker_client)
    
    # Opcional: Optimizar parámetros
    strategy_params = None
    if optimize_on_start:
        print("[Optimizador] Iniciando Grid Search de parámetros...")
        try:
            from test_hyperopt import create_dummy_data
            df_hist = create_dummy_data() # Usamos dummy temporalmente para ejemplo
            hyperopt = HyperoptManager()
            
            def rsi_sig(data, **p):
                return StrategyAgent.generate_vectorized_signals(data, strategy_type="RSI", **p)
                
            param_space = {"rsi_oversold": [25, 30, 35], "rsi_overbought": [65, 70, 75]}
            best_p = hyperopt.optimize("RSI_MeanReversion", df_hist, param_space, rsi_sig)
            if best_p:
                strategy_params = best_p
        except Exception as e:
            print(f"[Optimizador] Error durante la optimización: {e}")
            
    # 2. Instantiate the Agents Registry
    registry = {
        "MarketAnalystAgent": MarketAnalystAgent(event_bus=event_bus),
        "StrategyAgent": StrategyAgent(strategy_params=strategy_params),
        "RiskManagerAgent": RiskManagerAgent(broker_client=broker_client, max_capital_per_trade_pct=max_capital_per_trade_pct),
        "ExecutionAgent": ExecutionAgent(event_bus=event_bus),
        "NotificationAgent": NotificationAgent(event_bus=event_bus, config=config)
    }
    
    # 2. Instantiate the Workflow Engine
    engine = WorkflowEngine(registry)
    
    # 3. Load the YAML Workflow definition
    workflow_path = os.path.join("src", "workflows", "trading_loop.yaml")
    
    if not os.path.exists(workflow_path):
        print(f"Error: {workflow_path} no encontrado.")
        return
        
    workflow_data = engine.load_workflow(workflow_path)
    
    # 4. Run the workflow using the Scheduler
    scheduler = EpochScheduler(engine, workflow_data, config_path)
    
    parser = argparse.ArgumentParser(description="Guaritradbot Epic Multi-Agent Trading")
    parser.add_argument("--once", action="store_true", help="Execute the trading loop only once")
    args = parser.parse_args()
    
    if args.once:
        print("[System] Corriendo en modo UNA SOLA VEZ (--once)")
        scheduler.start(run_once_for_test=True)
        # Mostrar el resumen para la ejecución única
        # Necesitamos sacar el último estado (no está devolviendo el scheduler, pero el engine guarda memoria)
        print("\n=== Ciclo Único Completado ===")
    else:
        print("[System] Iniciando Demonio en Segundo Plano (Modo Épocas)...")
        scheduler.start(run_once_for_test=False)

if __name__ == "__main__":
    main()
