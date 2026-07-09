"""
Sprint 1 — Audit Ledger append-only.

Cada evento significativo del bot se persiste aquí. Los tipos de
eventos incluyen (Sprint 1):
  - BOT_START, BOT_STOP
  - WORKFLOW_START, WORKFLOW_END
  - MARKET_DATA_READY
  - HYPOTHESIS_GENERATED
  - TRADE_APPROVED, TRADE_REJECTED
  - TRADE_FILLED, TRADE_CLOSED
  - MANDATE_BLOCKED, MANDATE_OK
  - KILL_SWITCH_ARMED, KILL_SWITCH_DISARMED
  - SYSTEM_ERROR

El audit ledger es la fuente de verdad para forensics: cuándo se
abrió un trade, por qué se rechazó, etc. Append-only = inmutable en
la práctica (no permite editar eventos pasados).

JSONL = una línea JSON por evento, fácil de leer con `jq`, `grep`,
pandas.
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Any


class AuditLedger:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, event_type: str, payload: dict[str, Any]) -> dict:
        event = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event_type": event_type,
            **payload,
        }
        # Append atómico: abrir, escribir, fsync, cerrar
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return event

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def read_since(self, ts: float) -> list[dict]:
        return [r for r in self.read_all() if r.get("ts", 0) >= ts]

    def read_by_type(self, event_type: str) -> list[dict]:
        return [r for r in self.read_all() if r.get("event_type") == event_type]

    def summary(self) -> dict:
        rows = self.read_all()
        types = {}
        for r in rows:
            t = r.get("event_type", "unknown")
            types[t] = types.get(t, 0) + 1
        return {"total_events": len(rows), "by_type": types}
