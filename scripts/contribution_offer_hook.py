#!/usr/bin/env python3
"""Phase 6 — UserPromptSubmit hook that surfaces pending contribution offers.

Runs on every user prompt via .claude/settings.json. Hot path:

1. `Path.iterdir()` on ~/.genesis/pending-offers/ — sub-millisecond if empty.
2. If non-empty: read oldest marker (by mtime), inject a `[Contribution]`
   system-reminder to stdout, unlink the marker.
3. Exit 0. Fail-open on all errors.

Budget: 500ms hook timeout configured in .claude/settings.json. Real cost
should be <5ms even when pending offers exist. No network, no heavy I/O.

Stdlib only — no Genesis imports, no venv required.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback
from pathlib import Path


def _pending_dir() -> Path:
    base = os.environ.get("GENESIS_HOME") or str(Path.home() / ".genesis")
    return Path(base) / "pending-offers"


def _oldest_marker(d: Path) -> Path | None:
    """Return the oldest (by mtime) *.json marker file, or None if empty.

    Skips files starting with '.' (temp files from atomic rename pattern).
    """
    try:
        candidates = [
            p for p in d.iterdir()
            if p.is_file() and p.suffix == ".json" and not p.name.startswith(".")
        ]
    except (FileNotFoundError, PermissionError):
        return None
    if not candidates:
        return None
    # mtime sort — oldest first. For a handful of markers this is O(n) and
    # well under a millisecond.
    candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates[0]


def _load_marker(marker: Path) -> dict | None:
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _format_reminder(marker_data: dict) -> str:
    sha = str(marker_data.get("sha", "unknown"))[:12]
    subject = str(marker_data.get("subject", "<unknown subject>"))
    return (
        f"[Contribution] A 'fix:' commit just landed ({sha} \"{subject}\"). "
        f"Per the Phase 6 contribution pipeline, proactively offer the user "
        f"the option to contribute this fix upstream to the public Genesis "
        f"repo. Ask conversationally; do not run the pipeline without explicit "
        f"user approval. If the user declines or ignores, do nothing."
    )


def main() -> int:
    try:
        d = _pending_dir()
        marker = _oldest_marker(d)
        if marker is None:
            return 0  # hot path: nothing pending, silent exit

        data = _load_marker(marker)
        if data is None:
            # Corrupt marker — remove it so we don't keep tripping over it.
            with contextlib.suppress(OSError):
                marker.unlink()
            return 0

        reminder = _format_reminder(data)

        # Unlink BEFORE printing so a crash after print doesn't leave the
        # marker around for double-injection on the next prompt. Print failure
        # on a stale fd is rare; re-injection after a crash is the more likely
        # annoyance.
        with contextlib.suppress(OSError):
            marker.unlink()

        print(reminder, flush=True)
        return 0
    except Exception:
        # Fail-open. Log to stderr (CC captures it for debug but never shows
        # it to the user).
        print("contribution_offer_hook error:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
