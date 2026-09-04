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


# ── cd-target semantics ──────────────────────────────────────────────────────
#
# The SECOND spec duplicated across these hooks: which `cd` targets are
# resolvable from command text alone. FOUR copies exist — git_push_guard,
# review_enforcement_commit, pre_push_privacy_review, and repo_routing_guard.
# They deliberately do NOT share a signature: the first two classify a RAW
# SEGMENT ("cd $W"), the third is reached through `_effective_cwd` with a whole
# command, and the fourth walks a token stream inline. So this locks SEMANTICS,
# not identity.
#
# WHY IT EXISTS: pre_push_privacy_review had no unresolvable concept at all. It
# joined the raw target onto the payload cwd, produced a path that cannot exist,
# and silently scanned nothing — a push it could not scope was indistinguishable
# from a clean one. The other copies had modelled this correctly for months and
# nothing compared them, so the gap was invisible.
#
# HOW THE CASE LIST IS BUILT — this is the load-bearing part. The constructs are
# GENERATED (prefix x target), not hand-listed. A hand-written list is authored
# AFTER making the copies agree, so it systematically contains the agreeing cases
# and omits every real divergence: a green lock then certifies a parity that does
# not hold. The first version of this lock had exactly that defect — 8 of 26
# probed constructs diverged and not one was in its list, including the case
# where the weakest copy silently resolved `( cd $W && git push )` to the payload
# cwd and scanned the WRONG REPOSITORY.
#
# Physical de-duplication stays deferred to the gate-core extraction (S3), per
# this module's header. Until then this makes the next divergence a RED CI check.
#
# repo_routing_guard is measured but NOT locked, and the reason is recorded so
# the exclusion is not mistaken for coverage: it has no unresolvable sentinel to
# compare against — it fabricates a directory unconditionally. It also BLOCKS
# (exit 2) rather than advising, so giving it one is a verdict change needing its
# own true-positive controls. Tracked separately; add it here once it grows a
# sentinel.

_CD_BASE = "/BASE"


def _absent_user() -> str:
    """A username the host's passwd db provably cannot resolve.

    The sweep needs a `~user` that stays UNRESOLVABLE. A hard-coded
    "nosuchuser" is a guess about the host: if such an account exists the case
    silently resolves and the lock reports a divergence that is really a
    property of the machine. Probing until one is absent makes the case
    deterministic wherever this suite runs, including CI.
    """
    import pwd

    for n in range(1000):
        cand = f"genesis_absent_user_{n}"
        try:
            pwd.getpwnam(cand)
        except KeyError:
            return cand
    raise AssertionError("no absent username found — the passwd db is implausible")


_ABSENT_USER = _absent_user()


@pytest.fixture(autouse=True)
def _hermetic_cd_environment(monkeypatch):
    """CDPATH changes what `cd relative/sub` means, and the copies read it
    differently, so a non-empty CDPATH in the developer's shell makes this lock
    fail as an "undocumented divergence" with no drift at all. The sweep is
    about the COPIES agreeing, not about the host, so pin the environment.
    """
    monkeypatch.delenv("CDPATH", raising=False)


_CD_TARGETS = [
    "/abs/path",
    "'single quoted'",
    '"double quoted"',
    "relative/sub",
    "~",
    "~/wt",
    f"~{_ABSENT_USER}/wt",
    "$W",
    '"$W"',
    "'$W'",
    "${W}",
    "$(pwd)/x",
    "`pwd`/x",
    "/a/*/b",
    "/a/b?",
    "/a/b[1]",
    "/a/{x}",
    "/a/(x)",
    "/a/<x>",
    "/a/b\\c",
    "/a b",
    "'/a b'",
    "-",
    "-P /x",
    "",
]
_CD_PREFIXES = ["cd {t}", "( cd {t}", "{{ cd {t}"]

