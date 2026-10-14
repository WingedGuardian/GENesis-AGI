#!/usr/bin/env python3
"""Seed procedural_memory with known battle-tested procedures.

These procedures are extracted from existing hooks, memory files, and
incident-driven lessons. They start at their natural activation tier
(CORE for hook-backed procedures, LIBRARY for session-level knowledge).

Run once, or re-run safely (uses upsert with deterministic IDs).

It also seeds the SHIPPED LESSONS in ``config/lessons/*.yaml`` (one per file;
schema in that directory's README): each file is checked with the tool-name
normalizer and the lesson validators, upserted as a reviewed ADVISORY lesson
whose id comes from the file name, and a lesson whose file was removed is
deprecated. An invalid file is reported and skipped (its earlier row is kept).

Usage:
    python scripts/seed_procedures.py [--db PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import yaml

from genesis.db.schema import create_all_tables, seed_data

# Deterministic UUIDs from namespace + task_type for idempotent re-runs
_NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def _id(task_type: str) -> str:
    return str(uuid.uuid5(_NS, task_type))


SEED_PROCEDURES = [
    {
        "id": _id("youtube_content_fetch"),
        "task_type": "youtube_content_fetch",
        "principle": (
            "Fetch YouTube videos with the genesis web_fetch MCP tool: it returns "
            "metadata and the transcript. CC's built-in WebFetch gets only the page shell."
        ),
        "steps": [
            "Call mcp__genesis-health__web_fetch with the video URL (urls=[...] for several)",
            "Read the Transcript source line: which caption track was used (and its provenance)",
            "If the result has youtube_error, report that error; the rest is only the page title and description",
            "Foreground sessions only, if web_fetch is unavailable: the youtube-fetch skill's yt-dlp via Bash fallback",
        ],
        "tools_used": ["mcp__genesis-health__web_fetch"],
        "context_tags": ["youtube", "video", "transcript", "content-fetch"],
        "activation_tier": "CORE",
        "tool_trigger": ["WebFetch"],
        "draft": 0,
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
        "activation_tier": "LIBRARY",
        "tool_trigger": None,
        "draft": 0,
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
        "activation_tier": "CORE",
        "tool_trigger": ["Bash"],
        "draft": 0,
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
        "activation_tier": "CORE",
        "tool_trigger": ["Write", "Edit"],
        "draft": 0,
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
        "activation_tier": "LIBRARY",
        "tool_trigger": None,
        "draft": 0,
        "success_count": 10,
        "confidence": 0.92,
    },
    {
        "id": _id("tmp_filesystem_limit"),
        "task_type": "tmp_filesystem_limit",
        "principle": "CC sessions use ~/.genesis/cc-tmp/ via TMPDIR — never override TMPDIR in hooks or scripts. cc-tmp is a quota-capped volume every session shares; large temp goes to ~/tmp.",
        "steps": [
            "Never clone repos or write large files to /tmp/ or ~/.genesis/cc-tmp/",
            "Use ~/tmp/ instead for large temporary files",
            "CC temp is redirected to ~/.genesis/cc-tmp/ via TMPDIR env var",
            "genesis-tmp-watchgod guards whole disks and sweeps cc-tmp of what ENDED "
            "sessions left (untouched 7 days); it never deletes live work or kills a session",
            "Never override TMPDIR in hooks, scripts, or service files",
        ],
        "tools_used": ["Bash"],
        "context_tags": ["tmp", "filesystem", "disk", "safety", "tmpdir", "watchgod"],
        "activation_tier": "LIBRARY",
        "tool_trigger": None,
        "draft": 0,
        "success_count": 3,
        "confidence": 0.90,
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
            "No draft changes without diagnosis confirmation",
        ],
        "tools_used": ["Write", "Edit"],
        "context_tags": ["planning", "decision", "confidence", "framework", "analysis"],
        "activation_tier": "LIBRARY",
        "tool_trigger": None,
        "draft": 0,
        "success_count": 4,
        "confidence": 0.83,
    },
]


LESSONS_DIR = Path(__file__).resolve().parent.parent / "config" / "lessons"
_LESSON_KEYS = {"task_type", "principle", "steps", "tool_trigger", "patterns", "repeat"}
# Written only by this seed, so a row it retired is told apart from one the owner deprecated.
_RETIRED = "shipped lesson file removed"


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a duplicated key instead of keeping the last one."""

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(k, deep=deep) for k, _ in node.value]
        if len(keys) != len(set(map(str, keys))):
            raise ValueError("a key appears twice")
        return super().construct_mapping(node, deep=deep)


def _lesson_id(path: Path) -> str:
    return _id(f"lesson:{path.stem}")


