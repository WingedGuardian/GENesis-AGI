"""The two JSON shapes a lesson row carries on ``procedural_memory``.

``trigger_spec`` says when the tool-time advisor fires: ``patterns`` (regexes,
any one matching is enough) and ``repeat`` (``session`` fires once per session,
``every`` on every match). ``lesson`` records how the row was authored, which
is what the origin gate and the review label read.

Both validators return the value unchanged when it is valid and raise
``ValueError`` with a readable reason when it is not, so a write path can refuse
a bad shape before it reaches the table. The advisor runs the patterns on every
matching tool call, so they are checked here for what a write CAN catch: they
compile, there are at most 8 of at most 200 characters, and none matches the
empty string (which would fire on every call). Compiling bounds nothing at
match time: a backtracking pattern such as ``(a+)+$`` passes here, and the
matcher is what has to bound its own run time.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from genesis.memory.provenance import ORIGIN_CLASSES, ORIGIN_FIRST_PARTY, ORIGIN_OWNER

MAX_PATTERNS = 8
MAX_PATTERN_CHARS = 200
REPEAT_MODES = frozenset({"session", "every"})
AUTHORED_VIA = frozenset({"mcp", "shipped"})
AUTHORED_IN = frozenset({"foreground", "dispatched"})

_SPEC_KEYS = frozenset({"patterns", "repeat"})
_LESSON_KEYS = frozenset({"authored_via", "authored_in", "origin", "reviewed", "authored_at"})
_TRUSTED_ORIGINS = (ORIGIN_OWNER, ORIGIN_FIRST_PARTY)


def _sql_set(values) -> str:
    return ", ".join(f"'{v}'" for v in sorted(values))


# is_live_lesson as a SQL condition, for queries that read procedural_memory
# directly. A row without a lesson passes. Anything else must be TEXT (SQLite's
# JSON functions also accept a BLOB, which Python never decodes) and a complete
# record (exactly the five keys, once each, the enums and types valid) before
# its origin and review state are read; any other value is held. CASE keeps
# json_each/json_extract away from a malformed value, which would fail the query,
# and COALESCE keeps a JSON null from turning a NOT IN into NULL (not held).
_KEYS = _sql_set(_LESSON_KEYS)
# Built from module constants only, so the S608 string-SQL warning does not apply.
LIVE_LESSON_SQL = (
    "CASE WHEN lesson IS NULL THEN 1 WHEN typeof(lesson) != 'text' THEN 0 "  # noqa: S608
    "WHEN instr(lesson, char(0)) > 0 THEN 0 "  # SQLite's JSON stops at a NUL; Python does not
    "WHEN NOT json_valid(lesson) THEN 0 "
    "WHEN json_type(lesson) != 'object' THEN 0 "
    "WHEN (SELECT COUNT(DISTINCT key) FROM json_each(lesson) WHERE key IN (" + _KEYS + ")) != 5 "
    "OR (SELECT COUNT(*) FROM json_each(lesson)) != 5 THEN 0 "
    "WHEN json_type(lesson, '$.reviewed') NOT IN ('true', 'false') "
    "OR json_type(lesson, '$.authored_at') != 'text' "
    "OR COALESCE(json_extract(lesson, '$.authored_via'), '') NOT IN ("
    + _sql_set(AUTHORED_VIA)
    + ") "
    "OR COALESCE(json_extract(lesson, '$.authored_in'), '') NOT IN (" + _sql_set(AUTHORED_IN) + ") "
    "OR COALESCE(json_extract(lesson, '$.origin'), '') NOT IN ("
    + _sql_set(ORIGIN_CLASSES)
    + ") THEN 0 "
    "ELSE json_extract(lesson, '$.origin') IN (" + _sql_set(_TRUSTED_ORIGINS) + ") "
    "OR json_type(lesson, '$.reviewed') = 'true' END"
)


def _exact_keys(value: object, keys: frozenset[str], what: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be an object, not {type(value).__name__}")
    missing = sorted(keys - value.keys())
    if missing:
        raise ValueError(f"{what} is missing {', '.join(missing)}")
    extra = sorted(value.keys() - keys)
    if extra:
        raise ValueError(f"{what} has unknown field(s): {', '.join(map(str, extra))}")
    return value


def validate_trigger_spec(spec: object) -> dict:
    """Return *spec* if it is a valid trigger_spec, else raise ValueError."""
    spec = _exact_keys(spec, _SPEC_KEYS, "trigger_spec")
    patterns = spec["patterns"]
    if not isinstance(patterns, list) or not patterns:
        raise ValueError("trigger_spec.patterns must be a list of at least one regex")
    if len(patterns) > MAX_PATTERNS:
        raise ValueError(f"trigger_spec.patterns may hold at most {MAX_PATTERNS} regexes")
    for pattern in patterns:
        if not isinstance(pattern, str):
            raise ValueError("each trigger_spec pattern must be a string")
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ValueError(
                f"a trigger_spec pattern is longer than {MAX_PATTERN_CHARS} characters"
            )
        try:
            compiled = re.compile(pattern)
        except (re.error, OverflowError, RecursionError) as exc:
            raise ValueError(f"trigger_spec pattern {pattern!r} does not compile: {exc}") from exc
        if compiled.search("") is not None:
            raise ValueError(f"trigger_spec pattern {pattern!r} matches every input")
    if not isinstance(spec["repeat"], str) or spec["repeat"] not in REPEAT_MODES:
        raise ValueError(f"trigger_spec.repeat must be one of {sorted(REPEAT_MODES)}")
    return spec


def validate_lesson(lesson: object) -> dict:
    """Return *lesson* if it is a valid lesson record, else raise ValueError."""
    lesson = _exact_keys(lesson, _LESSON_KEYS, "lesson")
    for field, allowed in (
        ("authored_via", AUTHORED_VIA),
        ("authored_in", AUTHORED_IN),
        ("origin", ORIGIN_CLASSES),
    ):
        if not isinstance(lesson[field], str) or lesson[field] not in allowed:
            raise ValueError(f"lesson.{field} must be one of {sorted(allowed)}")
    if not isinstance(lesson["reviewed"], bool):
        raise ValueError("lesson.reviewed must be true or false")
    try:
        datetime.fromisoformat(lesson["authored_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("lesson.authored_at must be an ISO-8601 timestamp") from exc
    return lesson


def _no_duplicate_keys(pairs: list) -> dict:
    # SQLite's json_extract reads the FIRST duplicate and json.loads the last,
    # so a duplicated key could be live to one and held to the other.
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")  # NaN/Infinity: json_valid rejects them


def is_live_lesson(raw: object) -> bool:
    """Whether a stored ``lesson`` value may reach a session.

    A lesson from outside (``external_untrusted``) is held until the owner has
    reviewed it. Fail-closed: a value that is not a complete lesson record
    (exactly its five keys, valid enums and types; ``authored_at`` need only be
    a string here), or that names no trusted origin and has no ``reviewed:
    true``, is held. Mirrored by ``LIVE_LESSON_SQL``.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constant)
        except (ValueError, RecursionError):  # too-deep JSON raises the latter
            return False
    if not isinstance(raw, dict) or set(raw) != _LESSON_KEYS:
        return False
    if not isinstance(raw["reviewed"], bool) or not isinstance(raw["authored_at"], str):
        return False
    for field, allowed in (
        ("authored_via", AUTHORED_VIA),
        ("authored_in", AUTHORED_IN),
        ("origin", ORIGIN_CLASSES),
    ):
        if not isinstance(raw[field], str) or raw[field] not in allowed:
            return False
    return raw["origin"] in _TRUSTED_ORIGINS or raw["reviewed"]
