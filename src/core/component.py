"""
Sprint 6 — Component base con State Machine.

Inspirado en NautilusTrader:
- PRE_INITIALIZED  : instanciado pero no configurado
- READY           : configurado, listo para arrancar
- RUNNING         : en operación normal
- STARTING/STOPPING/STOPPED : transiciones
- DEGRADED        : funcionando parcialmente
- FAULTED         : error no recuperable, debe parar
- DISPOSED        : cleanup completo

Combinado con fail-fast data integrity (rechazar NaN/Infinity/negativos),
esto previene la corrupción silenciosa de data que es la causa #1 de
pérdidas en bots algorítmicos.
"""
from __future__ import annotations
from enum import Enum
from typing import Optional
import logging


logger = logging.getLogger("Component")


class ComponentState(str, Enum):
    PRE_INITIALIZED = "PRE_INITIALIZED"
    READY = "READY"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAULTED = "FAULTED"
    DISPOSED = "DISPOSED"


class Component:
    """
    Base para todos los componentes del sistema. Controla lifecycle
    y emite transiciones de estado al audit ledger.
    """

    def __init__(self, name: str, audit=None):
        self.name = name
        self.state: ComponentState = ComponentState.PRE_INITIALIZED
        self.audit = audit
        self._last_error: Optional[str] = None

    def _transition(self, new_state: ComponentState, reason: str = ""):
        """Cambia de estado y emite al audit ledger si está conectado."""
        old_state = self.state
        if old_state != new_state:
            self.state = new_state
            if self.audit:
                self.audit.append(
                    f"COMPONENT_STATE_{new_state.value}",
                    {"component": self.name, "from": old_state.value, "reason": reason or ""},
                )
            logger.info(f"[{self.name}] {old_state.value} → {new_state.value} ({reason})")

    def start(self):
        if self.state == ComponentState.READY:
            self._transition(ComponentState.STARTING)
            self._transition(ComponentState.RUNNING, "start()")
            return True
        elif self.state == ComponentState.RUNNING:
            return True
        else:
            logger.warning(f"[{self.name}] cannot start from {self.state.value}")
            return False

    def stop(self):
        if self.state in (ComponentState.RUNNING, ComponentState.DEGRADED):
            self._transition(ComponentState.STOPPING)
            self._transition(ComponentState.STOPPED, "stop()")

    def fault(self, reason: str):
        self._last_error = reason
        self._transition(ComponentState.FAULTED, reason)

    def degrade(self, reason: str):
        if self.state == ComponentState.RUNNING:
            self._transition(ComponentState.DEGRADED, reason)

    def recover(self):
        if self.state == ComponentState.DEGRADED:
            self._transition(ComponentState.RUNNING, "recovered")

    def ready(self):
        if self.state == ComponentState.PRE_INITIALIZED:
            self._transition(ComponentState.READY, "configure()")
