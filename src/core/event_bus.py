from typing import Callable, Dict, List, Any


class EventBus:
    """
    Bus de eventos central inspirado en NautilusTrader.
    Permite una arquitectura pub/sub (orientada a eventos).
    """
    def __init__(self):
        self.subscribers: Dict[str, List[Callable]] = {}
        # Sprint 43 H5 fix: track per-subscriber errors so we can
        # surface them in the audit log + a metric, instead of
        # silently killing the cycle.
        self.last_errors: Dict[str, List[Dict[str, Any]]] = {}

    def subscribe(self, event_type: str, callback: Callable):
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(callback)

    def publish(self, event_type: str, data: Any):
        """
        Publica un evento y notifica a todos los suscriptores.

        Sprint 43 H5 fix: each callback is wrapped in try/except.
        A failing subscriber no longer aborts the rest of the
        cycle. The error is logged (print + stored in
        `self.last_errors` for the audit reader) and the next
        subscriber is invoked. The audit caught that
        `risk_agent.publish(TRADE_OPENED, ...)` would crash
        mid-loop if any single subscriber raised.
        """
        print(f"[EventBus] Emitiendo evento: {event_type}")
        if event_type not in self.subscribers:
            return
        for i, callback in enumerate(self.subscribers[event_type]):
            try:
                callback(data)
            except Exception as e:
                # Isolate the failure. Log it loudly so the
                # operator sees it, and store for later audit.
                cb_name = getattr(callback, "__qualname__", repr(callback))
                print(
                    f"[EventBus] ⚠️ Subscriber {i} ({cb_name}) failed on "
                    f"event '{event_type}': {e!r}. Continuing to next subscriber."
                )
                self.last_errors.setdefault(event_type, []).append({
                    "subscriber_index": i,
                    "subscriber_name": cb_name,
                    "error": repr(e),
                    "event_data_summary": str(data)[:200] if data is not None else None,
                })
