"""Behavioural lock for the dated-docs privacy rule in `.gitignore`.

The rule (`docs/**/20[0-9][0-9]-*`) is the only thing standing between a
working artifact dropped into an opt-in docs directory and a `git add -A` that
publishes it to a PUBLIC repo. It lives inside a ~35-line block of interacting
ignore/negate rules: `docs/*` ignores everything, per-directory `!docs/<dir>/`
negations open specific directories back up, and this rule re-closes the dated
files inside them.

Nothing asserted that, so a later edit could reopen the leak with no signal.

THE ACTUAL HAZARD, measured rather than assumed. An earlier version of this
docstring named "reordering the block" as the risk. That is FALSE, and the
correction matters because it points at the wrong edit. Probed in a scratch
repo against `docs/architecture/2026-x.md`:

    baseline (rule last)                       -> IGNORED
    `!docs/architecture/**` added BELOW rule    -> NOT ignored   <-- LEAK
    `!docs/architecture/*`  added BELOW rule    -> NOT ignored   <-- LEAK
    `!/docs/public-new/**`  added BELOW rule    -> NOT ignored   <-- LEAK
    `!docs/actions/sub/**`  added BELOW rule    -> NOT ignored   <-- LEAK
    block REORDERED (rule above the opt-ins)   -> IGNORED

A directory-form negation (`!docs/<dir>/`) matches only the directory
component and never the file path beneath it, so it cannot win last-match-wins
against a file pattern regardless of order — reordering the current block is
safe. What DOES reopen the leak is specifically a `/*` or `/**` negation placed
below the rule, in ANY of its spellings: root-anchored (`!/docs/...`, which
`gitignore(5)` defines as anchoring to the `.gitignore` directory) and NESTED
(`!docs/<dir>/<sub>/**`) leak exactly as readily as the top-level form. The
negation scan below therefore routes on shape, not on segment count, and fails
loudly on any docs negation it does not recognise — a silently unparsed line is
a directory with zero coverage.

These tests query `git check-ignore`, i.e. git's own evaluation of the real
ignore rules, rather than re-implementing pattern matching. They run that
evaluation inside a THROWAWAY repo holding only this repository's own
`.gitignore` files, because `git check-ignore` in a live checkout also consults
`$GIT_DIR/info/exclude` and `core.excludesFile`. Both were measured to change
the verdict: a global `docs/architecture/*` rule turns the public assertions
red, and a matching contributor-local rule would keep the private assertions
green even with the repository rule deleted — i.e. the lock would silently stop
locking anything. Isolation is what makes the result a property of the
committed file rather than of whoever ran the suite.

They are install-agnostic: they need a git checkout and nothing else — no
services, no network, no local config.
"""

from __future__ import annotations

import atexit
import functools
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# A dated path component: the thing the rule withholds, spelled as a matcher for
# names already on disk. Kept next to the rule's own character class.
_DATED_RE = re.compile(r"^20[0-9][0-9]-")

# Scrubbed so an ambient GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE (a suite run from
# inside a hook or a rebase) cannot redirect any of the git calls below.
#
# HOME and XDG_CONFIG_HOME go too, and the additions are not belt-and-braces:
# `-c core.excludesFile=/dev/null` neutralises ONE ambient source, and git reads
# several. `core.ignoreCase=true` in a global config was MEASURED to keep every
# assertion here green while the rule was miscased to `Docs/**/20[0-9][0-9]-*`
# — the lock would have stopped locking, silently, on whoever ran it. Pointing
# GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM at /dev/null is git's own documented
# way to say "no ambient config", and it covers the sources an excludesFile
# override does not (Codex P2, PR #1633).
_GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "HOME": tempfile.gettempdir(),
    "XDG_CONFIG_HOME": tempfile.gettempdir(),
}


# --------------------------------------------------------------------------
# Parsing the `!docs/...` negations
# --------------------------------------------------------------------------

# Every negation line, whatever it points at. Git strips trailing whitespace
# (unless escaped) but NOT leading whitespace, so the `!` must be column 0.
_NEGATION_RE = re.compile(r"^!(?P<pattern>.+?)\s*$")

# A negation that touches a `docs` path segment in any spelling — used only to
# decide whether an unrecognised line is a silent coverage hole worth failing on.
_MENTIONS_DOCS_RE = re.compile(r"(?:^|/)docs(?:/|$)")

