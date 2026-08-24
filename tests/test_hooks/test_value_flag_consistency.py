"""Consistency LOCK for the value-flag specs duplicated across the guard hooks.

The pr-merge / push / commit guards each carry their OWN copy of the gh/git
"value-flag" sets — the flags that consume the FOLLOWING argv token as their
value (so a scan for a positional/binding does not misread that value). Those
copies MUST stay identical across files: a flag added to one copy but not the
others is exactly the parse divergence that let the separated ``-R`` form
bypass every fail-closed merge gate (``gh pr -R o/r merge N --admin``, #1385
round-5).

This test is the drift TRIP-WIRE. Physical de-duplication (one shared spec) is
deliberately deferred to the gate-core extraction (S3) — which restructures
these files anyway; until then this lock makes the next silent drift a RED CI
check instead of a live bypass.

If this test FAILS: you changed ONE copy of a value-flag set. Update ALL copies
named in the failing assertion so they match again — they are intentionally
identical, not coincidentally so.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
# The guard hooks live in scripts/hooks/ (shell_parse, git_push_guard,
# pre_push_privacy_review) and scripts/ (review_enforcement_commit); the privacy
# hook also imports genesis.contribution.sanitize, so src/ must be importable.
for _p in ("scripts/hooks", "scripts", "src"):
    _abs = str(_ROOT / _p)
    if _abs not in sys.path:
        sys.path.insert(0, _abs)

import git_push_guard as gpg  # noqa: E402
import pre_push_privacy_review as ppr  # noqa: E402
import review_enforcement_commit as rec  # noqa: E402
import shell_parse as sp  # noqa: E402

# git global options that consume the FOLLOWING token as their value. Every copy
# below MUST equal this canonical set (compared as a set — a copy may be a
# frozenset or a tuple; only membership matters).
_CANONICAL_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
)
# git push flags that consume the FOLLOWING token as their value.
_CANONICAL_PUSH_VALUE_FLAGS = frozenset(
    {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
)


def test_git_global_value_flags_identical_across_all_copies():
    """The four git-global value-flag copies must be byte-identical (as sets)."""
    copies = {
        "git_push_guard._GIT_GLOBAL_VALUE_FLAGS": gpg._GIT_GLOBAL_VALUE_FLAGS,
        "shell_parse._GIT_OPTS_WITH_ARG": sp._GIT_OPTS_WITH_ARG,
        "review_enforcement_commit._GIT_GLOBAL_VALUE_FLAGS": rec._GIT_GLOBAL_VALUE_FLAGS,
        "pre_push_privacy_review._GIT_GLOBAL_VALUE_OPTS": ppr._GIT_GLOBAL_VALUE_OPTS,
    }
    for name, spec in copies.items():
        members = set(spec)
        assert members == set(_CANONICAL_GIT_GLOBAL_VALUE_FLAGS), (
            f"{name} drifted from the canonical git-global value-flag set. "
            f"All copies MUST stay identical — update every one of "
            f"{sorted(copies)}. "
            f"Missing={set(_CANONICAL_GIT_GLOBAL_VALUE_FLAGS) - members}, "
            f"Extra={members - set(_CANONICAL_GIT_GLOBAL_VALUE_FLAGS)}"
        )


def test_push_value_flags_identical_across_all_copies():
    """The two git-push value-flag copies must be byte-identical (as sets)."""
    copies = {
        "git_push_guard._PUSH_VALUE_FLAGS": gpg._PUSH_VALUE_FLAGS,
        "pre_push_privacy_review._PUSH_VALUE_FLAGS": ppr._PUSH_VALUE_FLAGS,
    }
    for name, spec in copies.items():
        members = set(spec)
        assert members == set(_CANONICAL_PUSH_VALUE_FLAGS), (
            f"{name} drifted from the canonical git-push value-flag set. "
            f"All copies MUST stay identical — update every one of "
            f"{sorted(copies)}. "
            f"Missing={set(_CANONICAL_PUSH_VALUE_FLAGS) - members}, "
            f"Extra={members - set(_CANONICAL_PUSH_VALUE_FLAGS)}"
        )
