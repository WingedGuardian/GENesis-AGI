"""Credential-integrity init: startup self-heal + per-tick hook wiring.

Two entry points, both defensive (a failure here must never block bootstrap):

- ``selfheal_startup(rt)`` runs BEFORE secrets are loaded, so a corrupt/zeroed
  ``secrets.env`` is restored from backup before ``load_dotenv`` reads it.
- ``wire(rt)`` installs the per-tick check + self-heal on the awareness loop.
  Wired independently of ``guardian_remote.yaml`` (unlike the credential
  bridge) — integrity self-heal is valuable guardian-or-not.
"""

from __future__ import annotations

import logging

from genesis.guardian.cred_selfheal import check_and_selfheal

logger = logging.getLogger(__name__)


def selfheal_startup(rt) -> None:
    """Validate + restore credential files at startup (before secrets load)."""
    try:
        check_and_selfheal(startup=True)
    except Exception:
        logger.warning("startup credential self-heal failed", exc_info=True)


def wire(rt) -> None:
    """Install the per-tick credential integrity self-heal on the awareness loop."""
    loop = getattr(rt, "_awareness_loop", None)
    if loop is None:
        return
    loop.set_cred_integrity_fn(check_and_selfheal)
    logger.debug("credential-integrity self-heal wired to awareness tick")