# The complete set of targeted (single-file) escape hatches. Asserted EXACTLY,
# not merely as non-empty: a targeted negation un-ignores one path outright, so
# a future `!docs/actions/2028-private-plan.md` would republish a dated working
# artifact, and a mere "some targeted negation exists" check would greet that as
# a healthy escape hatch. Extending this tuple is the deliberate, reviewable act
# that adding a new hatch requires.
_EXPECTED_TARGETED_NEGATIONS = ("actions/README.md",)


def _docs_relative_negation(pattern: str) -> str | None:
    """Return the `docs/`-relative body of a negation, or None if it isn't one.

    A leading `/` anchors a pattern to the `.gitignore` directory and is
    otherwise a no-op here, so `!/docs/x/` and `!docs/x/` are the same rule.
    """
    body = pattern[1:] if pattern.startswith("/") else pattern
    if body.startswith("docs/"):
        return body[len("docs/") :]
    return None


def _is_directory_form(rest: str) -> bool:
    """`!docs/x/`, `!docs/x/*` and `!docs/x/**` all open a whole directory."""
    return rest.endswith(("/", "/*", "/**"))


def _classify_docs_negations() -> tuple[list[str], list[str]]:
    """Split every docs negation into (directory opt-ins, targeted paths).

    A DIRECTORY opt-in makes a whole directory public, at ANY depth:
    `!docs/architecture/`, `!docs/architecture`, `!docs/architecture/*`,
    `!docs/architecture/**`, and equally `!docs/actions/public-subdir/**`.
    Every one of them needs the dated-file tests below, so classification keys
    on the trailing form rather than on segment count — the earlier
    "one segment = directory, more = file" rule filed a nested `/**` opt-in as a
    single targeted path, which probed only that literal path while
    `docs/actions/public-subdir/2026-secret.md` was measurably NOT ignored.

    A TARGETED negation names a concrete deeper path (`!docs/actions/README.md`)
    and un-ignores exactly one thing. It is the deliberate escape hatch, so it is
    recognised rather than treated as a parse failure — but it is returned, not
    discarded, so the test below can assert it really is public.

    Anything matching neither shape raises, because a silently unparsed line is
    a directory with zero coverage.
    """
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    dirs: list[str] = []
    targeted: list[str] = []
    unparsed: list[str] = []
    for line in lines:
        m = _NEGATION_RE.match(line)
        if not m:
            continue
        pattern = m.group("pattern")
        rest = _docs_relative_negation(pattern)
        if rest is None:
            # Not a docs negation at all (e.g. `!secrets.env.example`) — unless
            # it reaches a docs path by some spelling this parser cannot read,
            # in which case that directory would get no coverage at all.
            if _MENTIONS_DOCS_RE.search(pattern):
                unparsed.append(line)
            continue
        # Drop glob/empty components so every directory spelling collapses to
        # the same path.
        parts = [p for p in rest.split("/") if p not in ("", "*", "**")]
        if not parts:
            # `!docs/`, `!docs/**` — opens docs wholesale; not a shape any test
            # below models, so fail rather than silently ignore it.
            unparsed.append(line)
        elif _is_directory_form(rest) or len(parts) == 1:
            dirs.append("/".join(parts))
        else:
            targeted.append("/".join(parts))
    assert not unparsed, (
        "unrecognised docs negation(s) — these would get NO privacy "
        f"coverage from this file: {unparsed}"
    )
    return sorted(set(dirs)), sorted(set(targeted))


def _opt_in_dirs() -> list[str]:
    return _classify_docs_negations()[0]


# --------------------------------------------------------------------------
# Asking git, in isolation from any non-repository ignore source
# --------------------------------------------------------------------------


