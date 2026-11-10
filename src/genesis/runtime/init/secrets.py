"""Init function: _load_secrets."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def unloadable_secrets_file() -> Path | None:
    """The secrets file's path when it cannot be loaded, else None.

    `genesis serve` asks this after bootstrap, once the startup credential
    restore has run, and exits rather than serve when the file is missing or
    cannot be read: the dashboard's password lives in this file, and no
    password means auth is disabled. Neither is the choice to run without one.
    The unit's `EnvironmentFile=-` ignores read errors as well as a missing
    file, so the read is attempted here.
    """
    from genesis.env import secrets_path

    path = secrets_path()
    try:
        if not path.is_file():
            return path
        with path.open("rb") as fh:
            fh.read(1)
    except OSError:
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
