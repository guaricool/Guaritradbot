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
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.logging_setup import get_logger
logger = get_logger(__name__)

try:
    import fcntl  # POSIX-only (Linux/macOS). Not available on Windows.
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

try:
    import msvcrt  # Windows-only equivalent of fcntl.flock.
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False


class AuditLedger:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def _maybe_rotate(self) -> None:
        """Sprint 46S (audit A8): rotate the live ledger to
        `audit-YYYY-MM.jsonl` when the calendar month has moved on,
        so `audit.jsonl` never grows past ~1 month of events. Before
        this, the file grew forever (audit's exact complaint:
        "audit.jsonl crece sin límite"), and every reader that calls
        `read_all()` (the dashboard's tail loop used to, before it
        switched to byte-offset tailing; `summary()`, `read_by_type()`
        still do) re-parses the WHOLE file every time.

        How it decides a rotation is due: compares the CURRENT file's
        mtime month against the current wall-clock month. A file that
        was last written to in an earlier month means the calendar
        rolled over since the last append — everything in it belongs
        to that earlier month, so it gets renamed out of the way and
        a fresh empty file takes over `self.path`. No separate marker
        file needed; this makes the check self-contained and correct
        across bot restarts (an idle bot that skips writing for a
        whole month would rotate on its first write back, which is
        exactly the desired behavior).

        `self.path` is deliberately kept as a STABLE path across
        rotations — src/api/server.py's `_audit_tail_loop` tails it by
        byte offset, and a path that changed mid-month would silently
        break that offset tracking. Only the ARCHIVED old file gets a
        dated name; the live file the rest of the bot writes to and
        reads from never moves.

        Read methods (`read_all`/`read_since`/`read_by_type`/
        `summary`) intentionally only see the CURRENT file after a
        rotation — same behavior as any rotated log (`docker logs`
        doesn't merge in old rotated files either). Archived
        `audit-YYYY-MM.jsonl` files remain on disk for manual/forensic
        inspection; they're just not auto-merged back in.

        Best-effort: any OSError here is logged and swallowed — a
        rotation hiccup must never block the actual event write below.

        Known edge case: `MandateGate._daily_loss_usd`/`_open_exposure_usd`
        have an audit-log-scanning FALLBACK for their rolling-24h checks,
        used only when `position_repo` isn't available. Right at a month
        boundary (e.g. checking at 00:30 on the 1st for a window back to
        the previous afternoon), that fallback would only see this
        month's events post-rotation, undercounting the true 24h window.
        In production `position_repo` is always constructed and passed
        (see main.py's `_build_mandate` call), so MandateGate always uses
        its PREFERRED PositionRepository-based path instead — this
        fallback is dormant in the live bot today. Flagged here so it
        isn't a surprise if that ever changes.
        """
        try:
            if not self.path.exists():
                return
            size = self.path.stat().st_size
            if size == 0:
                return
            current_month = time.strftime("%Y-%m")
            file_month = time.strftime("%Y-%m", time.localtime(self.path.stat().st_mtime))
            if file_month == current_month:
                return
            archive_path = self.path.parent / f"audit-{file_month}.jsonl"
            if archive_path.exists():
                # Already rotated once this month (e.g. a prior
                # restart already did it, or two processes raced) --
                # append rather than clobber so no events are lost.
                with open(self.path, "rb") as _src:
                    _leftover = _src.read()
                if _leftover:
                    with open(archive_path, "ab") as _dst:
                        _dst.write(_leftover)
                self.path.unlink()
                self.path.touch(exist_ok=True)
            else:
                os.rename(self.path, archive_path)
                self.path.touch(exist_ok=True)
            logger.info(f'[AuditLedger] Rotated {file_month} events to {archive_path.name}')
        except OSError as e:
            logger.warning(f'[AuditLedger] WARNING: monthly rotation check failed (continuing without rotating): {e}')

    def append(self, event_type: str, payload: dict[str, Any]) -> dict:
        self._maybe_rotate()
        event = {
            "ts": time.time(),
            # Sprint 46S (audit M12): "los timestamps de audit.jsonl son
            # naive, sin offset" -- `time.strftime` (the old code here)
            # formats wall-clock local time with NO indication of which
            # timezone/offset it's in, which is ambiguous the moment the
            # container's TZ changes (or differs between the bot host and
            # whoever's reading the file later). `datetime.now().astimezone()`
            # attaches the system's current UTC offset (e.g.
            # "2026-07-11T14:30:00-04:00"), so the same string is
            # unambiguous regardless of reader timezone. `ts` (unix
            # epoch, always UTC) was already unambiguous and is
            # unchanged -- this only fixes the human-readable "iso" field.
            "iso": datetime.now().astimezone().isoformat(timespec="seconds"),
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
        # On Windows, fcntl is unavailable; msvcrt.locking() is the
        # equivalent (Sprint 43 M1 originally only handled POSIX --
        # on Windows every append() was completely unlocked, so two
        # threads/processes racing to append could both seek to the
        # same "end of file" offset and one write clobber the
        # other's line, silently losing an event with no corruption
        # to even flag it). Bot runs on Linux (VPS) in production;
        # this path matters for local dev/tests on Windows. The
        # fsync below still ensures the bytes hit disk before we
        # release the lock.
        need_binary = HAS_FCNTL or HAS_MSVCRT
        open_mode = "ab" if need_binary else "a"
        with open(self.path, open_mode) as f:
            if HAS_FCNTL:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except (OSError, AttributeError):
                    # Best-effort: if flock fails for any reason
                    # (e.g. NFS doesn't support it), fall through
                    # to O_APPEND-only.
                    pass
            elif HAS_MSVCRT:
                # Lock a 1-byte "mutex" region at offset 0 (the
                # actual write always lands at EOF regardless of
                # seek position, since the file was opened in
                # append mode). LK_LOCK blocks, retrying for ~10s,
                # before raising -- best-effort, same as the flock
                # branch above.
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                except OSError:
                    pass
            line = (json.dumps(event, ensure_ascii=False, default=str) + "\n")
            if need_binary:
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
            elif HAS_MSVCRT:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
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
                        logger.warning(f'[AuditLedger] WARNING: dropping malformed line {lineno} in {self.path} (invalid JSON)')
                        continue
        if dropped:
            logger.warning(f'[AuditLedger] WARNING: {dropped} malformed line(s) skipped in {self.path}')
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
