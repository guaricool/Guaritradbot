"""
Sprint 46R (audit B8): centralized atomic-write helper.

The audit's B8 finding: "Las escrituras JSON usan tmp+`replace()`
pero sin `fsync` (solo el audit ledger lo hace) — ante corte
de energía puede persistir el rename antes que los datos."

Pre-Sprint-46R, the same tmp+replace pattern was repeated in
seven places (api/state.py x4, kelly_drawdown.py, positions.py,
equity_tracker.py), all without the fsync call that
audit_ledger.py correctly has. A power loss between the
write+close of the tmp file and the rename would leave the
rename persisted but the data on disk would be from a prior
version (or empty), which is exactly the failure mode the
audit's C7 quarantine test was checking for.

This helper centralizes the pattern: write text, fsync the
fd to make sure the bytes hit disk, rename atomically. The
rename itself is atomic on POSIX (which the bot's container
is, since python:3.11-slim runs on a real Linux filesystem
-- the `atomic on POSIX` caveats that apply to NFS / FAT are
not relevant in the Coolify Docker setup). If a future
deployment hits a filesystem where rename isn't atomic, this
is the right place to add a workaround.

A note on the fsync-on-tmp-file (vs fsync-on-directory):
strict POSIX durability requires fsync on the tmp file AND
on the parent directory after rename. We only fsync the tmp
file here -- the parent dir's fsync is a per-filesystem
operation that's hard to get right across Docker bind mounts,
and the bot's data_store/ + audit/ + mode_override paths are
all on the same overlay fs as the container itself, which
flushes on its own. The audit_ledger helper has the same
single-fsync approach and has been correct in production for
months; we follow the same pattern.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Union


def atomic_write_text(
    path: Union[str, Path],
    text: str,
    encoding: str = "utf-8",
) -> None:
    """Atomically write `text` to `path` with fsync for crash-safety.

    Mirrors the pattern in src/safety/audit_ledger.py:65-93
    (the only pre-Sprint-46R caller that had the fsync), and
    consolidates the seven tmp+replace duplicates that lacked
    it. The `os.replace()` is atomic on POSIX (which the bot's
    container is on) and best-effort on Windows.

    Raises whatever `path.write_text` raises (e.g. PermissionError,
    OSError on a full disk). The tmp file is cleaned up on any
    write failure so a failed atomic write doesn't leave a
    stray .tmp behind to confuse the next boot's load.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # Some filesystems (network mounts, /proc, etc.)
                # don't support fsync. Best effort — same as
                # audit_ledger's behavior.
                pass
        os.replace(tmp, p)
    except Exception:
        # Clean up the tmp on any failure so a half-written
        # file doesn't survive to confuse the next caller.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
