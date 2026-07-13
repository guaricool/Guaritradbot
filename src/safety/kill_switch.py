"""
Sprint 1 — Kill Switch filesystem.

Si el archivo existe, TODO el bot debe parar inmediatamente.
Patrón de Vibe-Trading — un archivo en disco es la red de seguridad
más simple y más auditable.

Uso:
    ks = KillSwitch("/tmp/GUARITRADBOT_KILL")
    ks.arm()    # crear el archivo → mata el bot
    ks.disarm() # borrar el archivo → revive
    if ks.is_triggered():
        sys.exit("kill switch activado")
"""
import os
from pathlib import Path

from src.core.logging_setup import get_logger
logger = get_logger(__name__)


class KillSwitch:
    def __init__(self, path: str):
        self.path = Path(path)

    def arm(self) -> None:
        self.path.touch()
        logger.info(f'[KillSwitch] ARMED at {self.path}')

    def disarm(self) -> None:
        # Sprint 43 L1 fix: use missing_ok=True so a TOCTOU race
        # between two operators disarming at the same time doesn't
        # raise FileNotFoundError. The audit flagged this as a
        # low-severity race; the fix is one keyword.
        self.path.unlink(missing_ok=True)
        logger.info(f'[KillSwitch] DISARMED ({self.path} removed)')

    def is_triggered(self) -> bool:
        triggered = self.path.exists()
        if triggered:
            logger.info(f'\n⛔ [KillSwitch] TRIGGERED — file found at {self.path}')
            logger.info('   rm %s  ← para revivir' % self.path)
        return triggered
