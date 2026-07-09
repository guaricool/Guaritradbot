"""
Sprint 0 fix — ExecutionAgent.

Ahora publica `ORDER_APPROVED` por cada trade aprobada para que
ExecutionNode (suscrito a ese evento) ejecute al broker real cuando
corresponda. Si no hay broker, sigue funcionando como paper-trading logger
para el dashboard.

Si broker == None → paper mode: solo loguea al state.
Si broker != None → ExecutionNode hace la ejecución real (este agent
solo emite el evento y deja que el broker decida).
"""
import logging

logger = logging.getLogger("ExecutionAgent")


class ExecutionAgent:
    """
    Toma trades aprobadas por RiskAgent y las enruta al broker real
    (via ExecutionNode) o las registra como paper-trades si no hay broker.
    """

    def __init__(self, event_bus=None):
        self.event_bus = event_bus

    def simulate_execution(self, inputs: dict, state: dict):
        """
        Publica ORDER_APPROVED por cada trade aprobada. ExecutionNode
        consume este evento y ejecuta al broker si está conectado.
        Mantiene compatibilidad: el state guardado en el dashboard
        sigue llamándose 'executed_trades'.
        """
        result = state.get("risk_evaluation", {})
        approved = result.get("approved_trades", [])
        rejected = result.get("rejected_trades", [])
        balance = result.get("account_balance", 0)
        balance_source = result.get("balance_source", "unknown")

        print(
            f"[ExecutionAgent] {len(approved)} aprobadas | {len(rejected)} rechazadas | "
            f"balance ${balance:.2f} ({balance_source})"
        )

        executed = []
        for trade in approved:
            executed.append(trade)
            print(
                f"  📋 route→ {trade['direction'].upper():5} {trade['asset']:8} "
                f"qty={trade['position_size']:.6f} risk=${trade['risk_usd']:.2f}"
            )
            # Emite el evento que consume ExecutionNode
            if self.event_bus:
                self.event_bus.publish("ORDER_APPROVED", trade)

        # El dashboard Streamlit lee 'executed_trades' del state → lo
        # mantenemos poblado aunque la ejecución real la haga otro componente.
        # En Sprint 1 esto se reemplaza por un `TradeJournal` real.
        return {
            "executed_trades": executed,
            "rejected_trades": rejected,
        }
