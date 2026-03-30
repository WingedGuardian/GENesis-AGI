"""Structured logging configuration for Genesis subsystems."""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class GenesisFormatter(logging.Formatter):
    """key=value log formatter for Genesis subsystems.

    Output: ts=2026-03-04T12:00:00+00:00 level=WARNING name=genesis.routing msg="..."
    """

    def __init__(self, *, clock=None):
        super().__init__()
        self._clock = clock or (lambda: datetime.now(UTC))

    def format(self, record: logging.LogRecord) -> str:
        ts = self._clock().isoformat()
        msg = record.getMessage()
        parts = [
            f"ts={ts}",
            f"level={record.levelname}",
            f"name={record.name}",
            f'msg="{msg}"',
        ]
        line = " ".join(parts)
        if record.exc_info and record.exc_info[0] is not None:
            import traceback

            line += "\n" + "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
        elif record.exc_text:
            line += "\n" + record.exc_text
        return line


def configure_logging(
    *,
    level: int = logging.INFO,
    clock=None,
    log_dir: str | None = None,
) -> None:
    """Set up structured logging for the ``genesis.*`` logger hierarchy.

    Adds a stderr handler with GenesisFormatter. Optionally adds a
    RotatingFileHandler for persistent logs.  Sets propagate=False
    so AZ's logging is unaffected.

    Parameters
    ----------
    log_dir:
        Directory for the rotating log file.  Defaults to
        ``$GENESIS_LOG_DIR`` or ``~/genesis/logs``.  Set to ``""``
        to disable file logging.
    """
    genesis_logger = logging.getLogger("genesis")
    genesis_logger.setLevel(level)
    genesis_logger.propagate = False

    # Avoid duplicate handlers on repeated calls
    if any(isinstance(h, logging.StreamHandler) for h in genesis_logger.handlers):
        return

    formatter = GenesisFormatter(clock=clock)

    # Stderr handler (always)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(level)
    stderr_handler.setFormatter(formatter)
    genesis_logger.addHandler(stderr_handler)

    # Rotating file handler (persistent logs)
    # Skip file handler during tests — test output pollutes production logs
    running_under_test = "pytest" in sys.modules or "_pytest" in sys.modules
    if log_dir != "" and not running_under_test:
        resolved = log_dir or os.environ.get(
            "GENESIS_LOG_DIR",
            str(Path.home() / "genesis" / "logs"),
        )
        log_path = Path(resolved)
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path / "genesis.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        genesis_logger.addHandler(file_handler)