def _lesson_from_file(path: Path) -> dict:
    """One shipped lesson file as a seed row; ValueError says why it is invalid."""
    from genesis.learning.procedural.lesson_shape import validate_trigger_spec
    from genesis.learning.procedural.tool_names import normalize_tool_trigger

    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)  # noqa: S506 (a SafeLoader)
    if not isinstance(data, dict):
        raise ValueError("not a YAML mapping")
    missing = sorted(_LESSON_KEYS - data.keys())
    extra = sorted(data.keys() - _LESSON_KEYS - {"context_tags"})
    if missing or extra:
        raise ValueError(f"missing {missing}, unknown {extra}")
    for key in ("task_type", "principle"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"{key} must be non-empty text")
    for key in ("steps", "patterns", "context_tags"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{key} must be a list of text")
    tools, cli_patterns, dropped = normalize_tool_trigger(data["tool_trigger"])
    if dropped or cli_patterns or not tools:
        # A shipped file names tools exactly; a shell command is Bash plus a pattern
        # the author writes, never one derived silently from a command word.
        raise ValueError(
            f"tool_trigger must hold exact tool names (a shell command is Bash with a pattern): "
            f"{dropped or data['tool_trigger']}"
        )
    spec = validate_trigger_spec({"patterns": data["patterns"], "repeat": data["repeat"]})
    return {
        "id": _lesson_id(path),
        "task_type": data["task_type"],
        "principle": data["principle"],
        "steps": data["steps"],
        "tool_trigger": tools,
        "context_tags": data.get("context_tags", []),
        "trigger_spec": spec,
    }


async def seed_shipped_lessons(db: aiosqlite.Connection, directory: Path = LESSONS_DIR) -> list[str]:
    """Upsert every valid lesson file, deprecate removed ones; return the errors."""
    from genesis.db.crud import procedural
    from genesis.memory.provenance import ORIGIN_FIRST_PARTY

    if not directory.is_dir():
        return [f"{directory} is missing; shipped lessons left as they are"]
    files = sorted(directory.glob("*.yaml"))
    errors: list[str] = [f"{p.name}: use the .yaml extension" for p in sorted(directory.glob("*.yml"))]
    now = datetime.now(UTC).isoformat()
    for path in files:
        try:
            lesson = _lesson_from_file(path)
        except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        prior = await procedural.get_by_id(db, lesson["id"])
        prior_lesson = json.loads(prior["lesson"]) if prior and prior.get("lesson") else {}
        await procedural.upsert(
            db,
            id=lesson["id"],
            task_type=lesson["task_type"],
            principle=lesson["principle"],
            steps=lesson["steps"],
            tools_used=lesson["tool_trigger"],
            context_tags=lesson["context_tags"],
            created_at=now,
            draft=0,
            activation_tier="ADVISORY",
            tool_trigger=lesson["tool_trigger"],
            # A first seed starts at the explicit-teach values; a re-seed keeps the
            # usage history the row has gathered since.
            success_count=prior["success_count"] if prior else 1,
            confidence=prior["confidence"] if prior else 2 / 3,
            version=prior["version"] if prior else 1,
            trigger_spec=lesson["trigger_spec"],
            lesson={
                "authored_via": "shipped",
                "authored_in": "foreground",
                "origin": ORIGIN_FIRST_PARTY,
                "reviewed": True,
                # Kept from the first seed, so a re-run changes nothing.
                "authored_at": prior_lesson.get("authored_at", now),
            },
        )
        if prior and prior["principle"] != lesson["principle"]:
            # The stored vector describes the old text; the embedding backfill
            # re-embeds a row whose vector is NULL.
            await procedural.update(db, lesson["id"], principle_embedding=None)
        if prior and prior["deprecated"] and prior.get("deprecated_reason") == _RETIRED:
            # Only a row this seed retired comes back; an owner's deprecation stands.
            await procedural.update(db, lesson["id"], deprecated=0, deprecated_reason=None)
    # A shipped lesson whose file is gone is deprecated; one whose file is merely
    # invalid keeps its earlier row.
    present = {_lesson_id(path) for path in files}
    for row in await db.execute_fetchall(
        "SELECT id, lesson FROM procedural_memory WHERE lesson IS NOT NULL AND deprecated = 0"
    ):
        try:
            shipped = json.loads(row[1]).get("authored_via") == "shipped"
        except (ValueError, AttributeError, RecursionError):
            shipped = False
        if shipped and row[0] not in present:
            await procedural.update(db, row[0], deprecated=1, deprecated_reason=_RETIRED)
    await db.commit()
    return errors


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
                activation_tier, tool_trigger, draft, success_count,
                confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                 principle = excluded.principle,
                 steps = excluded.steps,
                 tools_used = excluded.tools_used,
                 context_tags = excluded.context_tags,
                 activation_tier = excluded.activation_tier,
                 tool_trigger = excluded.tool_trigger,
                 draft = excluded.draft""",
            (
                proc_id,
                proc["task_type"],
                proc["principle"],
                json.dumps(proc["steps"]),
                json.dumps(proc["tools_used"]),
                json.dumps(proc["context_tags"]),
                proc["activation_tier"],
                json.dumps(proc["tool_trigger"]) if proc["tool_trigger"] else None,
                proc["draft"],
                proc["success_count"],
                proc["confidence"],
            ),
        )
        seeded += 1
        print(f"  {proc['activation_tier']} {proc['task_type']} (conf={proc['confidence']:.2f})")

    await db.commit()

    lesson_errors = await seed_shipped_lessons(db)

    # Regenerate CORE/ADVISORY trigger cache for the PreToolUse advisor hook
    from genesis.learning.procedural.trigger_cache import regenerate
    n_triggers = await regenerate(db)
    print(f"\nRegenerated trigger cache: {n_triggers} CORE/ADVISORY triggers")

    await db.close()
    print(f"Seeded {seeded} procedures into {db_path}")
    # Last, so a caller keeping only the tail (bootstrap keeps two lines) still sees it.
    for error in lesson_errors:
        print(f"  SKIPPED shipped lesson {error}")
    if lesson_errors:
        print(f"SKIPPED {len(lesson_errors)} shipped lesson file(s); see above")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed procedural memory")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to genesis.db (default: the app's genesis_db_path() resolver)",
    )
    args = parser.parse_args()
    # Resolve via the app's authoritative resolver (honors $GENESIS_DB_PATH /
    # $GENESIS_REPO_ROOT) so a non-standard install seeds the DB the server reads.
    from genesis.env import genesis_db_path
    asyncio.run(main(args.db or genesis_db_path()))