def _tracked_docs() -> list[str]:
    """Tracked paths under `docs/`, NUL-delimited.

    `-z` rather than splitting on whitespace: a legitimate
    `docs/architecture/notes 2027-roadmap.md` splits into two tokens under
    `.split()`, the second of which matches the dated pattern and is reported as
    a leak that does not exist (and inflates the corpus-size check besides).
    `-z` also disables the C-quoting git otherwise applies to unusual bytes.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "docs/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=_GIT_ENV,
    ).stdout
    return [p for p in out.split("\0") if p]


def _copy_ignore_file(src: Path, dest: Path) -> None:
    """Copy a `.gitignore`, REFUSING a symlink rather than following one.

    `shutil.copyfile` follows the link and writes its TARGET's bytes as a
    regular file, so the isolated repo would evaluate rules the live checkout
    does not: git explicitly does not follow a symlinked `.gitignore`. Every
    assertion here would pass against content that is inert in reality — the
    lock reporting green on rules git never reads (Codex P2, PR #1633).

    Refused rather than handled, because a symlinked ignore file in this repo
    would itself be the finding.
    """
    if src.is_symlink():
        raise AssertionError(
            f"{src} is a SYMLINK. Git does not follow a symlinked .gitignore, so "
            "its rules are inert in the real checkout — copying it here would "
            "make this suite pass against rules that do not apply."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def _make_repo(
    gitignore_text: str | None = None,
    extra_ignores: dict[str, str] | None = None,
) -> Path:
    """A throwaway repo carrying only this repository's own ignore rules.

    `git check-ignore` run in the live checkout answers from the union of
    `.gitignore`, `$GIT_DIR/info/exclude` and `core.excludesFile`. Both extra
    sources were measured to flip results, in both directions, so evaluating
    there would make these assertions a property of the machine rather than of
    the committed file. Here: a fresh repo, `.gitignore` copied in,
    `info/exclude` emptied (an `init.templateDir` can seed it), and
    `core.excludesFile` neutralised on every call.
    """
    tmp = Path(tempfile.mkdtemp(prefix="gitignore-privacy-lock-"))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    subprocess.run(
        ["git", "init", "-q", "."],
        cwd=tmp,
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    # Case sensitivity is PINNED, not inherited: a case-insensitive match would
    # accept a miscased rule that the real (case-sensitive) checkout ignores.
    subprocess.run(
        ["git", "config", "core.ignoreCase", "false"],
        cwd=tmp,
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    if gitignore_text is None:
        _copy_ignore_file(REPO_ROOT / ".gitignore", tmp / ".gitignore")
    else:
        (tmp / ".gitignore").write_text(gitignore_text)
    # Nested `.gitignore` files under docs/ also apply to docs paths. There are
    # none today (MEASURED: 0 of 92 tracked docs); copying them keeps the
    # isolation faithful if one is ever added, instead of silently dropping it.
    for nested in (p for p in _tracked_docs() if Path(p).name == ".gitignore"):
        _copy_ignore_file(REPO_ROOT / nested, tmp / nested)
    # Nested ignore files the caller invents — the mutation harness uses this to
    # ask whether a `.gitignore` added DEEPER in the tree can reopen the leak.
    for relpath, text in (extra_ignores or {}).items():
        dest = tmp / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    exclude = tmp / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("")
    return tmp


@functools.lru_cache(maxsize=1)
def _isolated_repo() -> Path:
    """The baseline repo, carrying this repository's real ignore rules."""
    return _make_repo()


def _ignored_subset_in(repo: Path, relpaths: Sequence[str]) -> set[str]:
    """Of `relpaths`, the ones git would ignore. The paths need not exist.

    `--no-index` on purpose: the threat is a NEW, untracked file swept up by
    `git add -A`, where indexed and non-indexed modes agree, and it decouples
    the result from whatever happens to be staged locally. The one case it
    structurally cannot see — an already-tracked dated file — is covered by
    `test_no_currently_tracked_doc_uses_the_dated_naming` below.

    Batched through `--stdin` so a 100-year sweep costs one subprocess.
    """
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.excludesFile=/dev/null",
            "check-ignore",
            "--no-index",
            "-z",
            "--stdin",
        ],
        cwd=repo,
        input="\0".join(relpaths) + "\0",
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    # 0 = at least one ignored, 1 = none ignored; anything else is a real error.
    if result.returncode not in (0, 1):
        raise AssertionError(
            f"git check-ignore failed for {list(relpaths)[:5]!r}...: "
            f"rc={result.returncode} {result.stderr}"
        )
    return {p for p in result.stdout.split("\0") if p}


def _ignored_subset(relpaths: Sequence[str]) -> set[str]:
    """`_ignored_subset_in` against the real rules."""
    return _ignored_subset_in(_isolated_repo(), relpaths)


def _is_ignored(relpath: str) -> bool:
    return relpath in _ignored_subset([relpath])


# --------------------------------------------------------------------------
# The negation block itself
# --------------------------------------------------------------------------


