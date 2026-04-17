#!/usr/bin/env python3
"""UserPromptSubmit hook that surfaces past-due pending reminders.

Bridge for CronCreate+durable upstream bug (anthropics/claude-code#40228).
Until the Genesis `memory_remind` MCP tool ships (see
~/.claude/plans/future-memory-remind.md), this hook + JSON sentinel files
in ~/.genesis/pending-reminders/ let us schedule one-shot reminders that
survive session exits.

Sentinel file format:
{
  "id": "stable-unique-identifier",
  "fire_at": "ISO8601 timestamp WITH timezone (naive is rejected)",
  "content": "reminder text surfaced as [Reminder] system-reminder",
  "created_at": "ISO8601 timestamp",
  "created_by": "who/what scheduled this",
  "source": "manual_bridge"   # or "memory_remind" once the MCP tool lands
}

Semantics:
- Files with fire_at > now are left untouched (checked on every prompt).
- Files with fire_at <= now are printed and unlinked (one-shot delivery).
- Corrupt / missing-field markers are unlinked silently so they don't
  keep tripping the hook. Naive-timezone markers are LEFT in place so
  the user can fix the file (they're a schema error, not data corruption).
- Past-due markers older than 1h have a "(delayed Xh)" note appended so
  delays are visible — reminders are never silently dropped without
  indicating the lag.

Hot path (empty dir): sub-millisecond.
Cold path (one past-due marker): a few ms.

Stdlib only. Fail-open on all errors — never blocks session start or
prompt submission.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


def _pending_dir() -> Path:
    base = os.environ.get("GENESIS_HOME") or str(Path.home() / ".genesis")
    return Path(base) / "pending-reminders"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_fire_at(raw: str) -> datetime | None:
    """Parse ISO8601 with timezone. Naive timestamps return None."""
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _iter_markers(d: Path) -> list[Path]:
    try:
        return [
            p for p in d.iterdir()
            if p.is_file() and p.suffix == ".json" and not p.name.startswith(".")
        ]
    except (FileNotFoundError, PermissionError):
        return []


def _format_reminder(data: dict, fire_at: datetime, now: datetime) -> str:
    content = str(data.get("content", "<missing content>"))
    rid = str(data.get("id", "<missing id>"))
    delay_seconds = (now - fire_at).total_seconds()
    delay_note = ""
    if delay_seconds > 3600:
        hours = int(delay_seconds // 3600)
        delay_note = f" (delayed {hours}h)"
    return (
        f"[Reminder] id={rid} fire_at={fire_at.isoformat()}{delay_note}\n"
        f"{content}"
    )


def main() -> int:
    try:
        d = _pending_dir()
        markers = _iter_markers(d)
        if not markers:
            return 0  # hot path — nothing pending

        now = _now()
        # Deterministic order: oldest-scheduled first (by fire_at if parseable,
        # falling back to mtime so corrupt ordering doesn't explode).
        markers.sort(key=lambda p: p.stat().st_mtime)

        fired: list[str] = []
        for marker in markers:
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                with contextlib.suppress(OSError):
                    marker.unlink()
                continue

            fire_at_raw = data.get("fire_at")
            if not isinstance(fire_at_raw, str):
                with contextlib.suppress(OSError):
                    marker.unlink()
                continue

            fire_at = _parse_fire_at(fire_at_raw)
            if fire_at is None:
                # Schema error (naive tz). Leave in place so user can fix.
                continue

            if fire_at > now:
                continue  # not due yet

            reminder_text = _format_reminder(data, fire_at, now)
            # Unlink BEFORE print so a crash between the two doesn't cause
            # double-delivery on the next prompt.
            with contextlib.suppress(OSError):
                marker.unlink()
            fired.append(reminder_text)

        if fired:
            print("\n\n".join(fired), flush=True)
        return 0
    except Exception:
        print("pending_reminders_hook error:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
