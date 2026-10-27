"""The validators for a lesson's trigger_spec and lesson record."""

import pytest

from genesis.learning.procedural.lesson_shape import validate_lesson, validate_trigger_spec

_LESSON = {
    "authored_via": "mcp",
    "authored_in": "foreground",
    "origin": "first_party",
    "reviewed": False,
    "authored_at": "2026-10-13T00:00:00+00:00",
}


def test_a_valid_trigger_spec_is_returned_unchanged():
    spec = {"patterns": [r"\bupdate\.sh\b", "systemd-run"], "repeat": "every"}
    assert validate_trigger_spec(spec) == spec


def test_a_valid_lesson_is_returned_unchanged():
    assert validate_lesson(_LESSON) == _LESSON


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        ("not a dict", "object"),
        ({"repeat": "every"}, "patterns"),
        ({"patterns": [], "repeat": "every"}, "at least one"),
        ({"patterns": ["a"] * 9, "repeat": "every"}, "at most 8"),
        ({"patterns": ["a" * 201], "repeat": "every"}, "200"),
        ({"patterns": ["("], "repeat": "every"}, "compile"),
        ({"patterns": [".*"], "repeat": "every"}, "every input"),
        ({"patterns": [""], "repeat": "every"}, "every input"),
        ({"patterns": ["update", "x?"], "repeat": "every"}, "every input"),
        ({"patterns": [3], "repeat": "every"}, "string"),
        ({"patterns": ["a"], "repeat": "always"}, "repeat"),
        ({"patterns": ["a"], "repeat": ["session"]}, "repeat"),
        ({"patterns": ["a"]}, "repeat"),
        ({"patterns": ["a"], "repeat": "every", "extra": 1}, "extra"),
    ],
)
def test_an_invalid_trigger_spec_is_refused_with_a_reason(spec, reason):
    with pytest.raises(ValueError, match=reason):
        validate_trigger_spec(spec)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"authored_via": "llm"}, "authored_via"),
        ({"authored_in": "cron"}, "authored_in"),
        ({"origin": "stranger"}, "origin"),
        ({"origin": {"x": 1}}, "origin"),
        ({"authored_via": ["mcp"]}, "authored_via"),
        ({"reviewed": "no"}, "reviewed"),
        ({"authored_at": "yesterday"}, "authored_at"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_an_invalid_lesson_is_refused_with_a_reason(change, reason):
    with pytest.raises(ValueError, match=reason):
        validate_lesson({**_LESSON, **change})


def test_a_lesson_missing_a_field_is_refused():
    partial = dict(_LESSON)
    del partial["origin"]
    with pytest.raises(ValueError, match="origin"):
        validate_lesson(partial)