def test_the_opt_in_list_is_actually_parsed():
    """Guard the guard: an empty list makes every parametrized test below
    silently green — pytest reports `SKIPPED (got empty parameter set)` and
    exits 0."""
    dirs = _opt_in_dirs()
    assert len(dirs) >= 5, dirs
    # Named anchors — if these ever stop being opt-in the tests below change
    # meaning, and that should be a deliberate, visible edit.
    assert "architecture" in dirs and "decisions" in dirs, dirs


def test_targeted_file_negations_are_exactly_the_documented_escape_hatches():
    """The deliberate escape hatch, enumerated and asserted rather than assumed.

    Two distinct claims, because a "targeted negations exist" check would pass
    on a hatch that defeats the very rule this file locks:

    1. The set is EXACTLY the documented one. A new `!docs/<dir>/<file>` line
       republishes that file outright, so it must not slip in under a test that
       merely counts.
    2. No hatch names a dated path component — that spelling would hand back
       precisely what `docs/**/20[0-9][0-9]-*` exists to withhold.
    """
    _, targeted = _classify_docs_negations()
    assert tuple(targeted) == _EXPECTED_TARGETED_NEGATIONS, (
        "the set of single-file `!docs/...` escape hatches changed: expected "
        f"{list(_EXPECTED_TARGETED_NEGATIONS)}, found {targeted}. Each one "
        "un-ignores a path outright — if the new entry is deliberate and public, "
        "add it to _EXPECTED_TARGETED_NEGATIONS."
    )
    dated_hatches = [
        path for path in targeted if any(_DATED_RE.match(part) for part in Path(path).parts)
    ]
    assert not dated_hatches, (
        "these escape hatches un-ignore a DATED path, defeating the "
        f"dated-docs privacy rule outright: {dated_hatches}"
    )
    for path in targeted:
        assert not _is_ignored(f"docs/{path}"), (
            f"docs/{path} carries an explicit `!docs/{path}` negation but is "
            "still ignored — the escape hatch is broken"
        )


# --------------------------------------------------------------------------
# The rule: dated things are private in every opt-in directory
# --------------------------------------------------------------------------


@pytest.mark.parametrize("subdir", _opt_in_dirs())
def test_dated_docs_are_private_in_every_opt_in_dir(subdir):
    """The whole point of the rule: per-directory opt-in must not mean
    'anything dropped in here is public'."""
    assert _is_ignored(f"docs/{subdir}/2026-working-draft.md"), (
        f"docs/{subdir}/ is opt-in PUBLIC and a dated working artifact in it "
        "is NOT ignored — a `git add -A` would publish it"
    )


# The contract is `20[0-9][0-9]-`: every year 2000-2099, not a sample of them.
_PROTECTED_YEARS = tuple(range(2000, 2100))


@pytest.mark.parametrize("subdir", _opt_in_dirs())
def test_every_protected_year_is_private(subdir):
    """All 100 years, not two of them.

    Sampling 2026 and 2031 leaves the character class untested: narrowing the
    rule to the plausible-looking `docs/**/20[23][0-9]-*` was MEASURED to keep
    both sampled years — and every other assertion in this file — green, while
    2000-2019 and 2040-2099 became publishable. One batched `check-ignore` call
    per directory covers the whole finite range.
    """
    probes = [f"docs/{subdir}/{year}-working-draft.md" for year in _PROTECTED_YEARS]
    ignored = _ignored_subset(probes)
    leaked = [p for p in probes if p not in ignored]
    assert not leaked, (
        f"{len(leaked)} of {len(probes)} protected years are NOT ignored under "
        f"docs/{subdir}/ — the year character class has been narrowed: {leaked}"
    )


@pytest.mark.parametrize("subdir", _opt_in_dirs())
def test_the_rule_is_not_extension_scoped(subdir):
    """Deliberately extensionless: a dated .svg or .txt is a working artifact
    too, and for a PRIVACY rule over-ignoring is the safe direction."""
    assert _is_ignored(f"docs/{subdir}/2026-diagram.svg")
    assert _is_ignored(f"docs/{subdir}/2031-notes.txt")


