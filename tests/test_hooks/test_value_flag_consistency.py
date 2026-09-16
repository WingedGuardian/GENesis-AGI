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

import os
import shutil
import subprocess
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
    import os
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
    #
    # The config assignment names an env var WE define in the child rather than
    # borrowing an ambient one. `--config-env` resolves its value through the
    # environment and is REJECTED when the named variable is unset, which is
    # indistinguishable here from "this option takes no value" — so an ambient
    # name would silently drop `--config-env` out of the measured set on any
    # machine that does not export it, and the subset assertion would then pass
    # vacuously for exactly the option class this test exists to catch. The
    # guard-the-guard below checks `-C`/`-c` and would not notice.
    probe_env = {**os.environ, "GENESIS_PROBE_VALUE": "probe"}
    values = ("/tmp/x", "user.name=GENESIS_PROBE_VALUE", "HEAD", ".git", ".")

    def marker_ran(argv: list[str]) -> bool:
        p = subprocess.run(
            ["git", *argv], capture_output=True, text=True, timeout=30, env=probe_env
        )
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


_SUBCOMMAND_MARKER = "GENESIS_SUBCOMMAND_MARKER"


@pytest.fixture(scope="module")
def marker_repo(tmp_path_factory):
    """One throwaway repo holding a config value only a SUBCOMMAND can print.

    Module-scoped and built through ``tmp_path_factory`` on purpose. Both tests
    below probe every member of their set, so a repo per probe would be 18 of
    them per run — created under the ambient temp dir, never removed, on every
    CI run and every local run. pytest owns this one and reaps it.
    """
    git = shutil.which("git")
    if not git:  # pragma: no cover - environment dependent
        pytest.skip("needs git to derive the classification")
    repo = tmp_path_factory.mktemp("genesis-marker-repo")
    subprocess.run([git, "init", "-q", str(repo)], check=True, timeout=30)
    marker_file = repo / "probe.cfg"
    marker_file.write_text(f"[genesis]\n\tmarker = {_SUBCOMMAND_MARKER}\n", encoding="utf-8")
    return repo, marker_file


def _runs_the_subcommand(
    option: str, marker_repo, *, attached_value: str = "", separate_value: str = ""
) -> bool:
    """Whether the installed git still runs a subcommand placed after *option*.

    Two things about this probe are load-bearing, and the first version of it
    got the second one wrong.

    The marker is a config value rather than the version string, because the
    obvious marker cannot answer this question: `git --version version` prints
    "git version …" whether the subcommand ran or not, so it cannot tell the
    two apart for the very options being asked about.

    And the marker is read from a file named by ABSOLUTE PATH, never from the
    repository's own config. A global that changes repository or config
    resolution — `--bare`, `--git-dir=…` — makes a repo-relative lookup miss
    while the subcommand runs perfectly well, and this probe cannot tell that
    miss from "the option was rejected". That is not hypothetical: it is how
    `--bare` came to be absent from every set while the sweep reported a tidy
    750-candidate breakdown. An oracle that the thing being measured can
    redirect is measuring something else.
    """
    repo, marker_file = marker_repo
    spelling = f"{option}={attached_value}" if attached_value else option
    argv = ["git", spelling]
    if separate_value:
        argv.append(separate_value)
    argv += ["config", "--file", str(marker_file), "--get", "genesis.marker"]
    p = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(repo),
    )
    return _SUBCOMMAND_MARKER in p.stdout


