"""Init function: _load_secrets."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


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
            logger.warning("No secrets.env found at %s", path)
    except (FileNotFoundError, OSError):
        logger.exception("Failed to read secrets file")
    except Exception:
        logger.exception("Failed to load secrets")