@pytest.mark.parametrize("subdir", _opt_in_dirs())
def test_a_dated_directory_is_private_too(subdir):
    """`docs/<dir>/2026-sprint/notes.md` is ignored by the rule (the `20..-`
    component matches at any depth). Asserted so the tracked-file scan below
    and this one agree about what "dated" means."""
    assert _is_ignored(f"docs/{subdir}/2026-sprint/notes.md")


@pytest.mark.parametrize("subdir", _opt_in_dirs())
def test_ordinary_and_adr_named_docs_stay_public(subdir):
    """The other direction — the rule must not swallow legitimate docs.

    `20[0-9][0-9]-` rather than `[0-9]{4}-` exists precisely so a 4-digit
    sequential ADR scheme is not silently ignored, and the year class is
    bounded on both sides. 1999 and 2100 pin those boundaries: widening the
    rule to `[0-9][0-9][0-9][0-9]-` would start swallowing ADRs, and this is
    where that shows up.
    """
    probes = [
        f"docs/{subdir}/{name}"
        for name in (
            "some-design-note.md",
            "001-first-decision.md",
            "0042-fourth-digit-adr.md",
            "1999-notes.md",
            "2100-notes.md",
        )
    ]
    ignored = sorted(_ignored_subset(probes))
    assert not ignored, (
        "the dated-private rule has widened past the documented "
        f"20[0-9][0-9]- year class and now ignores public docs: {ignored}"
    )


# --------------------------------------------------------------------------
# The case `--no-index` structurally cannot see: already-tracked files
# --------------------------------------------------------------------------

# The force-add escape hatch, made real. `.gitignore` promises that "a
# genuinely-public dated doc is still possible with an explicit `git add -f`",
# but `git add -f` leaves the path in `git ls-files` (MEASURED), so a bare
# "no tracked doc is dated" assertion fails forever afterwards and the promised
# hatch does not exist. Registering the path here is the second half of that
# hatch: force-add it, then declare it, and the declaration is what review sees.
_EXPLICITLY_PUBLIC_DATED_DOCS: frozenset[str] = frozenset()


def test_no_currently_tracked_doc_uses_the_dated_naming():
    """Measured against real data: the rule must not collide with a naming
    convention already in public use.

    Scoped to the DATED pattern on purpose. A blanket "no tracked doc is
    ignored" assertion is wrong here and was tried first: four top-level files
    (`docs/INDEX.md` and siblings) match the much older blanket `docs/*` rule
    at the top of the block, and are public only because git never applies
    ignore rules to files that are already tracked. That is pre-existing and
    has nothing to do with this rule, so asserting on it would fail on day one
    for the wrong reason.

    What this DOES lock is the claim the rule rests on — that no doc anyone
    publishes today is named this way. If someone later adds a genuinely public
    `2027-roadmap.md`, or a `docs/journey/2027-review/` directory, this fails
    and points at the two real remedies: rename it, or `git add -f` it AND
    register it in `_EXPLICITLY_PUBLIC_DATED_DOCS` above.
    """
    tracked = _tracked_docs()
    assert len(tracked) > 20, f"only {len(tracked)} tracked docs — test would be weak"

    # Every path COMPONENT, not just the basename: the rule matches a dated
    # directory as readily as a dated file.
    dated = [p for p in tracked if any(_DATED_RE.match(part) for part in Path(p).parts)]
    unregistered = [p for p in dated if p not in _EXPLICITLY_PUBLIC_DATED_DOCS]
    assert not unregistered, (
        "these docs are tracked/public but match the dated-private pattern, so "
        "the convention now collides with real public files: "
        f"{unregistered}. Rename them, or — if one is deliberately public — "
        "`git add -f` it and add it to _EXPLICITLY_PUBLIC_DATED_DOCS."
    )

    stale = sorted(_EXPLICITLY_PUBLIC_DATED_DOCS - set(tracked))
    assert not stale, (
        "_EXPLICITLY_PUBLIC_DATED_DOCS names paths that are no longer tracked "
        f"docs — the exemption has outlived its file: {stale}"
    )


