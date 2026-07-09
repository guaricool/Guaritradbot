class RiskManagerAgent:
    """
    Agent responsible for validating hypotheses and sizing positions based on risk rules.
    """
    def __init__(self, broker_client=None, max_capital_per_trade_pct=10.0):
        self.broker = broker_client
        self.max_capital_per_trade_pct = max_capital_per_trade_pct

    def validate_and_size(self, inputs: dict, state: dict):
        hypotheses = state.get("generate_hypotheses", {}).get("hypotheses", [])
        
        # Obtener el balance real del exchange (o simulado si no hay API válida)
        account_balance = self.broker.get_usdt_balance() if self.broker else 100.0
        
        print(f"[RiskManagerAgent] Evaluating {len(hypotheses)} hypotheses. Current Balance: ${account_balance:.2f}")
        approved_trades = []
        
        for hyp in hypotheses:
            # En lugar de un Stop Loss fijo, vamos a invertir un % fijo de nuestro capital por operación (Ej. 10%)
            capital_to_invest = account_balance * (self.max_capital_per_trade_pct / 100.0)
            
            # Filtro de protección: Exchange requiere mínimo $10 por operación típicamente
            if capital_to_invest < 10.0:
                print(f"[RiskManagerAgent] ❌ Orden rechazada: Capital asignado (${capital_to_invest:.2f}) es menor al mínimo de $10 del exchange.")
                continue
                
            # Calculamos el tamaño de la posición en base a monedas
            entry_price = hyp["price"]
            position_size = capital_to_invest / entry_price if entry_price > 0 else 0
            
            # Mock ATR stop loss distance (e.g., $5 away) para tener un stop loss
            stop_loss_distance = 5.0
            
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
