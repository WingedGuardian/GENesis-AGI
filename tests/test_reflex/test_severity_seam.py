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


def _is_emit_of(node: ast.AST, event_type: str) -> bool:
    """Is *node* an emit-family call carrying *event_type*?

    THE SINGLE discriminator for both axes. They had two, and the two diverged
    twice inside one PR — first the payload axis read only POSITIONAL event-type
    literals (grading 1 of 3 sites while reporting clean), then the severity axis
    silently DROPPED any site whose severity was not a literal (invisible to the
    floor test but graded by the payload one). Each fix was correct and left the
    other half free to drift again, which is what a shared helper removes: the
    two scanners can no longer grade different subsets, because they no longer
    each decide what a site is.
    """
    if not isinstance(node, ast.Call):
        return False
    literals = [
        a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
    ]
    kw_literals = [
        kw.value.value
        for kw in node.keywords
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
    ]
    if event_type not in literals + kw_literals:
        return False
    # An emit carries a severity in SOME spelling; its VALUE is what each axis
    # then grades (or reports as ungradeable), never what admits the site here.
    return any(isinstance(a, ast.Attribute) and a.attr in _ORDER for a in node.args) or any(
        kw.arg in ("severity", "severity_str") for kw in node.keywords
    )


def _emit_sites(event_type: str):
    """Yield ``(file, call)`` for every emit site of *event_type*."""
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - not our concern here
            continue
        for node in ast.walk(tree):
            if _is_emit_of(node, event_type):
                yield str(path.relative_to(_SRC)), node


_UNGRADEABLE = "UNGRADEABLE"


def _emitted_severities(event_type: str) -> list[tuple[str, str]]:
    """Every (file, Severity.X) an `emit`-family call uses for *event_type*.

    A site whose severity is not a literal is reported as ``UNGRADEABLE`` rather
    than skipped: skipping it made the floor test blind to exactly the emit a
    future author is most likely to write (a computed severity), while the
    per-type vacuity guard stayed satisfied by the other sites.
    """
    return [(where, _graded_severity(node)) for where, node in _emit_sites(event_type)]


def _graded_severity(call: ast.Call) -> str:
    """The literal severity this emit uses, or ``UNGRADEABLE``.

    Split out for the same reason as `_sets_error_type`: so the rejecting cases
    can be graded against constructed calls. No live site uses a computed
    severity, so the scan alone could never show that this discriminates.
    """
    sev = None
    for a in call.args:
        if isinstance(a, ast.Attribute) and a.attr in _ORDER:
            sev = a.attr
    for kw in call.keywords:
        if kw.arg == "severity_str" and isinstance(kw.value, ast.Constant):
            sev = kw.value.value
        elif kw.arg == "severity" and isinstance(kw.value, ast.Attribute):
            sev = kw.value.attr
    return sev if sev in _ORDER else _UNGRADEABLE


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
            if sev == _UNGRADEABLE:
                offenders.append(
                    f"{where} emits {event_type!r} with a NON-LITERAL severity — this "
                    "test cannot prove it clears the reflex subscriber floor. Use a "
                    "literal Severity, or teach this scanner to resolve it; a site it "
                    "cannot read is not a site that passes"
                )
            elif _ORDER[sev] < _ORDER[floor]:
                offenders.append(
                    f"{where} emits {event_type!r} at {sev}, below the reflex "
                    f"subscriber floor {floor} — the event reaches NOTHING"
                )
    assert checked >= 2, f"expected at least two emit sites to grade, graded {checked}"
    assert not offenders, "\n".join(offenders)


def _payload_axis_sites(event_type: str) -> list[tuple[str, bool]]:
    """Every emit site for *event_type*, graded on the PAYLOAD axis.

    Returns ``(file, ok)`` per site: ``ok`` iff the call spreads
    ``**failure_details(...)`` (the chokepoint that sets ``error_type`` from
    the exception) or passes an explicit ``error_type=`` keyword. The
    admission contract has TWO axes: severity ≥ the subscriber floor (test
    above) and ``error_type`` present in the details — `reflex/ingest.py`
    drops the event at ``if not error_type: return`` one branch after the
    floor.

    Site selection is `_is_emit_of` — shared with the severity axis, so the two
    can no longer grade different subsets. Returning graded sites rather than
    offenders is the other half: it lets the test assert the matcher SAW
    something per event type, so matching nothing can never read as passing.
    """
    return [(where, _sets_error_type(node)) for where, node in _emit_sites(event_type)]


