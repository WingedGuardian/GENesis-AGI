#!/usr/bin/env python3
"""Seed procedural_memory with known battle-tested procedures.

These procedures are extracted from existing hooks, memory files, and
incident-driven lessons. They start at their natural activation tier
(L1 for hook-backed procedures, L3 for session-level knowledge).

Run once, or re-run safely (uses upsert with deterministic IDs).

Usage:
    python scripts/seed_procedures.py [--db PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

import aiosqlite

from genesis.db.schema import create_all_tables, seed_data

# Deterministic UUIDs from namespace + task_type for idempotent re-runs
_NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def _id(task_type: str) -> str:
    return str(uuid.uuid5(_NS, task_type))


SEED_PROCEDURES = [
    {
        "id": _id("youtube_content_fetch"),
        "task_type": "youtube_content_fetch",
        "principle": "YouTube WebFetch is unreliable in this environment. Use yt-dlp instead.",
        "steps": [
            "Use yt-dlp instead of WebFetch for YouTube URLs",
            "For metadata: yt-dlp --skip-download --print '%(title)s|||%(uploader)s|||%(description)s' URL",
            "For transcript: yt-dlp --write-auto-sub --skip-download --sub-lang en -o '~/tmp/%(id)s' URL",
            "Read the VTT file at ~/tmp/VIDEO_ID.en.vtt for full transcript",
            "If SSL issues recur: add --no-check-certificate flag",
        ],
        "tools_used": ["Bash", "Read"],
        "context_tags": ["youtube", "video", "transcript", "ssl", "content-fetch"],
        "activation_tier": "L1",
        "tool_trigger": ["WebFetch"],
        "speculative": 0,
        "success_count": 5,
        "confidence": 0.86,  # (5+1)/(5+0+2) = 0.857
    },
    {
        "id": _id("youtube_transcript_services"),
        "task_type": "youtube_transcript_services",
        "principle": "When yt-dlp fails or when you need a different approach, use transcript extraction services.",
        "steps": [
            "Try youtubetotranscript.com — paste the YouTube URL to get transcript",
            "Search for alternative transcript services if that one is down",
            "Do NOT retry the same failed approach — find a different CLASS of tool",
        ],
        "tools_used": ["WebFetch", "WebSearch"],
        "context_tags": ["youtube", "transcript", "workaround", "fallback"],
        "activation_tier": "L3",
        "tool_trigger": None,
        "speculative": 0,
        "success_count": 2,
        "confidence": 0.75,  # (2+1)/(2+0+2) = 0.75
    },
    {
        "id": _id("pip_editable_worktree_safety"),
        "task_type": "pip_editable_worktree_safety",
        "principle": "Never pip install -e to a worktree — it redirects ALL system imports and crashes the bridge.",
        "steps": [
            "Use PYTHONPATH=/path/to/worktree/src instead of pip install -e",
            "Example: PYTHONPATH=.claude/worktrees/my-feature/src pytest tests/",
            "The editable install is system-wide — it affects ALL processes, not just the current session",
        ],
        "tools_used": ["Bash"],
        "context_tags": ["pip.*-e", "pip.*--editable"],
        "activation_tier": "L1",
        "tool_trigger": ["Bash"],
        "speculative": 0,
        "success_count": 8,
        "confidence": 0.90,  # (8+1)/(8+0+2) = 0.90
    },
    {
        "id": _id("process_kill_safety"),
        "task_type": "process_kill_safety",
        "principle": "Always validate pgid > 1 before os.killpg(). int(AsyncMock().pid) == 1 in Python 3.12.",
        "steps": [
            "Validate pgid > 1 before any os.killpg() or os.kill() call",
            "In tests: always set mock_proc.pid = <explicit value>, never rely on default",
            "os.killpg(1, sig) == kill(-1, sig) == kill ALL user processes in container",
        ],
        "tools_used": ["Write", "Edit"],
        "context_tags": ["os.killpg", "os.kill(", "killpg(", "mock_proc.pid", "pgid"],
        "activation_tier": "L1",
        "tool_trigger": ["Write", "Edit"],
        "speculative": 0,
        "success_count": 5,
        "confidence": 0.86,
    },
    {
        "id": _id("git_concurrent_session_safety"),
        "task_type": "git_concurrent_session_safety",
        "principle": "Never git add . or commit to main when other sessions are active. Use worktrees.",
        "steps": [
            "Always use git worktrees for isolation when other sessions might be active",
            "Never use 'git add .' or 'git add -A' — stage files by name",
            "Never commit directly to main from a worktree — use feature branches",
            "Before committing: run 'git diff --cached --stat' and verify all files are yours",
        ],
        "tools_used": ["Bash"],
        "context_tags": ["git", "worktree", "concurrent", "safety", "commit"],
        "activation_tier": "L3",
        "tool_trigger": None,
        "speculative": 0,
        "success_count": 10,
        "confidence": 0.92,
    },
    {
        "id": _id("tmp_filesystem_limit"),
        "task_type": "tmp_filesystem_limit",
        "principle": "/tmp is a 512MB tmpfs. Filling it kills CC's shell across ALL sessions.",
        "steps": [
            "Never clone repos or write large files to /tmp/",
            "Use ~/tmp/ instead for large temporary files",
            "If /tmp fills up, it kills the shell for ALL concurrent CC sessions",
        ],
        "tools_used": ["Bash"],
        "context_tags": ["tmp", "filesystem", "disk", "safety"],
        "activation_tier": "L3",
        "tool_trigger": None,
        "speculative": 0,
        "success_count": 3,
        "confidence": 0.80,
    },
    {
        "id": _id("confidence_framework"),
        "task_type": "confidence_framework",
        "principle": "All non-trivial decisions need explicit confidence percentages with rationale.",
        "steps": [
            "Include explicit confidence percentages (e.g., '70% because X, Y, Z')",
            "Call out what you don't know — lead with unknowns",
            "State falsifiability criteria: 'This would be DISPROVEN if [observation]'",
            "Include regression markers: what to watch for if the fix is wrong",
            "No speculative changes without diagnosis confirmation",
        ],
        "tools_used": ["Write", "Edit"],
        "context_tags": ["planning", "decision", "confidence", "framework", "analysis"],
        "activation_tier": "L3",
        "tool_trigger": None,
        "speculative": 0,
        "success_count": 4,
        "confidence": 0.83,
    },
]


async def main(db_path: Path) -> None:
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await create_all_tables(db)
    await seed_data(db)
    await db.commit()

    seeded = 0
    for proc in SEED_PROCEDURES:
        proc_id = proc["id"]
        # Use raw SQL for upsert with all fields including success_count/confidence
        await db.execute(
            """INSERT INTO procedural_memory
               (id, task_type, principle, steps, tools_used, context_tags,
                activation_tier, tool_trigger, speculative, success_count,
                confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                 principle = excluded.principle,
                 steps = excluded.steps,
                 tools_used = excluded.tools_used,
                 context_tags = excluded.context_tags,
                 activation_tier = excluded.activation_tier,
                 tool_trigger = excluded.tool_trigger,
                 speculative = excluded.speculative""",
            (
                proc_id,
                proc["task_type"],
                proc["principle"],
                json.dumps(proc["steps"]),
                json.dumps(proc["tools_used"]),
                json.dumps(proc["context_tags"]),
                proc["activation_tier"],
                json.dumps(proc["tool_trigger"]) if proc["tool_trigger"] else None,
                proc["speculative"],
                proc["success_count"],
                proc["confidence"],
            ),
        )
        seeded += 1
        print(f"  {proc['activation_tier']} {proc['task_type']} (conf={proc['confidence']:.2f})")

    await db.commit()

    # Regenerate L1 trigger cache for the PreToolUse advisor hook
    from genesis.learning.procedural.trigger_cache import regenerate
    n_triggers = await regenerate(db)
    print(f"\nRegenerated trigger cache: {n_triggers} L1 triggers")

    await db.close()
    print(f"Seeded {seeded} procedures into {db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed procedural memory")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / "genesis" / "data" / "genesis.db",
        help="Path to genesis.db",
    )
    args = parser.parse_args()
    asyncio.run(main(args.db))
