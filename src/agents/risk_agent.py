class RiskManagerAgent:
    """
    Agent responsible for validating hypotheses and sizing positions based on risk rules.
    """
    def __init__(self, max_risk_per_trade=0.01):
        self.max_risk_per_trade = max_risk_per_trade
        self.account_balance = 100000.0 # Mock initial balance

    def validate_and_size(self, inputs: dict, state: dict):
        hypotheses = state.get("generate_hypotheses", {}).get("hypotheses", [])
        
        print(f"[RiskManagerAgent] Evaluating {len(hypotheses)} hypotheses...")
        approved_trades = []
        
        for hyp in hypotheses:
            # We would normally calculate ATR here to place Stop Loss
            # For this architectural version, we approve all with a mock risk calculation
            risk_amount = self.account_balance * self.max_risk_per_trade
            
            # Mock ATR stop loss distance (e.g., $5 away)
            stop_loss_distance = 5.0
            
            # Position Size = Risk Amount / Stop Loss Distance
            position_size = risk_amount / stop_loss_distance if stop_loss_distance > 0 else 0
            
            approved_trades.append({
                "asset": hyp["asset"],
                "strategy": hyp["strategy"],
                "direction": hyp["direction"],
                "entry_price": hyp["price"],
                "stop_loss": hyp["price"] - stop_loss_distance if hyp["direction"] == "long" else hyp["price"] + stop_loss_distance,
                "position_size": position_size
            })
            print(f"[RiskManagerAgent] Approved {hyp['direction']} trade for {hyp['asset']} with size {position_size:.2f}")

        return {"approved_trades": approved_trades}
