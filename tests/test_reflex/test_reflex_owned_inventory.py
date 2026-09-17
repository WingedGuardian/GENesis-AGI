"""The reflex nerve's OWNED-EVENT inventory, and the two copies of it.

`task.failed` was emitted at WARNING while the reflex ingestor subscribed at
ERROR, and carried no `error_type` while the ingestor drops any event without
one — so the event cleared neither gate and reached nothing at all. Both halves
were individually reasonable and the event vanished between them.

WHAT PINS THAT FIX IS NOT HERE. It is the funnel tests, which drive the REAL
producer through a real bus into the REAL consumer and assert the event
arrives: `test_task_failed_funnel.py` for `task.failed` and
`tests/test_runtime/test_job_failure_funnel.py` for `job.failed`. Mutating
either axis at `surplus/dispatch.py` — the severity or the payload — fails
them, because they test ARRIVAL rather than the shape of a call.

An earlier version of this file tried to guarantee the contract for every
possible FUTURE emitter by AST-scanning the whole source tree on both axes.
That was the wrong shape and it is deleted. It modelled call semantics —
positional versus keyword literals, computed severities, wrapper defaults —
which is an open set, so each fix shipped the next round's defect: five review
findings, all about the scanner itself, and zero real defects caught. Worse,
its guarantee was hollow where it was unique: it graded `util/tasks.py`'s
emitter as PASSING both axes, while that emitter can raise and emit nothing at
all (issue #1970). It checked the shape of a call, not whether an event arrives.

The real guarantee for future emitters is a CHOKEPOINT that makes the contract
unconstructible — `emit_failure()`, issue #1969 — not a linter over a
convention. What remains here is the part that needs no semantic modelling at
all: an inventory of which event types the reflex arc owns, and a check that
the two hardcoded copies of that list agree.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "genesis"

# The reflex-owned event types, as this test believes them to be. Adding a type
# to `_REFLEX_OWNED_EVENT_TYPES` fails the first test below — deliberately. A
# new reflex-owned type needs its own FUNNEL test (real producer into real
# consumer); update this literal in the same change that adds one, so the
# obligation cannot be met by editing a list.
_EXPECTED_OWNED = frozenset({"task.failed", "job.failed"})


def _owned_from_ego() -> frozenset[str]:
    """`_REFLEX_OWNED_EVENT_TYPES` in `runtime/init/ego.py`, read from the AST.

    Parsed rather than imported: importing the ego module drags in the runtime,
    and the value is a literal in the assignment. Keeps the test hermetic — no
    services, no DB, no network — per the install-agnostic rule.
    """
    tree = ast.parse((_SRC / "runtime" / "init" / "ego.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if "_REFLEX_OWNED_EVENT_TYPES" not in [
            t.id for t in node.targets if isinstance(t, ast.Name)
        ]:
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Set):
                return frozenset(
                    e.value
                    for e in sub.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                )
    pytest.fail(
        "_REFLEX_OWNED_EVENT_TYPES not found in runtime/init/ego.py — the ego's "
        "reflex carve-out has moved; re-derive it rather than deleting this test"
    )


def _consumed_by_ingest() -> frozenset[str]:
    """The event types `ReflexIngestor.handle_event` actually consumes.

    A SECOND hardcoded copy of the owned list, in `reflex/ingest.py`. Read from
    the `event.event_type not in (...)` guard at the top of the handler.
    """
    tree = ast.parse((_SRC / "reflex" / "ingest.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ast.NotIn) for op in node.ops):
            continue
        for comparator in node.comparators:
            if not isinstance(comparator, ast.Tuple):
                continue
            values = [
                e.value
                for e in comparator.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if values and all("." in v for v in values):
                return frozenset(values)
    pytest.fail(
        "no `event.event_type not in (...)` guard found in reflex/ingest.py — the "
        "ingestor's intake filter has moved; re-derive it rather than deleting this test"
    )


def test_the_owned_event_inventory_is_what_we_think_it_is():
    """A new reflex-owned event type must not arrive silently.

    This fails the moment someone adds a third type — which is the point. A new
    owned type is gated OUT of the ego by design, so the reflex ingestor is its
    ONLY consumer, and it needs a funnel test proving its emit actually arrives.
    Updating this literal is the prompt to go write one.
    """
    assert _owned_from_ego() == _EXPECTED_OWNED, (
        "the reflex-owned event set changed. A newly owned type reaches NOTHING "
        "unless its emit clears the ingestor's ERROR floor AND carries error_type "
        "— write a funnel test for it (see test_task_failed_funnel.py), then "
        "update _EXPECTED_OWNED here."
    )


def test_the_ego_and_the_ingestor_agree_on_what_reflex_owns():
    """The two hardcoded copies of the owned list must not diverge.

    `runtime/init/ego.py` gates these types OUT of the ego; `reflex/ingest.py`
    decides which it consumes. Nothing else compares them. If the ego gates a
    type out that the ingestor does not take in, that event is refused by one
    subsystem and ignored by the other and reaches NOTHING — the original defect
    of this PR, one level up, and silent in exactly the same way.

    Two literals compared; no call semantics are modelled, which is what keeps
    this test out of the open-set trap the deleted scanner fell into.
    """
    ego, ingest = _owned_from_ego(), _consumed_by_ingest()
    assert ego == ingest, (
        f"ego gates out {sorted(ego)} but the ingestor consumes {sorted(ingest)} — "
        f"types in the difference {sorted(ego ^ ingest)} reach NOTHING. These two "
        "lists are duplicated by hand; issue #1969's emit_failure() chokepoint is "
        "where they stop being two."
    )