def test_the_no_subcommand_exemption_is_true_of_the_installed_git(marker_repo):
    """Each exempt option must really stop the subcommand running.

    THIS IS A SAFETY ASSERTION, not a tidiness one, and it is the price of the
    exemption. Because these options are exempt, a gated verb written after one
    of them is ALLOWED — MEASURED as a deliberate change: all seven go BLOCK ->
    ALLOW, on the ground that git never reaches the verb. If that ground ever
    stops holding for one of them (a git release that keeps parsing, a
    spelling that means something else), the exemption silently becomes a
    bypass of every gate keyed on the subcommand.

    So the claim is re-derived from the binary on every run rather than trusted
    from a comment. Without this, the fix for a hand-maintained list that fails
    open would have introduced a second hand-maintained list that fails open.

    GUARD-THE-GUARD, and it is structural rather than an extra assertion: this
    test and its sibling below demand OPPOSITE answers from the same helper —
    every option here must NOT run the subcommand, every option there MUST.
    MEASURED 7 False / 11 True. A helper stuck on either constant therefore
    fails one of the two, which is what stops a derived test going quiet.
    """
    still_running = []
    for option in sorted(sp._GIT_OPTS_NO_SUBCOMMAND):
        if _runs_the_subcommand(option, marker_repo):
            still_running.append(option)

    assert not still_running, (
        f"{still_running} are exempt from the closed world on the grounds that "
        "no subcommand runs after them, but on THIS git the subcommand DID "
        "run. A gated verb written after one of these is currently allowed, so "
        "each of these is a live bypass. Remove it from "
        "_GIT_OPTS_NO_SUBCOMMAND and classify it by what it actually does."
    )


def test_the_valueless_set_really_consumes_nothing(marker_repo):
    """A valueless option that actually eats a value would hide the verb.

    The mirror of the assertion above, in the other direction: the walk steps
    over these ALONE, so if one of them consumed the following token, that
    token is the verb slot again and we are back at the original defect.

    THREE-WAY, not two-way, because the set is VERSION-DEPENDENT and a first
    version of this test demanded that every member run bare — which failed
    the moment the set gained `--no-lazy-fetch`/`--no-advice`, valueless on the
    git CI runs and REJECTED outright by the git on this box. An entry the
    installed git rejects is INERT here (git refuses the command before any
    walk matters — the consumer set's `--super-prefix` precedent), so:

    * subcommand runs bare            -> valueless CONFIRMED on this git;
    * bare fails but runs WITH a value
      interposed                      -> a CONSUMER misfiled as valueless —
                                         the dangerous direction, the walk is
                                         off by one and a VALUE lands in the
                                         verb slot. FAIL, loudly;
    * neither runs                    -> this git rejects the option; inert.

    Accepted residual, stated rather than hidden: a TERMINAL option misfiled
    here (accepted, runs nothing, e.g. a future `--version` sibling) lands in
    the third bucket and passes — that misfile makes the walk look for a verb
    after an option git handles itself, an over-block in the safe direction.
    """
    values = ("/tmp/x", "user.name=x", "HEAD", ".")
    consumers_in_disguise = []
    for option in sorted(sp._GIT_OPTS_VALUELESS):
        if _runs_the_subcommand(option, marker_repo):
            continue  # valueless confirmed on this git
        if any(
            _runs_the_subcommand(option, marker_repo, separate_value=value)
            for value in values
        ):
            consumers_in_disguise.append(option)
    assert not consumers_in_disguise, (
        f"{consumers_in_disguise} are listed as valueless, but on THIS git the "
        "subcommand runs only when a VALUE follows them — they CONSUME it, the "
        "walk is off by one, and the token after them lands in the verb slot. "
        "Move each to _GIT_OPTS_WITH_ARG (all four copies + the canonical set)."
    )


def test_the_attached_spelling_is_measured_too(marker_repo):
    """The exemption is BARE-only, and this is the axis that proves why.

    `--exec-path` runs no subcommand; `--exec-path=<path>` sets the exec path
    and RUNS one. The sets are keyed by option NAME, so nothing in them can
    express that difference — the walk does, with a `not value_attached`
    qualifier, and dropping that qualifier reopens the bypass.

    The two tests above probe the bare spelling only, so neither of them can
    see this. Without this case the file whose job is "re-derive from the
    binary on every run" would grade a future edit to the sets with a probe
    blind to the dangerous half.
    """
    assert not _runs_the_subcommand("--exec-path", marker_repo), (
        "bare --exec-path ran the subcommand; the no-subcommand exemption is "
        "no longer true of this git"
    )
    # The path comes from the git BEING TESTED, never a literal: a source-built
    # git or a distro using /usr/libexec/git-core has no git-config under the
    # hardcoded directory, so the probe would return False and this test would
    # fail on a correct parser. Install-agnostic tests are a repo rule, and this
    # assertion is about the parser, not about where git was installed.
    exec_path = subprocess.run(
        ["git", "--exec-path"], capture_output=True, text=True, timeout=30
    ).stdout.strip()
    assert exec_path, "git --exec-path reported nothing; the probe below would be vacuous"
    assert _runs_the_subcommand(
        "--exec-path", marker_repo, attached_value=exec_path
    ), (
        "--exec-path=<path> did NOT run the subcommand on this git. If that is "
        "genuinely so here, the `not value_attached` qualifier in _verb_walk "
        "is no longer load-bearing — but verify before relaxing it, because "
        "relaxing it wrongly allows a publish this guard refuses."
    )


