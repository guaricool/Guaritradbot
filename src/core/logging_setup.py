"""
Sprint 46R: shared logging setup.

The audit's B9 finding: "Logging por print en el core (197
llamadas): sin niveles ni timestamps; migrar a logging por
módulo."

Sprint 46R doesn't migrate all 197 prints in one commit (that's
a follow-up series -- the work is to remove the 'print to
stdout' habit from the critical-path files first, where it
matters for the audit's concerns: errors and warnings with no
level distinction are how the audit's `M9` finding
classified the EventBus exception swallowing). This module
sets up the framework:

  - `setup_logging()` is called from main.py at startup and
    configures the root logger with a timestamped, leveled
    format that matches the audit's expectation.
  - `get_logger(name)` is the standard idiom for module-local
    loggers: `logger = get_logger(__name__)`. Files that want
    structured levels (DEBUG/INFO/WARNING/ERROR) can use this
    immediately; files that still use `print()` for dev-facing
    output continue to work (Python's logging adds print()
    calls to the root logger's stream handler at WARNING by
    default, so existing print() calls still surface in the
    container logs, just without a level prefix).
  - The bot's existing log format (raw stdout with the
    container's timestamping) is preserved as the default,
    because the Sprint 34 NotificationAgent already keys off
    specific log-line patterns from print() and we don't want
    to break that contract in the same commit.
"""
from __future__ import annotations

import logging
import sys


# Pre-46R the bot used print() exclusively. The container's
# stdout already has timestamps (via the Docker log driver),
# but no level prefix. Post-46R, modules that opt into
# `logger = get_logger(__name__)` get the [LEVEL] prefix;
# the remaining print() calls (the gradual migration is
# tracked as a follow-up) keep working unchanged.
DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger. Idempotent — safe to call
    multiple times (the second call just reconfigures the
    handler with the new format/level).

    Args:
        level: minimum level to emit. Default INFO matches
            the pre-46R print() verbosity for most modules.
            DEBUG for local dev if you want every print to
            route through the logger.
    """
    root = logging.getLogger()
    # Remove any existing handlers (idempotency)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Standard idiom for module-local loggers. Returns a
    logger that inherits the root config (set via
    setup_logging above) and is named after the module.
    """
    return logging.getLogger(name)
