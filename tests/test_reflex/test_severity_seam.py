"""The reflex nerve only receives what is emitted at or above its floor.

`task.failed` was emitted at WARNING while the reflex ingestor subscribed at
ERROR, so the one subscriber the event exists for never saw it — and the ego
gate correctly refused it as reflex-owned, so it reached nothing at all. A
silent seam: both halves were individually reasonable and the event vanished
between them.

These tests pin the SEAM rather than either line, so the next reflex-owned
event type is covered without anyone remembering to add a case.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "genesis"

# Severity is an ordered enum; the reflex subscriber's floor is what matters.
_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


def _reflex_subscriber_floor() -> str:
    """The `min_severity=` the reflex ingestor actually subscribes with.

    Read from the AST rather than imported: importing `reflex.ingest` drags in
    the event bus and a DB connection, and the value we need is a literal in the
    call. Parsing keeps the test hermetic (no services, no network) per the
    install-agnostic rule.
    """
    tree = ast.parse((_SRC / "reflex" / "ingest.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "subscribe"):
            continue
        for kw in node.keywords:
            if kw.arg == "min_severity" and isinstance(kw.value, ast.Attribute):
                return kw.value.attr
    pytest.fail(
        "no `subscribe(..., min_severity=Severity.X)` call found in reflex/ingest.py — "
        "the seam this test guards has moved; re-derive it rather than deleting the test"
    )


def _emitted_severities(event_type: str) -> list[tuple[str, str]]:
    """Every (file, Severity.X) an `emit`-family call uses for *event_type*.

    Matches the event type as a positional string literal anywhere in the call,
    and takes the severity from either a positional `Severity.X` attribute or a
    `severity_str="X"` keyword — both spellings are live in this repo.
    """
    found: list[tuple[str, str]] = []
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - not our concern here
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            literals = [
                a.value
                for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
            kw_literals = [
                kw.value.value
                for kw in node.keywords
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
            ]
            if event_type not in literals + kw_literals:
                continue
            sev = None
            for a in node.args:
                if isinstance(a, ast.Attribute) and a.attr in _ORDER:
                    sev = a.attr
            for kw in node.keywords:
                if kw.arg == "severity_str" and isinstance(kw.value, ast.Constant):
                    sev = kw.value.value
                elif kw.arg == "severity" and isinstance(kw.value, ast.Attribute):
                    sev = kw.value.attr
            if sev is not None:
                found.append((str(path.relative_to(_SRC)), sev))
    return found


def _reflex_owned_event_types() -> set[str]:
    """`_REFLEX_OWNED_EVENT_TYPES`, read from the constant, never copied.

    A hand-copied list here would be wrong the moment a type is added — which is
    the exact failure mode the test exists to prevent, one level up.
    """
    tree = ast.parse((_SRC / "runtime" / "init" / "ego.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_REFLEX_OWNED_EVENT_TYPES" not in names:
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Set):
                return {
                    e.value
                    for e in sub.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
    pytest.fail("_REFLEX_OWNED_EVENT_TYPES not found in runtime/init/ego.py")


def test_the_owned_event_set_is_not_empty():
    """Guard the guard: an empty set would make every assertion below vacuous."""
    owned = _reflex_owned_event_types()
    assert owned, "no reflex-owned event types parsed — the rest of this file proves nothing"
    assert "task.failed" in owned, f"expected task.failed among {owned}"


def test_every_reflex_owned_event_is_emitted_at_or_above_the_subscriber_floor():
    """The seam itself, for the whole CLASS of reflex-owned events.

    A reflex-owned event is gated OUT of the ego by design, so the reflex
    ingestor is its only consumer. Emitted below that subscriber's floor it
    reaches nothing at all — no error, no log, no signal row.

    VERIFY-RED: restoring `Severity.WARNING` on the `task.failed` emit in
    `surplus/dispatch.py` fails this test with the file and severity named.
    """
    floor = _reflex_subscriber_floor()
    assert floor in _ORDER, f"unrecognised subscriber floor {floor!r}"

    offenders: list[str] = []
    checked = 0
    for event_type in sorted(_reflex_owned_event_types()):
        emits = _emitted_severities(event_type)
        assert emits, (
            f"no emit found for reflex-owned {event_type!r} — either it is dead, or this "
            "test's matcher no longer recognises how it is emitted; both need a human"
        )
        for where, sev in emits:
            checked += 1
            if _ORDER[sev] < _ORDER[floor]:
                offenders.append(
                    f"{where} emits {event_type!r} at {sev}, below the reflex "
                    f"subscriber floor {floor} — the event reaches NOTHING"
                )
    assert checked >= 2, f"expected at least two emit sites to grade, graded {checked}"
    assert not offenders, "\n".join(offenders)
