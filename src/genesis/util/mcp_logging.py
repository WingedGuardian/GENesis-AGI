"""Environment-only selection of optional MCP log output and its rotation path.

Run this file directly to select a directory without importing Genesis or opening
a log: stdout is exactly one NUL-terminated filesystem path on success.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path


def _home_directory() -> Path | None:
    value = os.environ.get("HOME")
    if not value:
        if os.name == "posix":
            import pwd

            value = pwd.getpwuid(os.getuid()).pw_dir
        else:
            value = str(Path.home())
    home = Path(value)
    return home if home.is_absolute() else None


def mcp_log_directory() -> Path | None:
    """Select an existing absolute directory; do not create or open anything."""
    try:
        value = os.environ.get("GENESIS_MCP_LOG_DIR")
        if not value or value == "~" or value.startswith("~/"):
            home = _home_directory()
            if home is None:
                return None
            # Concatenate the suffix as expanduser does: with ~//logs, joining
            # value[2:] as a Path would instead discard HOME for /logs.
            candidate = home / "tmp" if not value else Path(str(home) + value[1:])
        else:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                home = _home_directory()
                if home is None:
                    return None
                candidate = home / candidate
        directory = candidate.resolve()
        return directory if directory.is_dir() else None
    except (OSError, RuntimeError, ValueError, KeyError):
        return None


def configure_mcp_logging(log_filter: logging.Filter) -> Path | None:
    """Attach the existing optional handler, or leave both file outputs disabled."""
    directory = mcp_log_directory()
    if directory is None:
        return None
    handler = None
    target = logging.getLogger("genesis")
    try:
        handler = logging.FileHandler(directory / "mcp_health.log")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s %(message)s",
        ))
        handler.setLevel(logging.DEBUG)
        handler.addFilter(log_filter)
        target.addHandler(handler)
    except Exception:
        if handler is not None:
            with contextlib.suppress(Exception):
                target.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
        return None
    return directory


def main() -> int:
    directory = mcp_log_directory()
    if directory is None:
        return 1
    try:
        sys.stdout.buffer.write(os.fsencode(directory) + b"\0")
        sys.stdout.buffer.flush()
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
