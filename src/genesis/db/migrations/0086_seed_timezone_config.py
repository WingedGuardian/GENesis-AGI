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


def _read_env_timezone() -> str:
    """The pre-flip effective ``USER_TIMEZONE`` — process env if present, else
    parsed directly from secrets.env.

    The server-startup migration path has secrets loaded into ``os.environ``
    (``load_dotenv`` in runtime/init/secrets runs BEFORE migrations). But the
    ``update.sh`` upgrade path runs ``python -m genesis.db.migrations --apply``
    WITHOUT loading secrets.env, so ``os.environ`` lacks USER_TIMEZONE there —
    read the file directly so the seed captures the real zone on BOTH paths (else
    the flip silently re-times an upgraded install to UTC).
    """
    val = (os.environ.get("USER_TIMEZONE") or "").strip()
    if val:
        return val
    try:
        from genesis.env import secrets_path  # stable path resolver (SECRETS_PATH / repo_root)

        path = secrets_path()
        if path.is_file():
            for raw in path.read_text().splitlines():
                line = raw.strip()
                if line.startswith("USER_TIMEZONE="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        logger.debug("tz-seed: could not read USER_TIMEZONE from secrets.env", exc_info=True)
    return ""


def _is_valid_zone(name: str) -> bool:
    """True iff ``name`` resolves as an IANA zone (guards against typos)."""
    if not name:
        return False
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return False


def _seed_timezone_into_config() -> None:
    """Adopt the effective ``USER_TIMEZONE`` into ``genesis.yaml`` when the file's
    timezone is UTC / absent / an invalid typo.

    Uses the SAME path ``genesis.env._local_config`` reads
    (``Path.home()/.genesis/config/genesis.yaml``) so the seed lands where the
    resolver looks. Self-contained; the caller guards against exceptions.
    """
    env_tz = _read_env_timezone()
    if not env_tz or env_tz.upper() == "UTC":
        return  # nothing real to seed
    if not _is_valid_zone(env_tz):
        # A broken USER_TIMEZONE resolved to UTC pre-flip anyway (ZoneInfo failure
        # → UTC); do not write a bad value into the now-authoritative file.
        logger.warning("tz-seed: USER_TIMEZONE=%r is not a valid IANA zone — not seeding", env_tz)
        return

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
    # Leave the file alone ONLY when it already holds a real, VALID non-UTC zone.
    # A non-UTC TYPO must not shadow a valid env zone → fall through and fix it.
    if file_tz and file_tz.upper() != "UTC" and _is_valid_zone(file_tz):
        return

    # File is absent-key / UTC-sentinel / an invalid typo while env is a real,
    # valid zone → adopt env.
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
    # FAIL-OPEN: init/db.py aborts server startup if a migration raises, and a
    # timezone seed must never brick a boot. The tradeoff (chosen over Codex's
    # "keep pending for retry", which would require raising → aborting startup):
    # a swallowed WRITE failure means the flip leaves this install on UTC until
    # the tz is set via the dashboard Timezone control or genesis.yaml — so it is
    # logged at ERROR (actionable), not silently.
    try:
        _seed_timezone_into_config()
    except Exception:
        logger.error(
            "tz-seed migration FAILED to write genesis.yaml — timezone may resolve "
            "to UTC after this upgrade. Set it via the dashboard Timezone control "
            "(Configuration tab) or add `timezone:` to ~/.genesis/config/genesis.yaml.",
            exc_info=True,
        )


async def down(db: aiosqlite.Connection) -> None:  # noqa: ARG001
    # Not cleanly reversible — a seeded zone is indistinguishable from a
    # user-set one. No-op.
    return
