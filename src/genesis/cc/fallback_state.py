"""Account-wide CC fallback-state record (cross-process).

When the home model (Claude) is rate-limited/exhausted — an ACCOUNT-WIDE
condition — conversation turns fail over to a roster peer (e.g. GLM). This module
records that degraded condition in a small JSON file so it survives across
processes: the server writes it on switch/recovery, and the Phase-4 header hook +
dashboard read it to show an honest "running on <peer>" indicator.

Scope: ACCOUNT-WIDE only (``is_fallback`` / ``original`` / ``fallback`` /
``reason`` / ``since``). A Claude rate limit affects every conversation, so one
global flag is correct. The STICKY per-conversation peer session id is NOT stored
here — it lives per-session in ``cc_sessions.metadata`` (key ``fallback_session``)
so concurrent conversations don't share/clobber each other's continuation session.

Writes are atomic (temp file + ``os.replace``, mirroring ``sentinel/shared.py``)
because the server and background sessions may touch the file concurrently; reads
tolerate a missing/corrupt file (return inactive). No secret is ever stored here.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from genesis.env import genesis_home

logger = logging.getLogger(__name__)

_STATE_FILE = "cc_fallback_state.json"


def _state_path() -> Path:
    return genesis_home() / _STATE_FILE


@dataclass(frozen=True)
class FallbackState:
    """Account-wide CC fallback condition (advisory, cross-process)."""

    is_fallback: bool = False
    original: str = ""  # home model name (e.g. "claude")
    fallback: str = ""  # active peer name (e.g. "glm-5.2")
    reason: str = ""  # short cause (e.g. "rate_limit")
    since: str = ""  # ISO8601 UTC when the fallback began


_INACTIVE = FallbackState()


def read() -> FallbackState:
    """Current fallback state; inactive default on a missing/corrupt file.

    Never raises — it sits on the conversation hot path and on the CC session-start
    hook path, neither of which may be broken by a bad state file.
    """
    try:
        raw = _state_path().read_text()
    except (FileNotFoundError, OSError):
        return _INACTIVE
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Corrupt cc_fallback_state.json — treating as inactive", exc_info=True)
        return _INACTIVE
    if not isinstance(data, dict):
        return _INACTIVE
    return FallbackState(
        is_fallback=bool(data.get("is_fallback", False)),
        original=str(data.get("original", "")),
        fallback=str(data.get("fallback", "")),
        reason=str(data.get("reason", "")),
        since=str(data.get("since", "")),
    )


def _write(state: FallbackState) -> None:
    """Atomic write (temp file + os.replace) so a reader never sees partial JSON."""
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(asdict(state), indent=2).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, str(path))
    except OSError:
        logger.error("Failed to write cc_fallback_state.json", exc_info=True)


def enter(original: str, fallback: str, reason: str) -> bool:
    """Transition inactive→active. Returns True ONLY on the actual transition.

    Idempotent: if already active, refreshes the active peer/reason (in case the
    failover moved to a different peer) but returns False, so the caller fires
    exactly one "switched to fallback" ALERT per outage.
    """
    current = read()
    if current.is_fallback:
        if current.fallback != fallback or current.reason != reason:
            _write(FallbackState(
                is_fallback=True,
                original=current.original or original,
                fallback=fallback,
                reason=reason,
                since=current.since,
            ))
        return False
    _write(FallbackState(
        is_fallback=True,
        original=original,
        fallback=fallback,
        reason=reason,
        since=datetime.now(UTC).isoformat(),
    ))
    return True


def clear() -> bool:
    """Transition active→inactive. Returns True ONLY if it was active.

    Idempotent, so the caller fires exactly one "recovered" ALERT per outage.
    """
    if not read().is_fallback:
        return False
    _write(_INACTIVE)
    return True
