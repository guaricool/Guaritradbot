from typing import Callable, Dict, List, Any

class EventBus:
    """
    Bus de eventos central inspirado en NautilusTrader.
    Permite una arquitectura pub/sub (orientada a eventos).
    """
    def __init__(self):
        self.subscribers: Dict[str, List[Callable]] = {}

    def subscribe(self, event_type: str, callback: Callable):
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(callback)

    def publish(self, event_type: str, data: Any):
        """
        Publica un evento y notifica a todos los suscriptores.
        """
        print(f"[EventBus] Emitiendo evento: {event_type}")
        if event_type in self.subscribers:
            for callback in self.subscribers[event_type]:
                callback(data)
