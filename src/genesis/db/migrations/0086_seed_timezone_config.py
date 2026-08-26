"""One-time seed: adopt ``USER_TIMEZONE`` env into ``genesis.yaml`` before the
timezone source-of-truth flip (env-first → file-first) goes live.

``genesis.env.user_timezone()`` now resolves ``genesis.yaml{timezone}`` →
``USER_TIMEZONE`` env → UTC (previously env-first). On an install whose
``genesis.yaml`` carries the shipped ``timezone: UTC`` sentinel
(``config/genesis.yaml.example``) — or has no ``timezone`` key — while the REAL
zone lives only in the ``USER_TIMEZONE`` env, the flip would silently re-time
every ``CronTrigger(timezone=user_timezone())`` and all display to UTC on the
next restart. This migration copies the real env zone into the file BEFORE the
flip takes effect, so the flip preserves behavior on existing installs.

Why a migration (verified): migrations run in ``runtime/init/db.py`` right after
``_load_secrets`` (so ``USER_TIMEZONE`` is already in ``os.environ``) and BEFORE
any scheduler binds (so the current boot's CronTriggers get the corrected zone),
on EVERY deploy path including ``git pull`` + restart. The migrations ledger
makes it strictly ONE-TIME, so it can NEVER later revert a deliberate
dashboard-set UTC (a per-boot startup check would).

STRICTLY FAIL-OPEN: ``runtime/init/db.py`` aborts server startup if a migration
raises, so the whole body is guarded — a ``genesis.yaml`` write hiccup must never
wedge boot. The ``USER_TIMEZONE`` env fallback still resolves the zone at runtime
in the (rare) write-failure case.

Self-contained per the migration convention (see 0077/0085): the read/write logic
is inlined and frozen, not imported from evolving runtime code, so the effect is
deterministic across deployment history. It touches a FILE, not the DB — unusual
for a schema migration but legitimate (``up`` is arbitrary Python).

Trigger (all must hold): ``USER_TIMEZONE`` env is set AND ``!= UTC``
(case-insensitive) AND ``genesis.yaml`` has no ``timezone`` key OR it equals UTC.
Otherwise a no-op (file already holds a real zone / env unset / env is UTC).
Idempotent by construction and one-time by the ledger regardless.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


def _seed_timezone_into_config() -> None:
    """Adopt ``USER_TIMEZONE`` env into ``genesis.yaml`` when the file is UTC/absent.

    Uses the SAME path ``genesis.env._local_config`` reads
    (``Path.home()/.genesis/config/genesis.yaml``) so the seed lands where the
    resolver looks. Fully self-contained; the caller guards against exceptions.
    """
    env_tz = (os.environ.get("USER_TIMEZONE") or "").strip()
    if not env_tz or env_tz.upper() == "UTC":
        return  # nothing real to seed

    import yaml  # noqa: PLC0415 — lazy import, yaml is always available

    cfg_path = Path.home() / ".genesis" / "config" / "genesis.yaml"
    existing: dict = {}
    if cfg_path.is_file():
        try:
            with cfg_path.open() as fh:
                loaded = yaml.safe_load(fh)
        except Exception:
            logger.warning(
                "tz-seed: could not parse %s — leaving it unchanged",
                cfg_path,
                exc_info=True,
            )
            return
        if isinstance(loaded, dict):
            existing = loaded

    file_tz = str(existing.get("timezone", "") or "").strip()
    if file_tz and file_tz.upper() != "UTC":
        return  # file already holds a real zone — authoritative, leave it

    # File is absent-key or UTC-sentinel while env is a real zone → adopt env.
    existing["timezone"] = env_tz
    cfg_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(cfg_path.parent), suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            yaml.dump(existing, fh, default_flow_style=False)
        os.replace(tmp, str(cfg_path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    logger.info(
        "tz-seed: adopted USER_TIMEZONE env into %s (behavior-preserving before "
        "the timezone source-of-truth flip)",
        cfg_path,
    )

    # Best-effort: refresh runtime caches so THIS boot's display reflects the
    # seed. Schedulers read user_timezone() fresh at registration (which runs
    # after migrations), so they are already correct; this only fixes the
    # display cache if tz.py was imported before the seed. Cosmetic on failure.
    try:
        from genesis.env import _invalidate_local_config
        from genesis.util.tz import reload as tz_reload

        _invalidate_local_config()
        tz_reload()
    except Exception:
        logger.debug("tz-seed: cache refresh skipped", exc_info=True)


async def up(db: aiosqlite.Connection) -> None:  # noqa: ARG001 — file migration, no DB use
    # STRICTLY FAIL-OPEN: init/db.py aborts server startup if a migration raises.
    try:
        _seed_timezone_into_config()
    except Exception:
        logger.warning(
            "tz-seed migration hit an error — leaving genesis.yaml unchanged; the "
            "USER_TIMEZONE env fallback still resolves the zone at runtime.",
            exc_info=True,
        )


async def down(db: aiosqlite.Connection) -> None:  # noqa: ARG001
    # Not cleanly reversible — a seeded zone is indistinguishable from a
    # user-set one. No-op.
    return
