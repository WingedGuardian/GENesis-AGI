"""Init function: _load_secrets."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def missing_secrets_file() -> Path | None:
    """The secrets file's path when it is still missing, else None.

    `genesis serve` asks this after bootstrap, once the startup credential
    restore has run, and exits rather than serve when it is missing: the
    dashboard's password lives in this file, and no password means auth is
    disabled. A missing file is not the choice to run without one.
    """
    from genesis.env import secrets_path

    path = secrets_path()
    return None if path.is_file() else path


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