# --------------------------------------------------------------------------
# Can this suite actually DETECT the rule being broken?
# --------------------------------------------------------------------------
#
# Twelve review findings landed on this file (PR #1633) and every one of them
# had the same shape: "replace the rule with <narrowing> and all 43 tests still
# pass while <a real path> becomes publishable". They are not twelve bugs. They
# are twelve reports that the SUITE'S PROBE CORPUS is thinner than the contract
# it claims to lock — and arguing corpus adequacy finding-by-finding is how a
# thirteenth arrives.
#
# So adequacy is MEASURED instead. Each mutation below is applied to a copy of
# the real `.gitignore`, the whole corpus is re-evaluated by git, and the suite
# fails if nothing flips. A mutation that survives is this file's bug, reported
# as this file's bug, before it is anyone else's.
#
# This is the same instrument as verify-RED, pointed at the suite rather than at
# one test: a lock nobody has watched fail is a lock nobody has tested.

_DATED_RULE = "docs/**/20[0-9][0-9]-*"

#: (name, replacement-for-_DATED_RULE, why it is plausible)
_NARROWINGS = (
    (
        "depth-narrowed",
        "docs/*/20[0-9][0-9]-*",
        "`*` instead of `**` — reads as 'a dated file in a docs subdir' and "
        "still ignores direct children, so only a NESTED probe can see it",
    ),
    (
        "decade-narrowed",
        "docs/**/20[23][0-9]-*",
        "plausible tidy-up 'we only care about this decade'; needs a probe "
        "outside 2020-2039",
    ),
    (
        "suffix-narrowed",
        "docs/**/20[0-9][0-9]-??*",
        "a `?` added to 'require a real name'; needs a probe with a zero- or "
        "one-character suffix",
    ),
    (
        "case-changed",
        "Docs/**/20[0-9][0-9]-*",
        "a capitalised path from an editor or a case-insensitive machine; only "
        "visible with core.ignoreCase pinned false",
    ),
    (
        "deleted",
        "",
        "the rule removed outright — the floor of this whole file",
    ),
)

#: Negations appended BELOW the rule. Last match wins, so a `/*` or `/**`
#: negation re-publishes everything under it. Each spelling leaks identically
#: and each was measured to do so.
_LEAKING_APPENDS = (
    ("negated-toplevel", "!docs/architecture/**"),
    ("negated-root-anchored", "!/docs/architecture/**"),
    ("negated-nested", "!docs/architecture/sub/**"),
    ("negated-wildcard-mid", "!docs/*/architecture/**"),
)


def _privacy_probes() -> list[str]:
    """Every path the dated-docs rule is supposed to withhold.

    ONE corpus, shared by the mutation harness, so "what this suite covers" is a
    single readable list rather than an emergent property of nine parametrised
    tests. Spans the axes each finding named: depth, year, suffix length, and
    file-vs-directory.
    """
    probes: list[str] = []
    for subdir in _opt_in_dirs():
        base = f"docs/{subdir}"
        for year in (2000, 2019, 2026, 2040, 2099):  # inside and outside 20[23]x
            probes += [
                f"{base}/{year}-working-draft.md",  # direct child, long suffix
                f"{base}/sub/{year}-working-draft.md",  # NESTED — needs `**`
                f"{base}/sub/deeper/{year}-notes.md",  # deeper still
                f"{base}/{year}-x",  # single-char suffix, no extension
                f"{base}/{year}-",  # ZERO-character suffix
                f"{base}/{year}-sprint/notes.md",  # dated DIRECTORY
                # A subdirectory NAMED after an opt-in dir. Found by the
                # mutation harness itself: `!docs/*/architecture/**` appended
                # below the rule matched nothing in the corpus, because every
                # probe put `architecture` at depth 1 and that negation needs it
                # at depth 2. Without this row a negation carrying an
                # INTERMEDIATE wildcard reopens the leak unseen (Codex P2,
                # PR #1633) — and the harness reported it before a reviewer did.
                f"{base}/{subdir}/{year}-namesake-draft.md",
            ]
    return probes


def test_the_probe_corpus_is_not_empty():
    """Guard the guard: an empty corpus makes every mutation 'undetected' —
    which would fail loudly — but an almost-empty one fails for the wrong
    reason. Assert the shape directly."""
    probes = _privacy_probes()
    assert len(probes) == len(_opt_in_dirs()) * 5 * 7, len(probes)
    assert _ignored_subset(probes) == set(probes), (
        "the BASELINE rule does not ignore its own corpus — every mutation "
        "result below would be meaningless: "
        f"{sorted(set(probes) - _ignored_subset(probes))[:10]}"
    )


def _mutated_repo(gitignore_text: str) -> Path:
    return _make_repo(gitignore_text)