# Divergences that are UNDERSTOOD AND ACCEPTED, each with the reason it is not a
# bug. The test asserts this table is EXACT in both directions: an undocumented
# divergence fails, and so does an entry that no longer diverges — otherwise the
# table rots into a permanent excuse list.
_KNOWN_CD_DIVERGENCES = {
    f"cd ~{_ABSENT_USER}/wt": (
        "ppr refuses a ~user the passwd db cannot resolve; the others return the "
        "literal, which then cannot exist. ppr is stricter and safer for an advisory."
    ),
    "cd '$W'": (
        "Single quotes suppress expansion, so the others correctly return a literal "
        "$W. ppr is handed a shlex-SPLIT token with the quotes already gone and "
        "cannot tell it from an unquoted $W, so it refuses. Contract limit, not a bug."
    ),
    "cd /a/b[1]": "git_push_guard's char set lacks [ ; rec and ppr both refuse.",
    "cd /a/{x}": "git_push_guard's char set lacks { ; rec and ppr both refuse.",
    "cd /a/(x)": "git_push_guard's char set lacks ( ; rec and ppr both refuse.",
    "cd /a/<x>": "git_push_guard's char set lacks < ; rec and ppr both refuse.",
    "cd /a/b\\c": (
        "shlex has ALREADY applied bash's escape semantics for ppr, so it resolves "
        "/a/bc — exactly the directory bash enters. The raw-segment copies cannot "
        "know that and refuse. ppr is more precise here, not divergent by accident."
    ),
}


# The FULL verdict each documented divergence produces, per module. Recorded so
# the lock below can assert equality rather than mere disagreement: a documented
# divergence that changes to a DIFFERENT still-divergent verdict, or a construct
# that vanishes from the generated sweep, both used to pass while the comments
# claimed they were caught.
#
# Regenerate deliberately, never to make a red test green: a change here is a
# statement that the copies' semantics moved on purpose.
_KNOWN_CD_VERDICT_MAPS = {
    f"cd ~{_ABSENT_USER}/wt": {
        "git_push_guard": f"'~{_ABSENT_USER}/wt'",
        "review_enforcement_commit": f"'~{_ABSENT_USER}/wt'",
        "pre_push_privacy_review": "UNRESOLVABLE",
    },
    "cd '$W'": {
        "git_push_guard": "'$W'",
        "review_enforcement_commit": "'$W'",
        "pre_push_privacy_review": "UNRESOLVABLE",
    },
    "cd /a/b[1]": {
        "git_push_guard": "'/a/b[1]'",
        "review_enforcement_commit": "UNRESOLVABLE",
        "pre_push_privacy_review": "UNRESOLVABLE",
    },
    "cd /a/{x}": {
        "git_push_guard": "'/a/{x}'",
        "review_enforcement_commit": "UNRESOLVABLE",
        "pre_push_privacy_review": "UNRESOLVABLE",
    },
    "cd /a/(x)": {
        "git_push_guard": "'/a/(x)'",
        "review_enforcement_commit": "UNRESOLVABLE",
        "pre_push_privacy_review": "UNRESOLVABLE",
    },
    "cd /a/<x>": {
        "git_push_guard": "'/a/<x>'",
        "review_enforcement_commit": "UNRESOLVABLE",
        "pre_push_privacy_review": "UNRESOLVABLE",
    },
    "cd /a/b\\c": {
        "git_push_guard": r"'/a/b\\c'",
        "review_enforcement_commit": "UNRESOLVABLE",
        "pre_push_privacy_review": "'/a/bc'",
    },
}


def _cd_verdict(mod, segment: str) -> str:
    """Classify through each copy's REAL call path, normalised for comparison.

    Routing matters: an earlier version of this adapter called ppr._cd_target
    directly, which production never does — so a fix living in _effective_cwd was
    invisible and the sweep reported a divergence that had already been closed.
    """
    unknown = getattr(mod, "_CWD_UNKNOWN", None) or getattr(mod, "_CWD_UNRESOLVED", None)
    assert unknown is not None, f"{mod.__name__} exposes no unresolvable sentinel"

    if mod is ppr:
        got = mod._effective_cwd(f"{segment} && git " + "push origin br", _CD_BASE)
        if got is unknown:
            return "UNRESOLVABLE"
        if got == _CD_BASE:
            return "not-a-cd"
        # ppr returns a RESOLVED cwd; the raw-segment copies return the TARGET.
        if isinstance(got, str) and got.startswith(_CD_BASE + "/"):
            got = got[len(_CD_BASE) + 1 :]
        return repr(got)

    got = mod._cd_target(segment)
    if got is unknown:
        return "UNRESOLVABLE"
    if got is None:
        return "not-a-cd"
    return repr(got)


