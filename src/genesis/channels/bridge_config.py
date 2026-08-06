"""Shared, side-effect-free Telegram bridge-config parser.

The SINGLE source of truth for "given secrets.env content, what Telegram config would
the adapter load (or None if it can't start)?" — used by BOTH the live adapter
start-gate (``channels.bridge._load_bridge_config``) and the onboarding readiness T2
signal (``genesis.onboarding.readiness``), so the two can NEVER diverge on parsing or
validation. Stdlib-only (no ``GenesisRuntime``, no file IO, no ``sys.exit``) so it is
safe to import and call on the dashboard hot path.

Parsing mirrors the adapter's historical manual semantics (line-based,
``key.strip() = value.strip().strip('"')``, ``#``-comment lines skipped) — deliberately
NOT dotenv, which strips single quotes / inline comments / interpolates ``${VAR}``.
Malformed values the adapter would choke on (a non-numeric ``DAY_BOUNDARY_HOUR``, or a
``TELEGRAM_ALLOWED_USERS`` entry that ``str.isdigit()`` accepts but ``int()`` rejects —
e.g. ``'²'``) RAISE here exactly as they do in the adapter; a caller that must not crash
(readiness) wraps the call and treats a raise as "not loadable".
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

_TELEGRAM_TOKEN_PLACEHOLDER = "PLACEHOLDER"  # noqa: S105 - sentinel, not a credential


def parse_secrets_env_text(text: str) -> dict[str, str]:
    """Parse ``secrets.env`` TEXT with the adapter's manual semantics (NOT dotenv)."""
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key.strip()] = value.strip().strip('"')
    return parsed


def build_bridge_config(
    secrets: Mapping[str, str], *, log: logging.Logger | None = None
) -> dict | None:
    """Build the Telegram bridge config from a parsed secrets mapping.

    Returns the config dict, or ``None`` if Telegram cannot start (missing/placeholder
    token, or no valid numeric recipient). RAISES (``ValueError``) on a value the live
    adapter would also choke on — a non-numeric ``DAY_BOUNDARY_HOUR``, or a
    ``TELEGRAM_ALLOWED_USERS`` entry that ``str.isdigit()`` accepts but ``int()`` rejects
    — so the adapter's fail-to-load behaviour is preserved for its caller. ``log``
    (optional) receives the adapter's diagnostic messages; readiness passes none (silent
    on the dashboard hot path).
    """
    token = secrets.get("TELEGRAM_BOT_TOKEN", "")
    if not token or token == _TELEGRAM_TOKEN_PLACEHOLDER:
        if log:
            log.info("TELEGRAM_BOT_TOKEN not set — Telegram adapter will not start")
        return None

    allowed_users: set[int] = set()
    allowed_raw = secrets.get("TELEGRAM_ALLOWED_USERS", "")
    if allowed_raw:
        for uid in allowed_raw.split(","):
            uid = uid.strip()
            if uid.isdigit():
                allowed_users.add(int(uid))
            elif uid and log:
                log.warning("Invalid UID in TELEGRAM_ALLOWED_USERS: %r", uid)

    if not allowed_users:
        if log:
            log.error(
                "TELEGRAM_ALLOWED_USERS is empty or has no valid user IDs — "
                "Telegram will not start. Set numeric user IDs "
                "(get yours from @userinfobot on Telegram)"
            )
        return None

    # Optional forum chat ID for per-session topics.
    forum_raw = secrets.get("TELEGRAM_FORUM_CHAT_ID", "")
    forum_chat_id = int(forum_raw) if forum_raw.strip().lstrip("-").isdigit() else None

    return {
        "token": token,
        "allowed_users": allowed_users,
        "whisper_model": secrets.get("WHISPER_MODEL", "whisper-large-v3"),
        "day_boundary_hour": int(secrets.get("DAY_BOUNDARY_HOUR", "0")),
        "forum_chat_id": forum_chat_id,
    }