@pytest.mark.parametrize(
    ("name", "replacement", "why"), _NARROWINGS, ids=[m[0] for m in _NARROWINGS]
)
def test_a_narrowed_rule_is_detected(name, replacement, why):
    """Each narrowing must make at least one probe leak. If none does, the
    corpus cannot see that mutation and this suite would greet it as healthy."""
    original = (REPO_ROOT / ".gitignore").read_text()
    assert _DATED_RULE in original, (
        f"the dated-docs rule {_DATED_RULE!r} is not in .gitignore verbatim — "
        "the mutation harness is targeting a line that no longer exists"
    )
    mutated = original.replace(_DATED_RULE, replacement)
    probes = _privacy_probes()
    leaked = set(probes) - _ignored_subset_in(_mutated_repo(mutated), probes)
    assert leaked, (
        f"mutation '{name}' SURVIVED: replacing the rule with {replacement!r} "
        f"left every probe still ignored, so this suite cannot detect it. "
        f"Why it is plausible: {why}. Add a probe to _privacy_probes() that "
        "this narrowing would expose."
    )


@pytest.mark.parametrize(("name", "line"), _LEAKING_APPENDS, ids=[m[0] for m in _LEAKING_APPENDS])
def test_a_negation_below_the_rule_is_detected(name, line):
    """The hazard the module docstring measured: a `/*` or `/**` negation placed
    BELOW the rule re-publishes everything under it, in any spelling. The suite
    must see each one."""
    original = (REPO_ROOT / ".gitignore").read_text()
    mutated = original.replace(_DATED_RULE, f"{_DATED_RULE}\n{line}")
    probes = _privacy_probes()
    leaked = set(probes) - _ignored_subset_in(_mutated_repo(mutated), probes)
    assert leaked, (
        f"mutation '{name}' SURVIVED: appending {line!r} below the rule left "
        "every probe still ignored, so this suite would not notice that "
        "negation reopening the leak."
    )


def test_an_unrelated_edit_is_not_flagged():
    """CONTROL, and it is load-bearing: without it, a harness that reported
    EVERY mutation as detected — because the corpus was somehow always leaking —
    would pass every test above while proving nothing."""
    original = (REPO_ROOT / ".gitignore").read_text()
    mutated = original + "\n# a comment changes nothing\n*.unrelated-extension\n"
    probes = _privacy_probes()
    leaked = set(probes) - _ignored_subset_in(_mutated_repo(mutated), probes)
    assert not leaked, (
        "an edit that does not touch the docs rules was reported as a leak: "
        f"{sorted(leaked)[:5]}"
    )


#: A `.gitignore` added DEEPER in the tree. Git applies it to everything below
#: its own directory and it is evaluated AFTER the root file, so a negation
#: there wins last-match-wins over the root rule — the same leak as an appended
#: negation, arriving through a file the root parse never reads.
_NESTED_IGNORE_LEAKS = (
    ("nested-negation-bare", "!20[0-9][0-9]-*"),
    ("nested-negation-globbed", "!**/20[0-9][0-9]-*"),
)


@pytest.mark.parametrize(
    ("name", "line"), _NESTED_IGNORE_LEAKS, ids=[m[0] for m in _NESTED_IGNORE_LEAKS]
)
def test_a_negation_in_a_nested_gitignore_is_detected(name, line):
    """A negation can arrive in a `.gitignore` the root parse never reads.

    There are none under `docs/` today, so this is not a live leak — it is the
    question of whether the suite would SEE one. It would not have: the opt-in
    list and every probe are derived from the root file, so a tracked
    `docs/<dir>/sub/.gitignore` containing `!20[0-9][0-9]-*` republishes that
    subtree with all 43 tests green (Codex P2, PR #1633).

    Detected here without teaching the parser about nested files — the harness
    asks GIT, which already knows, and the corpus's nested probes are what make
    the answer visible.
    """
    subdir = _opt_in_dirs()[0]
    repo = _make_repo(extra_ignores={f"docs/{subdir}/sub/.gitignore": f"{line}\n"})
    probes = _privacy_probes()
    leaked = set(probes) - _ignored_subset_in(repo, probes)
    assert leaked, (
        f"mutation '{name}' SURVIVED: a nested .gitignore under "
        f"docs/{subdir}/sub/ containing {line!r} left every probe still "
        "ignored, so this suite would not notice that subtree being reopened."
    )
