#!/usr/bin/env python3
"""Stop hook: don't let a deliverable session end with an unverified artifact.

Part of the `deliverable-builder` skill. The skill writes a per-session marker at
`~/.genesis/sessions/<session_id>/deliverable.json` carrying a `status`:

    drafting -> rendered_unverified -> verified -> shipped   (or cancelled)

This hook blocks session-end (exit 2) ONLY when *this* session's marker is in
`rendered_unverified` — i.e. something was rendered but Gate 2 hasn't passed it. In a
correct run, Render -> Gate 2 is immediate, so that state only persists if the session
rendered something and tried to quit without verifying — exactly what we prevent.

Fail-open is ABSOLUTE: no session id, no marker, a marker for a different session, a
malformed marker, or ANY exception -> exit 0 (allow). A bug here must never wedge a session.

Reads hook input from stdin as JSON: {"session_id": "...", ...}.  Stdlib only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_BLOCK_STATUS = "rendered_unverified"

_BLOCK_MSG = (
    "BLOCKED: this session rendered a deliverable that has not passed Gate 2 verification.\n"
    "Resolve before ending:\n"
    "  - run Gate 2 (references/qa-protocol.md) and record a PASS (status -> verified), or\n"
    "  - if abandoning it, set the marker status to \"cancelled\".\n"
    "Marker: ~/.genesis/sessions/<session_id>/deliverable.json"
)


def _sessions_root() -> Path:
    return Path.home() / ".genesis" / "sessions"


def _decide(data: dict, sessions_root: Path) -> int:
    """Return 2 to block, 0 to allow. Fail-open on anything unexpected."""
    try:
        sid = data.get("session_id")
        # Falsy or path-unsafe session id -> allow (never touch arbitrary paths).
        if not sid or "/" in sid or ".." in sid:
            return 0
        marker = sessions_root / sid / "deliverable.json"
        if not marker.is_file():
            return 0
        spec = json.loads(marker.read_text())
        # A marker that names a different session is stale/foreign -> never block.
        marker_sid = spec.get("session_id")
        if marker_sid and marker_sid != sid:
            return 0
        if spec.get("status") == _BLOCK_STATUS:
            return 2
        return 0
    except Exception:
        return 0  # fail-open


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0  # fail-open on unreadable/malformed stdin
    if not isinstance(data, dict):
        return 0
    code = _decide(data, _sessions_root())
    if code == 2:
        print(_BLOCK_MSG, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
