"""The carrier-set drift lock (#2232).

Five "programs that carry another command" sets live across the guards and the
resolver — each derived for its own consumer, so they legitimately differ.
That is exactly why drift hid here: with no recorded reason per absence, an
intentional omission was indistinguishable from a forgotten one, and four of
the five pairwise edges were unlocked (the fifth — `destructive` ⊇
`_REPARSE_CARRIERS` — already has a parity test).

Option 2 from the issue: the sets stay separate, but every union member
absent from a set must carry a reason in that set's `_CARRIER_EXCLUDES` map
(or its named equivalent). This test fails when:

- a name appears in ANY set and is absent from another WITHOUT a recorded
  reason — the "forgotten vs deliberate" distinction, now machine-checked;
- an excludes map names a member that IS in the set (stale reason), or a name
  no set knows (typo would silently retire the check for that name).

Adding a carrier to any set therefore fails loudly at every sibling that
doesn't explain its absence — the property the issue asks to lock.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(_SCRIPTS))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve cls.__module__ via sys.modules
    spec.loader.exec_module(mod)
    return mod


sp = _load("shell_parse")
fsg = _load("full_suite_guard")
wcg = _load("worktree_cwd_guard")
dcg = _load("destructive_command_guard")

#: set label -> (member frozenset, absence-reason map)
SETS: dict[str, tuple[frozenset, dict]] = {
    "full_suite_guard._CARRIER_EXES": (fsg._CARRIER_EXES, fsg._CARRIER_EXCLUDES),
    "worktree_cwd_guard._CARRIER_NAMES": (wcg._CARRIER_NAMES, wcg._CARRIER_EXCLUDES),
    "shell_parse._RUN_CARRIERS": (sp._RUN_CARRIERS, sp._RUN_CARRIER_EXCLUDES),
    "shell_parse._REPARSE_CARRIERS": (sp._REPARSE_CARRIERS, sp._REPARSE_CARRIER_EXCLUDES),
    "destructive_command_guard._COMMAND_CARRIERS": (
        dcg._COMMAND_CARRIERS,
        dcg._COMMAND_CARRIER_EXCLUDES,
    ),
}

_UNION = frozenset().union(*(members for members, _ in SETS.values()))


def test_every_absence_carries_a_reason():
    """The four unlocked edges: a member of any set absent from a sibling
    must be in that sibling's excludes map with a non-empty reason."""
    missing: list[str] = []
    for label, (members, excludes) in SETS.items():
        for name in _UNION - members:
            reason = excludes.get(name)
            if not reason or not reason.strip():
                missing.append(f"{name} absent from {label} with no recorded reason")
    assert not missing, "undocumented carrier absences:\n" + "\n".join(missing)


def test_excludes_maps_are_not_stale():
    """An excludes entry for a name that IS in the set, or in NO set, means
    the map drifted — the lock would quietly stop covering that name."""
    for label, (members, excludes) in SETS.items():
        stale = set(excludes) & members
        unknown = set(excludes) - _UNION
        assert not stale, f"{label}: excludes reasons for members: {sorted(stale)}"
        assert not unknown, f"{label}: reasons for names in no set: {sorted(unknown)}"


def test_sets_are_nonoverlapping_exceptions_as_designed():
    """Pin today's deliberate inclusions so a silent new member is visible in
    review — the sets differ on purpose; growth should be loud."""
    # The edges the issue calls out as considered, kept from drifting back.
    assert "ssh" in wcg._CARRIER_NAMES and "ssh" not in sp._REPARSE_CARRIERS
    assert "xargs" in wcg._CARRIER_NAMES and "xargs" not in sp._REPARSE_CARRIERS
    assert "uvx" in fsg._CARRIER_EXES and "uvx" not in sp._RUN_CARRIERS
    # The already-locked fifth edge, asserted here too so this file is the
    # single place naming all five relationships.
    assert sp._REPARSE_CARRIERS <= dcg._COMMAND_CARRIERS
    assert dcg._NESTED_SHELLS <= dcg._COMMAND_CARRIERS
