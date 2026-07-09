import os
from src.workflows.engine import WorkflowEngine
from src.agents.market_analyst import MarketAnalystAgent
from src.agents.strategy_agent import StrategyAgent
from src.agents.risk_agent import RiskManagerAgent
from src.agents.execution_agent import ExecutionAgent

def main():
    print("=== Iniciando Bot Épico (Multi-Agente) ===")
    
    # 1. Instantiate the Agents Registry
    registry = {
        "MarketAnalystAgent": MarketAnalystAgent(),
        "StrategyAgent": StrategyAgent(),
        "RiskManagerAgent": RiskManagerAgent(),
        "ExecutionAgent": ExecutionAgent()
    }
    
    # 2. Instantiate the Workflow Engine
    engine = WorkflowEngine(registry)
    
    # 3. Load the YAML Workflow definition
    workflow_path = os.path.join("src", "workflows", "trading_loop.yaml")
    
    if not os.path.exists(workflow_path):
        print(f"Error: {workflow_path} no encontrado.")
        return
        
    workflow_data = engine.load_workflow(workflow_path)
    
    # 4. Run the workflow
    final_state = engine.run(workflow_data)
    
    print("\n=== Resumen Final de Operaciones ===")
    executed_trades = final_state.get("execute_trades", {}).get("executed_trades", [])
    if not executed_trades:
        print("No se ejecutaron operaciones en este ciclo.")
    else:
        for t in executed_trades:
            print(f"- {t['asset']} | {t['strategy']} | {t['direction'].upper()} @ {t['entry_price']:.2f} | Size: {t['position_size']:.2f}")

if __name__ == "__main__":
    main()
