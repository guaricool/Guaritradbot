from collections import deque
from typing import Callable, Dict, List, Any, Deque


class EventBus:
    """
    Bus de eventos central inspirado en NautilusTrader.
    Permite una arquitectura pub/sub (orientada a eventos).
    """
    # Sprint 46R audit M9: cap the per-event-type error buffer to
    # 50 entries. Pre-46R this was an unbounded list, so a long-
    # running daemon (the bot runs for weeks at a time) would grow
    # the dict without bound. The cap keeps the most recent N
    # errors available for the /api/audit reader and any future
    # "last N subscriber errors" dashboard widget, while bounding
    # memory. 50 is a guess at "more than enough to debug a
    # flapping subscriber" without leaking.
    _LAST_ERRORS_MAXLEN = 50

    def __init__(self):
        self.subscribers: Dict[str, List[Callable]] = {}
        # Sprint 43 H5 fix: track per-subscriber errors so we can
        # surface them in the audit log + a metric, instead of
        # silently killing the cycle.
        # Sprint 46R audit M9: switched from `List` to `deque(maxlen)`
        # to cap memory growth on long-running daemons.
        self.last_errors: Dict[str, Deque[Dict[str, Any]]] = {}

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
                    f"[EventBus] WARNING: Subscriber {i} ({cb_name}) failed on "
                    f"event '{event_type}': {e!r}. Continuing to next subscriber."
                )
                # Sprint 46R audit M9: deque(maxlen) caps the buffer.
                # First access creates the deque; subsequent appends
                # silently evict the oldest entry once we hit maxlen.
                bucket = self.last_errors.setdefault(
                    event_type, deque(maxlen=self._LAST_ERRORS_MAXLEN)
                )
                bucket.append({
                    "subscriber_index": i,
                    "subscriber_name": cb_name,
                    "error": repr(e),
                    "event_data_summary": str(data)[:200] if data is not None else None,
                })
