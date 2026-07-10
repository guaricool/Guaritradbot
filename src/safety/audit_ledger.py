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

try:
    import fcntl  # POSIX-only (Linux/macOS). Not available on Windows.
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False


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
        # Sprint 43 M1 fix: take an exclusive lock on the file
        # before writing, so a concurrent process (e.g. the bot
        # container + the dashboard container, both mounting the
        # same audit volume) cannot interleave writes. The audit
        # caught that "lines corrupt, silently discarded" was
        # possible because the old `with open(..., 'a')` block
        # relied solely on O_APPEND for atomicity, which POSIX
        # only guarantees for writes ≤ PIPE_BUF (~4KB on Linux).
        # JSONL lines for large TRADE_FILLED events can exceed
        # that. fcntl.flock blocks the other writer until we're
        # done.
        #
        # On Windows, fcntl is unavailable. We fall back to the
        # O_APPEND-only behavior. The bot runs on Linux (VPS),
        # so the Windows path is just for local dev on Carlos's
        # workstation. The fsync below still ensures the bytes
        # hit disk before we release the lock.
        open_mode = "a"
        if HAS_FCNTL:
            # Use line-buffered text mode + flock. We need binary
            # mode for fcntl.flock, so open in 'ab' and write bytes.
            open_mode = "ab"
        with open(self.path, open_mode) as f:
            if HAS_FCNTL:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except (OSError, AttributeError):
                    # Best-effort: if flock fails for any reason
                    # (e.g. NFS doesn't support it), fall through
                    # to O_APPEND-only.
                    pass
            line = (json.dumps(event, ensure_ascii=False, default=str) + "\n")
            if HAS_FCNTL:
                # Binary mode + flock → need bytes
                if isinstance(line, str):
                    line = line.encode("utf-8")
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # Some filesystems (e.g. virtual mounts) don't
                # support fsync. Best effort.
                pass
            if HAS_FCNTL:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except (OSError, AttributeError):
                    pass
        return event

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        dropped = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        # Sprint 45 fix (M1): the audit ledger's own
                        # docstring calls it "forenseable" / tamper-
                        # evident, but a corrupted or torn line (e.g.
                        # a crash mid-write, or — before the flock fix
                        # above — an interleaved write from a second
                        # process) was silently discarded with no
                        # trace at all. Log a warning so a corrupted
                        # ledger is at least visible instead of just
                        # quietly missing events.
                        dropped += 1
                        print(
                            f"[AuditLedger] WARNING: dropping malformed "
                            f"line {lineno} in {self.path} (invalid JSON)"
                        )
                        continue
        if dropped:
            print(f"[AuditLedger] WARNING: {dropped} malformed line(s) skipped in {self.path}")
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
