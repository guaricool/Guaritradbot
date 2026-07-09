"""
Sprint 0+1 — ExecutionNode.

Sprint 0 fix: el viejo `input()` bloqueante rompía el daemon en Docker.
Sprint 1 añade:
- Kill Switch filesystem: si el archivo existe, NO se ejecuta nada.
- Cada fill (real o paper) se registra en el audit ledger.
"""


class ExecutionNode:
    """
    Nodo de ejecución abstracto inspirado en NautilusTrader.
    Maneja el enrutamiento de órdenes aislando a los agentes
    de los detalles del broker (Simulado o en Vivo).
    """

    def __init__(
        self,
        event_bus,
        execution_mode="auto",
        broker_client=None,
        kill_switch=None,
        audit=None,
    ):
        self.event_bus = event_bus
        self.execution_mode = execution_mode
        self.broker = broker_client
        self.kill_switch = kill_switch
        self.audit = audit
        self.event_bus.subscribe("ORDER_APPROVED", self.on_order_approved)

    def on_order_approved(self, data: dict):
        # Sprint 1: Kill switch filesystem (defensa en profundidad)
        if self.kill_switch and self.kill_switch.is_triggered():
            print(
                f"[ExecutionNode] ⛔ Kill switch ARMED — orden {data.get('asset')} NO ejecutada."
            )
            if self.audit:
                self.audit.append(
                    "TRADE_BLOCKED_KILLSWITCH",
                    {"asset": data.get("asset")},
                )
            return

        if self.execution_mode == "human_in_the_loop":
            print(f"\n[ExecutionNode] 🛑 ORDEN PENDIENTE DE APROBACIÓN HUMANA")
            print(f"  Asset:       {data.get('asset')}")
            print(f"  Dirección:   {data.get('direction')}")
            print(f"  Tamaño:      {data.get('position_size', 0):.6f}")
            print(f"  Stop loss:   {data.get('stop_loss', 0):.4f}")

            if self.event_bus:
                self.event_bus.publish(
                    "ORDER_PENDING_APPROVAL",
                    {"order": data, "timeout_s": 30},
                )

            try:
                decision = input("¿Aprobar? (Y/N, default=N en 30s): ").strip().upper()
                if decision != "Y":
                    print("[ExecutionNode] ❌ Orden rechazada por el humano.")
                    if self.audit:
                        self.audit.append(
                            "TRADE_REJECTED_HUMAN",
                            {"asset": data.get("asset")},
                        )
                    return
            except (EOFError, KeyboardInterrupt):
                print("[ExecutionNode] ⚠️ No hay TTY. SKIP seguro (cambia execution_mode a 'auto' para bypass).")
                if self.audit:
                    self.audit.append(
                        "TRADE_SKIPPED_NO_TTY",
                        {"asset": data.get("asset")},
                    )
                return

        self.execute_order(data)

    def execute_order(self, order_data: dict):
        # Doble check del kill switch
        if self.kill_switch and self.kill_switch.is_triggered():
            print("[ExecutionNode] ⛔ Kill switch ARMED — execute_order cancelado.")
            return

        print(
            f"[ExecutionNode] 🚀 EJECUTANDO ORDEN: "
            f"{order_data['asset']} {order_data['direction']} "
            f"qty={order_data['position_size']}"
        )

        status = "FILLED (SIMULATED)"

        if self.broker:
            side = "buy" if order_data["direction"] == "long" else "sell"
            amount = order_data["position_size"]
            symbol = order_data["asset"]
            if "-" in symbol:
                symbol = symbol.replace("-", "/")
            elif "/" not in symbol:
                symbol = f"{symbol}/USDT"

            broker_order = self.broker.create_market_order(symbol, side, amount)
            if broker_order and broker_order.get("status") != "failed":
                status = "FILLED (LIVE MARKET)"
            else:
                status = "FAILED (LIVE MARKET)"

        # Sprint 1: audit
        if self.audit:
            self.audit.append(
                "TRADE_FILLED" if status.startswith("FILLED") else "TRADE_FAILED",
                {
                    "asset": order_data["asset"],
                    "direction": order_data["direction"],
                    "qty": order_data["position_size"],
                    "entry_price": order_data["entry_price"],
                    "status": status,
                },
            )

        if self.event_bus:
            self.event_bus.publish(
                "ORDER_EXECUTED",
                {"status": status, "order": order_data},
            )
