#!/usr/bin/env python3
"""SessionStart hook: warn about stale pending items in cognitive state.

Reads the most recent cognitive state from the Genesis DB, parses pending
actions, and outputs warnings for items older than STALE_THRESHOLD_DAYS.
Output is injected into the CC session as hook context.
"""

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# The shared hook helpers live in scripts/hooks/; this script runs from scripts/
# (a different sys.path[0]), so add the hooks dir before importing them. Same
# idiom as the sibling SessionStart hook. Unguarded on purpose: a missing helper
# is a broken checkout and must be loud, not silently unbounded.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_output import BoundedStdout  # noqa: E402

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

    # The directive leads, and the variable-length lists follow it. The harness
    # keeps the HEAD of an over-cap hook's output and files the rest, so a
    # directive printed after its list is exactly what disappears — and it
    # disappears when the list is longest, i.e. when the situation is worst.
    # Worded without a positional reference ("the following", not "these") so it
    # reads correctly wherever a truncation lands.
    body = [
        f"STALE PENDING ITEMS ({age_days} days old — threshold is {STALE_THRESHOLD_DAYS} days)",
        f"ACTION REQUIRED: raise the following with the user before starting new "
        f"work. These items have been pending for over {STALE_THRESHOLD_DAYS} days.",
        f"Cognitive state last updated: {created_at.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
    ]
    body += [f"  {item}" for item in pending_items]
    if red_flags:
        body += ["", "RED FLAGS:"] + [f"  {flag}" for flag in red_flags]
    if yellow_flags:
        body += ["", "YELLOW FLAGS:"] + [f"  {flag}" for flag in yellow_flags]

    # `active_context` has no length cap in any of its three writers, so this
    # list is structurally unbounded even though it is small today (measured:
    # one row, 421 chars). The writer decides what survives; this hook does not
    # compute characters.
    out = BoundedStdout(label="stale-pending")
    out.emit_or_degrade(
        "\n".join(body),
        block="stale-pending",
        notice=(
            "\n[stale-pending: {kept} chars kept — the rest was omitted to stay "
            "under the hook output cap. Full state: cognitive_state.active_context]"
        ),
    )


if __name__ == "__main__":
    main()