def test_every_global_gits_usage_advertises_is_classified():
    """git's own usage line is a completeness check the strings sweep is not.

    The sweep answers "what does this option DO"; it cannot answer "did I miss
    one", because a sweep blind to a class scores that class as rejected and
    reports a tidy total either way. This reads the list git itself advertises
    and asserts the closed world has an answer for each — the check that would
    have caught `--bare`, which the first sweep silently dropped.

    Scoped to the globals git prints before its command list, so it stays a
    dozen-odd options rather than an open-ended scrape.
    """
    import re

    # LC_ALL=C: the split sentinel below is English, and git's help text is
    # translatable. Under a localized catalog the sentinel is absent, `head`
    # becomes the WHOLE help output, and the footer's `git help -a` / `git help -g`
    # examples get scraped as advertised globals — so the test fails on a
    # localized box while the option table is perfectly correct.
    usage = subprocess.run(
        ["git"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "LC_ALL": "C", "LANGUAGE": "C"},
    ).stdout
    head = usage[: usage.find("These are common")] if "These are common" in usage else usage
    assert head.strip(), "could not read git's usage line; the check would be vacuous"

    advertised = set(re.findall(r"(?<![\w-])(--?[a-zA-Z][\w-]*)", head))
    known = sp._GIT_OPTS_WITH_ARG | sp._GIT_OPTS_VALUELESS | sp._GIT_OPTS_NO_SUBCOMMAND
    unclassified = advertised - known

    assert not unclassified, (
        f"{sorted(unclassified)} are globals git ADVERTISES and the closed "
        "world now refuses, so ordinary commands using them are blocked. "
        "Classify each against the installed binary with an oracle that does "
        "not resolve through the repository (see _runs_the_subcommand), then "
        "add it to the matching set."
    )


def test_the_attached_only_terminal_globals_are_true_of_the_installed_git(marker_repo):
    """`--list-cmds=<groups>` is terminal ATTACHED — the mirror of `--exec-path`.

    Two option shapes that look alike and behave oppositely, which is why they
    need separate sets rather than one `no_subcommand` field:

        --exec-path        bare -> no subcommand   attached -> RUNS the subcommand
        --list-cmds        bare -> REJECTED        attached -> no subcommand

    A single bare-only exemption gets `--list-cmds=main` wrong in the
    over-block direction: the closed world calls it unclassified and the push
    guard refuses it. That is not hypothetical — the installed bash-completion
    script invokes `__git --list-cmds=main,others,alias,nohelpers`, so the
    spelling is in live use on this box.

    It is invisible to the usage-line completeness check because `git -h` does
    not advertise it, so this case exists precisely to cover what that sweep
    cannot see. Raised by review, confirmed against the binary here.
    """
    assert not _runs_the_subcommand("--list-cmds", marker_repo, attached_value="main"), (
        "git --list-cmds=main ran the subcommand on this git; if that is genuinely "
        "so here, it is NOT a terminal option and must leave "
        "_GIT_OPTS_NO_SUBCOMMAND_ATTACHED — otherwise a gated verb after it is allowed"
    )
    for option in sorted(sp._GIT_OPTS_NO_SUBCOMMAND_ATTACHED):
        bare_runs = _runs_the_subcommand(option, marker_repo)
        assert not bare_runs, (
            f"{option} is listed as attached-only terminal, but its BARE form ran "
            "the subcommand on this git — a gated verb after the bare spelling "
            "would then be allowed. Re-derive its classification."
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
