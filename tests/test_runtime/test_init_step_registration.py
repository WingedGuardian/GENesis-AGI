"""Guardrail: an init step absent from ``_INIT_CHECKS`` always reports ``ok``.

``GenesisRuntime._run_init_step`` records a bootstrap verdict like this::

    attr = self._INIT_CHECKS.get(name)
    if attr and getattr(self, attr, None) is None:
        self._bootstrap_manifest[name] = "degraded"
    else:
        self._bootstrap_manifest[name] = "ok"

So a name that is NOT a key returns ``None`` from ``.get()``, the ``if`` is
False, and the step records ``"ok"`` — unconditionally, whether or not the init
did anything at all. The failure is silent and it points the wrong way: the
bootstrap manifest, which exists to report degradation, is most confident about
exactly the steps nobody registered.

Note the distinction this guard turns on: ``secrets`` and ``tool_registry`` are
PRESENT with an explicit ``None`` value, which is a deliberate "there is no
attribute that proves this one". A MISSING KEY is the defect; a ``None`` value
is a decision. This test therefore checks key membership, never truthiness.

MEASURED 2026-09-08: **11 of 34 stepped names (32%)** were missing, including
``ego``, ``guardian``, ``sentinel``, ``tasks`` and ``reflex``. Those are recorded
below as a frozen baseline rather than fixed in the same breath, because each
needs a per-subsystem judgement about which attribute proves initialisation, and
adding a check can flip a step to ``degraded`` and move live health alerts —
an operator decision, not a drive-by one. The baseline may SHRINK freely; it may
never GROW, which is what stops the class spreading while the existing debt is
paid down deliberately.
"""
from __future__ import annotations

import ast
from pathlib import Path

import genesis

_CORE = Path(genesis.__file__).parent / "runtime" / "_core.py"

# Pre-existing debt, MEASURED 2026-09-08. Remove a name when it gains a real
# _INIT_CHECKS entry. Adding a name here is not a fix — it is a decision to ship
# another subsystem whose bootstrap status is meaningless, and needs saying out
# loud in review.
_KNOWN_UNCHECKED: frozenset[str] = frozenset({
    "alert_drain",
    "cred_integrity",
    "cred_integrity_startup",
    "direct_session",
    "ego",
    "guardian",
    "guardian_monitoring",
    "infra_profile",
    "reflex",
    "sentinel",
    "tasks",
})


def _scan() -> tuple[set[str], set[str]]:
    """Return (_INIT_CHECKS keys, names passed to _run_init_step*)."""
    tree = ast.parse(_CORE.read_text())
    declared: set[str] = set()
    stepped: set[str] = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_INIT_CHECKS"
            and isinstance(node.value, ast.Dict)
        ):
            declared.update(
                k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("_run_init_step", "_run_init_step_async")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            stepped.add(node.args[0].value)
    return declared, stepped


def test_no_new_unchecked_init_steps():
    """A NEW init step must declare itself in ``_INIT_CHECKS``."""
    declared, stepped = _scan()
    unchecked = stepped - declared
    new = unchecked - _KNOWN_UNCHECKED

    assert not new, (
        "These init steps are not keys in _INIT_CHECKS, so _run_init_step records "
        f"'ok' for them unconditionally even when the init did nothing: {sorted(new)}.\n"
        "Add each to _INIT_CHECKS — mapped to the attribute that proves it "
        "initialised, or to None if genuinely nothing proves it (a deliberate "
        "declaration, which is what makes it reviewable)."
    )


def test_baseline_has_no_stale_entries():
    """The baseline must shrink as debt is paid — a stale entry hides a regression.

    Without this, fixing a subsystem would leave its name in ``_KNOWN_UNCHECKED``
    forever, and the slot would silently absorb a NEW unchecked step of the same
    name later.
    """
    declared, stepped = _scan()
    unchecked = stepped - declared
    stale = _KNOWN_UNCHECKED - unchecked

    assert not stale, (
        f"These names are in the _KNOWN_UNCHECKED baseline but are no longer "
        f"unchecked: {sorted(stale)}. Remove them from the baseline."
    )


def test_the_scan_actually_finds_both_sides():
    """Guard the guard: an empty scan would make both tests above vacuous."""
    declared, stepped = _scan()
    assert len(declared) > 15, f"_INIT_CHECKS scan found only {len(declared)} keys"
    assert len(stepped) > 25, f"_run_init_step* scan found only {len(stepped)} names"