def _sets_error_type(call: ast.Call) -> bool:
    """Does this emit call produce a payload carrying ``error_type``?

    Split out of the scan so it can be graded against constructed calls rather
    than only against whatever the tree happens to contain today — the tests
    below exercise the rejecting cases, which no live call site has (the whole
    point of a guard is the shape nobody has written YET).

    Two admission forms, and only two:
      * an explicit ``error_type=`` keyword;
      * ``**failure_details(exc=<not None>)`` — the chokepoint's EXCEPTION
        form. ``failure_details`` alone is NOT enough: its reason-only form
        returns ``error_reason`` and no ``error_type``, so spreading
        ``failure_details(reason=…)`` builds exactly the payload the ingestor
        drops (CodeRabbit, #1941).
    """
    for kw in call.keywords:
        if kw.arg == "error_type":
            # The runtime gate is `if not error_type: return` — TRUTHINESS, not
            # presence. A statically-falsy literal (None, "") builds exactly the
            # payload the ingestor drops, so presence-of-key would pass a site
            # that cannot work. Non-constant values are accepted: this scanner
            # cannot evaluate them, and refusing every computed value would flag
            # the correct `type(exc).__name__` spelling every live site uses.
            return not (isinstance(kw.value, ast.Constant) and not kw.value.value)
        if kw.arg is None and isinstance(kw.value, ast.Call):
            f = kw.value.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name != "failure_details":
                continue
            if any(
                d.arg == "exc" and not (isinstance(d.value, ast.Constant) and d.value.value is None)
                for d in kw.value.keywords
            ):
                return True
    return False


def test_every_reflex_owned_emit_payload_can_pass_the_admission_gate():
    """The SECOND axis of the admission contract, which #1941's first cut
    missed: an emitter with the right severity and a bare payload cleared the
    floor and was then dropped at the ingestor's ``error_type`` gate — the
    seam test above stayed green while the real event still reached nothing.

    VERIFY-RED (both watched): removing ``**failure_details(exc=exc)`` from
    the task.failed emit in `surplus/dispatch.py` fails this test with the
    file named; so does gutting the ``job.failed`` payload in
    `runtime/_job_health.py` (the case the first matcher was blind to).
    """
    offenders: list[str] = []
    for event_type in sorted(_reflex_owned_event_types()):
        sites = _payload_axis_sites(event_type)
        # Guard the guard: a matcher that saw no emit site for an owned event
        # type is blind, not clean — the exact fail-open this test shipped
        # with (0 job.failed sites graded read as a pass).
        assert sites, (
            f"payload axis graded NO emit site for {event_type!r} — either the "
            "event is dead or this matcher no longer recognises how it is "
            "emitted; both need a human, neither is a pass"
        )
        for where, ok in sites:
            if not ok:
                offenders.append(
                    f"{where} emits {event_type!r} without routing its payload "
                    "through failure_details (or an explicit error_type=) — the "
                    "event clears the severity floor and is then dropped at "
                    "reflex/ingest.py's admission gate, reaching NOTHING"
                )
    assert not offenders, "\n".join(offenders)


def _grade(src: str) -> bool:
    """Grade a single constructed emit call with the real predicate."""
    call = ast.parse(src).body[0].value
    assert isinstance(call, ast.Call), "fixture must build a Call node"
    return _sets_error_type(call)