def _sweep_cd_constructs() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for prefix in _CD_PREFIXES:
        for target in _CD_TARGETS:
            seg = prefix.format(t=target).rstrip()
            out[seg] = {m.__name__.split(".")[-1]: _cd_verdict(m, seg) for m in (gpg, rec, ppr)}
    return out


def test_cd_semantics_have_no_undocumented_divergence():
    """The lock. Every construct on which the copies disagree must be a KNOWN,
    reasoned divergence — anything else is drift, and drift here is how one copy
    silently resolves what another refuses."""
    swept = _sweep_cd_constructs()
    diverging = {seg: v for seg, v in swept.items() if len(set(v.values())) > 1}
    undocumented = {s: v for s, v in diverging.items() if s not in _KNOWN_CD_DIVERGENCES}
    assert not undocumented, (
        "cd-target semantics drifted across copies. Either make them agree, or add "
        f"an entry to _KNOWN_CD_DIVERGENCES stating WHY: {undocumented}"
    )


def test_every_known_divergence_still_produces_exactly_its_recorded_verdict():
    """The other direction, and genuinely EXACT this time.

    The previous version only detected CONVERGENCE, and skipped any key missing
    from the sweep (``if seg in swept``). So a documented divergence changing to
    a DIFFERENT still-divergent verdict passed, and a construct disappearing
    from the generated space passed — while the comments claimed both were
    caught. That is this lock's own failure mode, inside the lock.

    Asserting the full per-module map covers all three at once: a fixed
    divergence converges and fails, a drifted one mismatches and fails, and a
    removed construct is absent and fails.
    """
    swept = _sweep_cd_constructs()

    missing = [seg for seg in _KNOWN_CD_DIVERGENCES if seg not in swept]
    assert not missing, (
        "the generated construct space no longer produces these documented "
        f"divergences, so nothing is checking them: {missing}"
    )

    undocumented = [seg for seg in _KNOWN_CD_DIVERGENCES if seg not in _KNOWN_CD_VERDICT_MAPS]
    assert not undocumented, (
        "every entry in _KNOWN_CD_DIVERGENCES needs a recorded verdict map in "
        f"_KNOWN_CD_VERDICT_MAPS, else it is only half-locked: {undocumented}"
    )

    drifted = {
        seg: {"recorded": _KNOWN_CD_VERDICT_MAPS[seg], "actual": swept[seg]}
        for seg in _KNOWN_CD_DIVERGENCES
        if swept[seg] != _KNOWN_CD_VERDICT_MAPS[seg]
    }
    assert not drifted, (
        "a documented divergence no longer produces its recorded verdict. If a "
        "copy was FIXED, drop the entry from both tables; if the semantics moved "
        f"deliberately, update the recorded map and say why: {drifted}"
    )

    converged = [seg for seg in _KNOWN_CD_DIVERGENCES if len(set(swept[seg].values())) == 1]
    assert not converged, (
        f"these no longer diverge — remove them from _KNOWN_CD_DIVERGENCES: {converged}"
    )


def test_the_sweep_actually_covers_the_dangerous_shapes():
    """Guard-the-guard: a sweep that silently stopped generating the risky
    constructs would pass both tests above while checking nothing."""
    swept = _sweep_cd_constructs()
    for required in ("cd $W", "( cd $W", "{ cd $W", "cd -", "cd"):
        assert required in swept, f"construct space lost {required!r}"
    assert len(swept) >= 70, f"construct space collapsed to {len(swept)}"


def test_the_advisory_never_fabricates_a_path():
    """The specific regression. Joining an unresolved target onto the payload cwd
    yields a directory that cannot exist, and the resulting empty scan is
    indistinguishable from a clean one."""
    got = ppr._effective_cwd("W=/x; cd $W && git " + "push origin br", "/main")
    assert got is ppr._CWD_UNRESOLVED
    assert not isinstance(got, str), "a fabricated path is worse than an honest unknown"


def test_subshell_is_unresolvable_in_every_copy():
    """The worst shape in the class: `( cd /wt && git push )` scopes its cd, so a
    copy that misses it keeps the PAYLOAD cwd and scans a DIFFERENT repository —
    reporting clean about a tree that was never pushed."""
    for seg in ("( cd /wt", "{ cd /wt"):
        verdicts = {m.__name__.split(".")[-1]: _cd_verdict(m, seg) for m in (gpg, rec, ppr)}
        assert set(verdicts.values()) == {"UNRESOLVABLE"}, verdicts
