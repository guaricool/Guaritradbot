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


class KillSwitch:
    def __init__(self, path: str):
        self.path = Path(path)

    def arm(self) -> None:
        self.path.touch()
        print(f"[KillSwitch] ARMED at {self.path}")

    def disarm(self) -> None:
        if self.path.exists():
            self.path.unlink()
            print(f"[KillSwitch] DISARMED ({self.path} removed)")

    def is_triggered(self) -> bool:
        triggered = self.path.exists()
        if triggered:
            print(f"\n⛔ [KillSwitch] TRIGGERED — file found at {self.path}")
            print("   rm %s  ← para revivir" % self.path)
        return triggered
