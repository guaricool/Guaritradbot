class ExecutionNode:
    """
    Nodo de ejecución abstracto inspirado en NautilusTrader.
    Maneja el enrutamiento de órdenes aislando a los agentes de los detalles del broker (Simulado o en Vivo).
    """
    def __init__(self, event_bus, execution_mode="auto"):
        self.event_bus = event_bus
        self.execution_mode = execution_mode
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
        # En una implementación real, aquí se llamaría a la API de Binance, Interactive Brokers, o un simulador local.
        print(f"[ExecutionNode] 🚀 EJECUTANDO ORDEN EN EL MERCADO: {order_data}")
        # Publicar que la orden se ejecutó
        self.event_bus.publish("ORDER_EXECUTED", {"status": "FILLED", "order": order_data})
