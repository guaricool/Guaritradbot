class ExecutionNode:
    """
    Nodo de ejecución abstracto inspirado en NautilusTrader.
    Maneja el enrutamiento de órdenes aislando a los agentes de los detalles del broker (Simulado o en Vivo).
    """
    def __init__(self, event_bus, execution_mode="auto", broker_client=None):
        self.event_bus = event_bus
        self.execution_mode = execution_mode
        self.broker = broker_client
        # Suscribirse a órdenes aprobadas
        self.event_bus.subscribe("ORDER_APPROVED", self.on_order_approved)

    def on_order_approved(self, data: dict):
        """
        Callback cuando el RiskManager aprueba una orden.
        """
        if self.execution_mode == "human_in_the_loop":
            print(f"\n[ExecutionNode] 🛑 ATENCIÓN: ORDEN PENDIENTE DE APROBACIÓN HUMANA")
            print(f"Propuesta: {data}")
            decision = input("¿Deseas ejecutar esta orden en el mercado real? (Y/N): ")
            if decision.strip().upper() != 'Y':
                print("[ExecutionNode] ❌ Orden rechazada por el humano.")
                return
            
        self.execute_order(data)

    def execute_order(self, order_data: dict):
        print(f"[ExecutionNode] 🚀 EJECUTANDO ORDEN EN EL MERCADO: {order_data}")
        
        status = "FILLED (SIMULATED)"
        
        # Enviar al broker real (si está conectado)
        if self.broker:
            # Transformar formato (Ej: direction "long" -> side "buy")
            side = "buy" if order_data["direction"] == "long" else "sell"
            amount = order_data["position_size"]
            symbol = order_data["asset"]
            
            # Muchos exchanges requieren un formato específico de par como BTC/USDT
            if "-" in symbol:
                symbol = symbol.replace("-", "/")
            elif "/" not in symbol:
                symbol = f"{symbol}/USDT" # Fallback a USDT
                
            broker_order = self.broker.create_market_order(symbol, side, amount)
            if broker_order and broker_order.get("status") != "failed":
                status = "FILLED (LIVE MARKET)"
            else:
                status = "FAILED (LIVE MARKET)"
                
        # Publicar que la orden se ejecutó
        self.event_bus.publish("ORDER_EXECUTED", {"status": status, "order": order_data})
