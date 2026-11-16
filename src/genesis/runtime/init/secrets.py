"""Init function: _load_secrets."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def unloadable_secrets_file() -> Path | None:
    """The secrets file's path when its keys cannot load, else None.

    `genesis serve` asks this after bootstrap, once the startup credential
    restore has run, and exits rather than serve when it returns a path: the
    dashboard's password lives in this file, and no password means auth is
    disabled. A file that is missing, unreadable, or present but unloadable is
    not the choice to run without one. The unit's `EnvironmentFile=-` ignores
    all of these, so the check is made here, in two parts:
    - Guardian's `validate_bytes`, the one shared definition of a corrupt file
      (empty, NUL bytes, not UTF-8, no key at all);
    - python-dotenv's own parser, which the server's load uses: a byte-order
      mark or any line it cannot parse (an unterminated quote swallows every
      line after it) would silently drop keys, the password among them.
    """
    from dotenv.parser import parse_stream

    from genesis.env import secrets_path
    from genesis.guardian.cred_integrity import validate_bytes

    path = secrets_path()
    try:
        if not path.is_file():
            return path
        data = path.read_bytes()
    except OSError:
        return path
    if not validate_bytes("dotenv", data, min_keys=1).ok:
        return path
    text = data.decode("utf-8")  # validate_bytes proved it decodes
    if text.startswith("\ufeff"):
        return path
    if any(binding.error for binding in parse_stream(io.StringIO(text))):
        return path
    return None


def load(rt: GenesisRuntime) -> None:
    """Load API keys from secrets.env."""
    try:
        from dotenv import load_dotenv

        from genesis.env import secrets_path

        path = secrets_path()
        if path.is_file():
            load_dotenv(str(path), override=True)
            logger.info("Secrets loaded from %s", path)
        else:
            logger.critical("No secrets.env found at %s", path)
            from genesis.env import alert_queue_root
            from genesis.guardian.alert.queue import enqueue_alert

            enqueue_alert(
                alert_queue_root(),
                severity="critical",
                source="runtime.secrets",
                title="Secrets file missing at startup",
                body="Run bootstrap or restore credentials from backup; provider keys are unavailable.",
                dedupe_key="runtime:missing-secrets",
            )
    except (FileNotFoundError, OSError):
        logger.exception("Failed to read secrets file")
    except Exception:
        logger.exception("Failed to load secrets")
