class ExecutionAgent:
    """
    Agent responsible for executing approved trades in the market or a paper-trading simulation.
    """
    def __init__(self, event_bus=None):
        self.event_bus = event_bus

    def simulate_execution(self, inputs: dict, state: dict):
        approved_trades = state.get("risk_evaluation", {}).get("approved_trades", [])
        
        print(f"[ExecutionAgent] Executing {len(approved_trades)} trades...")
        executed = []
        
        for trade in approved_trades:
            print(f"> Executed {trade['direction']} on {trade['asset']} at {trade['entry_price']:.2f}")
            executed.append(trade)
            
        if self.event_bus and executed:
            self.event_bus.emit("TRADES_EXECUTED", {"trades": executed})
            
        return {"executed_trades": executed}
