"""Connectivity window store — ``~/.genesis/network_state.json``.

The NetworkSentinel (``genesis.resilience.network_sentinel``) owns the live
in-memory connectivity state and publishes a snapshot here every probe round.
This module is the *lightweight, stdlib-only* read/write side of that store so
consumers that must NOT import the async probe machinery can still read it:

- ``genesis.observability.snapshots.infrastructure`` — the dashboard "Internet"
  light (runs in-process, but importing the sentinel would drag asyncio +
  probe deps into the health snapshot path).
- (PR-4) ``scripts/backup.sh`` — bash reads the JSON directly to decide whether
  a failed git push is network-caused.

New-store justification (New-Store Gate): consumers span the genesis-server
process, the standalone health MCP, and bash — a cross-process JSON status file
is the established pattern here (``status.json``, ``backup_status.json``,
``cred_integrity_status.json``). A DB table cannot serve the bash consumer and
the watchdog oneshot without opening the DB. Retention is built in: only the
last ``MAX_CLOSED_WINDOWS`` closed outage windows are kept, so the file is
self-bounding (no disk-hygiene wiring needed).

Snapshot schema (all keys always present after the first write)::

    {
      "state": "NORMAL" | "DEGRADED" | "OFFLINE",
      "since": "<ISO8601>",          # when the CURRENT state began
      "cause": "clean|partial|dns_only|all_fail",
      "last_probe_at": "<ISO8601>",  # freshness anchor — consumers staleness-check this
      "window_open": bool,           # True while an outage window is open
      "closed_windows": [            # most-recent-last, capped at MAX_CLOSED_WINDOWS
        {"start": "<ISO8601>", "end": "<ISO8601>", "cause": "..."}
      ]
    }
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Retention: an outage every hour for two days still fits. Bounds the file.
MAX_CLOSED_WINDOWS = 50


def default_path() -> Path:
    """Install-agnostic store path (adapts to any user via ``Path.home()``)."""
    return Path.home() / ".genesis" / "network_state.json"


def read_state(path: Path | None = None) -> dict | None:
    """Read the connectivity snapshot, tolerantly.

    Returns ``None`` when the file is absent (sentinel disabled / never ran —
    an empty-state install) or unreadable/corrupt. Callers treat ``None`` as
    "no connectivity signal" and behave exactly as they did before the sentinel
    existed. Never raises.
    """
    p = path or default_path()
    try:
        raw = p.read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("network_state.json is corrupt at %s — treating as absent", p)
        return None
    return data if isinstance(data, dict) else None


def write_state(data: dict, path: Path | None = None) -> None:
    """Atomically write the connectivity snapshot (mkstemp + os.replace).

    Caps ``closed_windows`` to ``MAX_CLOSED_WINDOWS`` (most recent kept) so the
    file self-bounds. Mirrors ``StatusFileWriter``'s atomic-write shape so a
    concurrent reader (dashboard, bash) never observes a partial file. Logs and
    swallows on failure — a failed connectivity write must never crash the
    sentinel loop (the in-memory axis remains authoritative).
    """
    p = path or default_path()
    windows = data.get("closed_windows")
    if isinstance(windows, list) and len(windows) > MAX_CLOSED_WINDOWS:
        data = {**data, "closed_windows": windows[-MAX_CLOSED_WINDOWS:]}

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=2)
        fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        fd_closed = False
        try:
            os.write(fd, content.encode())
            os.close(fd)
            fd_closed = True
            os.replace(tmp_path, str(p))
        except Exception:
            if not fd_closed:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
    except Exception:
        logger.error(
            "network_state write FAILED at %s — connectivity signal will be stale",
            p,
            exc_info=True,
        )


def probe_age_s(snapshot: dict | None, now: datetime) -> float | None:
    """Seconds since the snapshot's ``last_probe_at``, or ``None`` if unknowable.

    The shared freshness primitive for every consumer that must fail-safe on a
    dead/stalled sentinel (watchdog restart-suppression, dashboard light). A
    ``None`` return (absent snapshot, missing/garbled timestamp) means "no
    trustworthy connectivity signal" — callers must treat it as unknown and
    fail toward their SAFE default (the watchdog: do NOT suppress a restart).
    """
    if not isinstance(snapshot, dict):
        return None
    ts = snapshot.get("last_probe_at")
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    # Tolerate a naive stored timestamp by matching the caller's tzinfo.
    if parsed.tzinfo is None and now.tzinfo is not None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return (now - parsed).total_seconds()