class TestAdmissionPredicate:
    """The predicate's REJECTING cases, which no live call site exercises.

    Every emitter in the tree today passes, so the scan above cannot show that
    the predicate rejects anything — a matcher that accepted everything would
    look identical. These grade constructed calls so the guard is proven to
    discriminate, not just to be satisfied.
    """

    def test_accepts_the_exception_form_of_the_chokepoint(self):
        assert _grade('emit(S.ERROR, "task.failed", **failure_details(exc=exc))')

    def test_accepts_an_explicit_error_type(self):
        assert _grade('emit(S.ERROR, "task.failed", error_type=type(e).__name__)')

    def test_REJECTS_the_reason_only_form(self):
        """`failure_details(reason=…)` returns error_reason and NO error_type,
        so spreading it builds precisely the payload the ingestor drops."""
        assert not _grade('emit(S.ERROR, "task.failed", **failure_details(reason=r))')

    def test_REJECTS_an_explicitly_none_exception(self):
        """`exc=None` takes the reason branch inside the chokepoint."""
        assert not _grade('emit(S.ERROR, "task.failed", **failure_details(exc=None))')

    def test_REJECTS_a_bare_payload(self):
        """The original defect: task_id/task_type and nothing else."""
        assert not _grade('emit(S.ERROR, "task.failed", task_id=t.id)')

    def test_REJECTS_a_lookalike_helper(self):
        """A different function spread into the call is not the chokepoint."""
        assert not _grade('emit(S.ERROR, "task.failed", **other_details(exc=exc))')

    def test_REJECTS_a_statically_falsy_error_type(self):
        """The runtime gate is truthiness (`if not error_type`), not presence.

        The continuation case for the `error_type=` admission form — the same
        shape the `exc=None` test covers for the other form. Both of this
        file's earlier matcher defects were a check that admitted a value the
        runtime rejects; this is that class's fourth member.
        """
        assert not _grade('emit(S.ERROR, "task.failed", error_type=None)')
        assert not _grade('emit(S.ERROR, "task.failed", error_type="")')
        # …but a computed value must still pass: it is what every live site uses.
        assert _grade('emit(S.ERROR, "task.failed", error_type=type(exc).__name__)')


def _call(src: str) -> ast.Call:
    call = ast.parse(src).body[0].value
    assert isinstance(call, ast.Call), "fixture must build a Call node"
    return call


class TestBothAxesSelectTheSameSites:
    """The two axes must agree on WHAT a site is, and each must say so when it
    cannot grade one.

    Every defect this file has had was the two scanners disagreeing: first the
    payload axis saw only positional event-type literals (1 of 3 sites, clean
    pass); then the severity axis silently dropped any site whose severity was
    not a literal (invisible to the floor test, graded by the payload one).
    Both are now selected by one `_is_emit_of`, and these grade the shapes no
    live site uses — which is the only way to show the selection discriminates.
    """

    def test_selects_either_event_type_spelling(self):
        assert _is_emit_of(_call('emit(S.ERROR, "task.failed", x=1)'), "task.failed")
        assert _is_emit_of(
            _call('emit_sync(severity_str="ERROR", event_type="task.failed")'), "task.failed"
        )

    def test_does_not_select_a_non_emit_mentioning_the_name(self):
        """`reflex/ingest.py` compares against the literal; that is not an emit.

        `_is_emit_of` takes any AST node precisely so a non-Call cannot be
        mistaken for a site, so these pass the raw node rather than forcing a
        Call — the membership test below parses to `ast.Compare`.
        """
        membership = ast.parse('x not in ("task.failed", "job.failed")').body[0].value
        assert isinstance(membership, ast.Compare), "fixture must build the non-Call shape"
        assert not _is_emit_of(membership, "task.failed")
        # A Call naming it but carrying no severity is not an emit either.
        assert not _is_emit_of(_call('log.info("task.failed happened")'), "task.failed")

    def test_a_computed_severity_is_UNGRADEABLE_not_skipped(self):
        """Skipping it made the floor test blind to the emit a future author is
        most likely to write, while the per-type vacuity guard stayed satisfied
        by the other sites."""
        assert _graded_severity(_call('emit(severity=sev_var, event_type="task.failed")')) == (
            _UNGRADEABLE
        )
        assert _graded_severity(
            _call('emit_sync(severity_str=level, event_type="task.failed")')
        ) == (_UNGRADEABLE)

    def test_a_literal_severity_grades_normally(self):
        assert _graded_severity(_call('emit(S.WARNING, "task.failed")')) == "WARNING"
        assert _graded_severity(_call('emit_sync(severity_str="ERROR", x=1)')) == "ERROR"
