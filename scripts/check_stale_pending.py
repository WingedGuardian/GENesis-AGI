#!/usr/bin/env python3
"""SessionStart hook: warn about stale pending items in cognitive state.

Reads the most recent cognitive state from the Genesis DB, parses pending
actions, and outputs warnings for items older than STALE_THRESHOLD_DAYS.
Output is injected into the CC session as hook context.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

STALE_THRESHOLD_DAYS = 3
DB_PATH = Path.home() / "genesis" / "data" / "genesis.db"


def main() -> None:
    if not DB_PATH.exists():
        return

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=3)
        cursor = conn.execute(
            "SELECT content, created_at FROM cognitive_state "
            "WHERE section = 'active_context' ORDER BY created_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        conn.close()
    except Exception:
        return  # Don't block session start on DB errors

    if not row:
        return

    content, created_at_str = row
    try:
        created_at = datetime.fromisoformat(created_at_str)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        created_at = None

    # Parse pending actions section
    lines = content.split("\n")
    in_pending = False
    pending_items = []
    for line in lines:
        if line.strip().startswith("**Pending Actions**"):
            in_pending = True
            continue
        if in_pending:
            if line.strip().startswith("**") and "Pending" not in line:
                break  # Hit next section
            stripped = line.strip()
            if stripped and stripped[0].isdigit() and "." in stripped[:4]:
                # Numbered item like "1. **Fix foo** — description"
                pending_items.append(stripped)

    if not pending_items or not created_at:
        return

    now = datetime.now(UTC)
    age = now - created_at
    age_days = age.days

    if age_days < STALE_THRESHOLD_DAYS:
        return

    # Parse state flags for red/yellow items
    red_flags = []
    yellow_flags = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("\U0001f534"):  # Red circle
            red_flags.append(stripped)
        elif stripped.startswith("\U0001f7e1"):  # Yellow circle
            yellow_flags.append(stripped)

    print(f"STALE PENDING ITEMS ({age_days} days old — threshold is {STALE_THRESHOLD_DAYS} days)")
    print(f"Cognitive state last updated: {created_at.strftime('%Y-%m-%d %H:%M')} UTC")
    print()
    for item in pending_items:
        print(f"  {item}")
    if red_flags:
        print()
        print("RED FLAGS:")
        for flag in red_flags:
            print(f"  {flag}")
    if yellow_flags:
        print()
        print("YELLOW FLAGS:")
        for flag in yellow_flags:
            print(f"  {flag}")
    print()
    print(
        "ACTION REQUIRED: Raise these with the user before starting new work. "
        "These items have been pending for over 3 days."
    )


if __name__ == "__main__":
    main()
