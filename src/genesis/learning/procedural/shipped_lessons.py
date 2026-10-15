"""Shipped lessons: ``config/lessons/*.yaml`` seeded into procedural_memory.

Each file is checked with the tool-name normalizer and the lesson validators,
upserted as a reviewed ADVISORY lesson whose id comes from the file name, and a
lesson whose file was removed is deprecated. An invalid file is reported and
skipped (its earlier row is kept). Idempotent: a re-run changes nothing.

Run by ``scripts/seed_procedures.py`` (bootstrap) and at genesis-server start
(``seed_at_start``), so a lesson reaches the advisor on every deploy path, not
only a full update. It writes reviewed lessons with raw SQL from first-party
repo config, outside ``store_procedure*``, so the procedure gate's shadow emit
(tests/test_security/test_procedure_gate_coverage.py) does not apply to it.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import yaml

logger = logging.getLogger(__name__)

# The seed's namespace for deterministic ids (scripts/seed_procedures.py uses it
# for its procedures too); changing it would orphan every seeded row.
SEED_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


LESSONS_DIR = Path(__file__).resolve().parents[4] / "config" / "lessons"
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
    return str(uuid.uuid5(SEED_NAMESPACE, f"lesson:{path.stem}"))


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
    for key in ("steps", "patterns", "context_tags", "tool_trigger"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{key} must be a list of text")
    tools, cli_patterns, dropped = normalize_tool_trigger(data["tool_trigger"])
    if dropped or cli_patterns or not tools or tools != data["tool_trigger"]:
        # A shipped file names tools exactly; a shell command is Bash plus a pattern
        # the author writes, never one derived silently from a command word, and an
        # MCP tool is its full name, never a short name resolved from today's servers.
        raise ValueError(
            f"tool_trigger must hold exact, unique tool names (a shell command is Bash with a pattern): "
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


# The seed writes only what the file owns. Usage history (success and failure
# counts, confidence, version, workarounds, invocations) belongs to the runtime
# and stays in the database: a first seed starts at the explicit-teach values,
# and a re-seed never reads or rewrites those columns. A new principle drops the
# vector of the old text in the same statement, so the backfill re-embeds it.
_SHIPPED_UPSERT = """INSERT INTO procedural_memory
    (id, task_type, principle, steps, tools_used, context_tags, draft, created_at,
     activation_tier, tool_trigger, success_count, confidence, trigger_spec, lesson)
    VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'ADVISORY', ?, 1, 2.0 / 3, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
      task_type = excluded.task_type, principle = excluded.principle,
      principle_embedding = CASE WHEN procedural_memory.principle IS excluded.principle
        THEN procedural_memory.principle_embedding END,
      steps = excluded.steps, tools_used = excluded.tools_used,
      context_tags = excluded.context_tags, draft = 0, activation_tier = 'ADVISORY',
      tool_trigger = excluded.tool_trigger, trigger_spec = excluded.trigger_spec,
      lesson = excluded.lesson"""


def _first_seeded(prior: dict | None) -> str | None:
    """The row's ``authored_at`` when it is a readable timestamp, else None.

    An unreadable stored record is repaired from the file, never fatal, and a
    value that is not an ISO timestamp is replaced: copying it forward would
    leave the lesson held on every later seed.
    """
    try:
        value = json.loads(prior["lesson"])["authored_at"] if prior else None
        datetime.fromisoformat(value)
    except (ValueError, TypeError, KeyError, RecursionError):
        return None
    return value


async def seed_shipped_lessons(db: aiosqlite.Connection, directory: Path = LESSONS_DIR) -> list[str]:
    """Upsert every valid lesson file, deprecate removed ones; return the errors."""
    from genesis.db.crud import procedural
    from genesis.memory.provenance import ORIGIN_FIRST_PARTY

    if not directory.is_dir():
        return [f"{directory} is missing; shipped lessons left as they are"]
    files = sorted(directory.glob("*.yaml"))
    misnamed = sorted(directory.glob("*.yml"))
    errors: list[str] = [f"{p.name}: use the .yaml extension" for p in misnamed]
    now = datetime.now(UTC).isoformat()
    for path in files:
        try:
            lesson = _lesson_from_file(path)
        except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        prior = await procedural.get_by_id(db, lesson["id"])
        record = {
            "authored_via": "shipped",
            "authored_in": "foreground",
            "origin": ORIGIN_FIRST_PARTY,
            "reviewed": True,
            # Kept from the first seed, so a re-run changes nothing.
            "authored_at": _first_seeded(prior) or now,
        }
        await db.execute(
            _SHIPPED_UPSERT,
            (
                lesson["id"], lesson["task_type"], lesson["principle"], json.dumps(lesson["steps"]),
                json.dumps(lesson["tool_trigger"]), json.dumps(lesson["context_tags"]), now,
                json.dumps(lesson["tool_trigger"]), json.dumps(lesson["trigger_spec"]),
                json.dumps(record),
            ),
        )
        await db.commit()
        if prior and prior["deprecated"] and prior.get("deprecated_reason") == _RETIRED:
            # Only a row this seed retired comes back; an owner's deprecation stands.
            await procedural.update(db, lesson["id"], deprecated=0, deprecated_reason=None)
    # A shipped lesson whose file is gone is deprecated; one whose file is merely
    # invalid (including one renamed to .yml) keeps its earlier row.
    present = {_lesson_id(path) for path in files + misnamed}
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


async def seed_at_start(db: aiosqlite.Connection) -> None:
    """Seed the shipped lessons and rebuild the trigger cache (genesis-server start).

    A code-only deploy restarts the server but never runs bootstrap's seed, so
    without this a lesson replacing a retired hook would be missing until the
    next full update. Idempotent: on an unchanged tree it changes nothing. A
    failure is logged and re-raised, so the tracked task running it reports it.
    """
    from genesis.learning.procedural.trigger_cache import regenerate

    try:
        errors = await seed_shipped_lessons(db)
        triggers = await regenerate(db)
    except Exception:
        logger.exception("Shipped lesson seed at start failed")
        raise
    for error in errors:
        logger.warning("Shipped lesson skipped: %s", error)
    logger.info("Shipped lessons seeded; trigger cache holds %d triggers", triggers)
