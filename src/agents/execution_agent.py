class ExecutionAgent:
    """
    Agent responsible for executing approved trades in the market or a paper-trading simulation.
    """
    def simulate_execution(self, inputs: dict, state: dict):
        approved_trades = state.get("risk_evaluation", {}).get("approved_trades", [])
        
        print(f"[ExecutionAgent] Executing {len(approved_trades)} trades...")
        executed = []
        
        for trade in approved_trades:
            print(f"> Executed {trade['direction']} on {trade['asset']} at {trade['entry_price']:.2f}")
            executed.append(trade)
            
        return {"executed_trades": executed}
