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

import pytest

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
#
# MEMBERSHIP IS DERIVED FROM THE INSTALLED BINARY, never read off `git -h` —
# that usage line prints only ATTACHED forms and omits `--attr-source` and
# `--shallow-file` entirely, so it cannot tell you a SEPARATED form is accepted,
# which is the only property this set encodes.
#
# The derivation is `test_the_table_covers_every_option_the_installed_git_consumes`
# below, and it is CHEAP — it is the same probe run at module scope there.
# An earlier revision of this comment asserted that no machine-readable
# enumeration was possible and that members could only be added "as they are
# measured" one incident at a time. That was FALSE, and it was load-bearing: it
# is what licensed stopping at the two options already suspected, while
# `--shallow-file` sat unlisted and let a force-publish and a worktree removal
# past their guards. Sweeping `strings -a $(command -v git)` for candidates and
# probing each takes ~23s for ~750 candidates (MEASURED here, git 2.43) and
# returns the whole set.
#
# `--super-prefix` is RETAINED although git 2.43 rejects it as unknown: the set
# is version-dependent (that option was removed, `--attr-source` and
# `--shallow-file` added, by different releases), and an entry for an option a
# given git lacks is inert — that git refuses the command outright, so nothing
# runs. This is why the derived test asserts a SUBSET rather than equality.
_CANONICAL_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--config-env",
        "--attr-source",
        "--shallow-file",
    }
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


# ── Derived expectation: the table must cover what the INSTALLED git consumes ──
#
# The consistency test above locks the four copies to each other. It cannot see
# whether the set they agree on is RIGHT — all four were identically wrong about
# `--config-env`, `--attr-source` and `--shallow-file`, and stayed so until a
# reviewer constructed a bypass. This test closes that: it asks the installed
# git which global options consume a following token, and fails if the table
# skips one of them alone.
#
# Direction is SUBSET, not equality, and deliberately so. The table may legally
# carry members this git does not have (`--super-prefix` was removed; an entry
# for an absent option is inert because git refuses the command outright). What
# must never happen is the reverse — git eating a value for an option the walk
# steps over alone, which puts that value in the verb slot and hides the real
# subcommand from every guard built on `git_subcommand_index`.
#
# A git release that ADDS a value-consuming global turns this RED rather than
# opening a silent bypass. That is the intended failure direction: the table is
# a claim about another project's CLI, and this is the only thing that makes the
# claim falsifiable.


def _installed_git_value_consuming_globals() -> set[str]:
    """Global options the installed git consumes a SEPARATE token for.

    Candidates come from the binary's own strings rather than `git -h`, which
    prints only ATTACHED forms and omits several of these entirely. `git
    version` is the marker because it needs no repository, no config and no
    network, so this runs the same way on a fresh clone and on CI.

    An option counts as a consumer when it is REJECTED bare but ACCEPTED with a
    value — the marker still printing proves git ate the value and then ran the
    verb. Measured ~23s for ~750 candidates.
    """
    import re
    import shutil
    import subprocess

    git = shutil.which("git")
    strings_bin = shutil.which("strings")
    if not git or not strings_bin:  # pragma: no cover - environment dependent
        pytest.skip("needs git and strings(1) to derive the expected set")

    blob = subprocess.run(
        [strings_bin, "-a", git], capture_output=True, text=True, timeout=120
    ).stdout
    candidates = sorted({m for m in re.findall(r"--[a-z][a-z0-9-]{2,30}", blob)})
    candidates += ["-" + c for c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"]

    # Value shapes wide enough that each option TYPE has a valid candidate: a
    # path, a config assignment, a tree-ish, a git dir, a directory.
    values = ("/tmp/x", "user.name=HOME", "HEAD", ".git", ".")

    def marker_ran(argv: list[str]) -> bool:
        p = subprocess.run(["git", *argv], capture_output=True, text=True, timeout=30)
        return p.returncode == 0 and p.stdout.startswith("git version")

    consumers: set[str] = set()
    for opt in candidates:
        if marker_ran([opt, "version"]):
            continue  # accepted bare -> takes no separate value
        for value in values:
            if marker_ran([opt, value, "version"]):
                consumers.add(opt)
                break
    return consumers


def test_the_table_covers_every_option_the_installed_git_consumes():
    """Every value-consuming global on THIS git must be in the canonical set.

    Guard-the-guard: the derivation must find the options we already know are
    consumers. A probe that returns nothing would make the subset assertion
    below vacuously true, which is exactly how a derived test goes quiet.
    """
    measured = _installed_git_value_consuming_globals()

    assert {"-C", "-c"} <= measured, (
        "the derivation found neither -C nor -c, which this git certainly "
        f"consumes — the probe is broken, not the table. Measured: {sorted(measured)}"
    )

    missing = measured - set(_CANONICAL_GIT_GLOBAL_VALUE_FLAGS)
    assert not missing, (
        "the installed git consumes a following token for these options, and "
        f"the verb walk steps over them ALONE: {sorted(missing)}. That puts the "
        "option's VALUE in the subcommand slot, so every guard keyed on the "
        "subcommand stops seeing the real operation. Add them to "
        "_CANONICAL_GIT_GLOBAL_VALUE_FLAGS and to all four copies it locks."
    )
