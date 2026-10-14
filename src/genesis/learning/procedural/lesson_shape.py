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

import re
from datetime import datetime

from genesis.memory.provenance import ORIGIN_CLASSES

MAX_PATTERNS = 8
MAX_PATTERN_CHARS = 200
REPEAT_MODES = frozenset({"session", "every"})
AUTHORED_VIA = frozenset({"mcp", "shipped"})
AUTHORED_IN = frozenset({"foreground", "dispatched"})

_SPEC_KEYS = frozenset({"patterns", "repeat"})
_LESSON_KEYS = frozenset({"authored_via", "authored_in", "origin", "reviewed", "authored_at"})


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
        except re.error as exc:
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
