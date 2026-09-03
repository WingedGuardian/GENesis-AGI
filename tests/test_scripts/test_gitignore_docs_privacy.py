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

    baseline (rule last)                      -> IGNORED
    `!docs/architecture/**` added BELOW rule   -> NOT ignored   <-- LEAK
    `!docs/architecture/*`  added BELOW rule   -> NOT ignored   <-- LEAK
    block REORDERED (rule above the opt-ins)   -> IGNORED

A directory-form negation (`!docs/<dir>/`) matches only the directory
component and never the file path beneath it, so it cannot win last-match-wins
against a file pattern regardless of order — reordering the current block is
safe. What DOES reopen the leak is specifically a `/*` or `/**` negation placed
below the rule. That is the edit these tests exist to catch, and it is also the
spelling the opt-in parser below had to be widened to see.

These tests query `git check-ignore`, i.e. git's own evaluation of the real
`.gitignore`, rather than re-implementing pattern matching. They are
install-agnostic: they need a git checkout and nothing else — no services, no
network, no local config.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every `!docs/...` negation, in any spelling git accepts. The trailing slash is
# stylistic, not required, and `/*` and `/**` are the forms that actually leak
# (see the module docstring) — so a parser matching only `!docs/<dir>/` would be
# blind to precisely the edit these tests guard against.
# Anything at all that opens a docs path back up. Used to prove the parse is
# EXHAUSTIVE: an unrecognised spelling must fail loudly, because a silently
# unparsed line means that directory gets zero coverage while the count check
# below still passes on the other eight.
_ANY_DOCS_NEGATION_RE = re.compile(r"^!docs/(.+?)\s*$")


def _classify_docs_negations() -> tuple[list[str], list[str]]:
    """Split every `!docs/...` line into (directory opt-ins, targeted paths).

    A DIRECTORY opt-in names one segment (`!docs/architecture/`,
    `!docs/architecture`, `!docs/architecture/*`, `!docs/architecture/**`) and
    makes a whole directory public — every one of those needs the dated-file
    tests below.

    A TARGETED negation names a deeper path (`!docs/actions/README.md`) and
    un-ignores exactly one thing. It is the deliberate escape hatch, so it is
    recognised and excluded rather than treated as a parse failure — but it is
    returned, not discarded, so the test below can assert it really is public.

    Anything matching neither shape raises, because a silently unparsed line is
    a directory with zero coverage.
    """
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    dirs: list[str] = []
    targeted: list[str] = []
    unparsed: list[str] = []
    for line in lines:
        m = _ANY_DOCS_NEGATION_RE.match(line)
        if not m:
            continue
        # Drop trailing glob/empty components so the four directory spellings
        # collapse to the same single segment.
        parts = [p for p in m.group(1).split("/") if p not in ("", "*", "**")]
        if len(parts) == 1:
            dirs.append(parts[0])
        elif len(parts) > 1:
            targeted.append(m.group(1))
        else:
            unparsed.append(line)
    assert not unparsed, (
        "unrecognised !docs/ negation(s) — these would get NO privacy "
        f"coverage from this file: {unparsed}"
    )
    return sorted(set(dirs)), sorted(set(targeted))


def _opt_in_dirs() -> list[str]:
    return _classify_docs_negations()[0]


def _is_ignored(relpath: str) -> bool:
    """Ask git whether `relpath` would be ignored. The path need not exist.

    `--no-index` on purpose: the threat is a NEW, untracked file swept up by
    `git add -A`, where indexed and non-indexed modes agree, and it decouples
    the result from whatever happens to be staged locally. The one case it
    structurally cannot see — an already-tracked dated file — is covered by
    `test_no_currently_tracked_doc_uses_the_dated_naming` below.
    """
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "--", relpath],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(
            f"git check-ignore failed for {relpath!r}: "
            f"rc={result.returncode} {result.stderr.decode(errors='replace')}"
        )
    return result.returncode == 0


def test_the_opt_in_list_is_actually_parsed():
    """Guard the guard: an empty list makes every parametrized test below
    silently green — pytest reports `SKIPPED (got empty parameter set)` and
    exits 0."""
    dirs = _opt_in_dirs()
    assert len(dirs) >= 5, dirs
    # Named anchors — if these ever stop being opt-in the tests below change
    # meaning, and that should be a deliberate, visible edit.
    assert "architecture" in dirs and "decisions" in dirs, dirs


def test_targeted_file_negations_really_are_public():
    """The deliberate escape hatch, asserted rather than assumed.

    `!docs/actions/README.md` un-ignores exactly one file. It is excluded from
    the directory tests above by shape, so without this nothing would check
    that it works — and a targeted negation that stopped working would silently
    drop a file everyone believes is published.
    """
    _, targeted = _classify_docs_negations()
    assert targeted, "no targeted negations found — this test would be vacuous"
    for path in targeted:
        assert not _is_ignored(f"docs/{path}"), (
            f"docs/{path} carries an explicit `!docs/{path}` negation but is "
            "still ignored — the escape hatch is broken"
        )


@pytest.mark.parametrize("subdir", _opt_in_dirs())
def test_dated_docs_are_private_in_every_opt_in_dir(subdir):
    """The whole point of the rule: per-directory opt-in must not mean
    'anything dropped in here is public'."""
    assert _is_ignored(f"docs/{subdir}/2026-working-draft.md"), (
        f"docs/{subdir}/ is opt-in PUBLIC and a dated working artifact in it "
        "is NOT ignored — a `git add -A` would publish it"
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
    sequential ADR scheme is not silently ignored.
    """
    assert not _is_ignored(f"docs/{subdir}/some-design-note.md")
    assert not _is_ignored(f"docs/{subdir}/001-first-decision.md")
    assert not _is_ignored(f"docs/{subdir}/0042-fourth-digit-adr.md")


_DATED_RE = re.compile(r"^20[0-9][0-9]-")


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
    and tells them it needs `git add -f` or a rename, instead of the file
    quietly never being added.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "--", "docs/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert len(tracked) > 20, f"only {len(tracked)} tracked docs — test would be weak"

    # Every path COMPONENT, not just the basename: the rule matches a dated
    # directory as readily as a dated file.
    dated = [p for p in tracked if any(_DATED_RE.match(part) for part in Path(p).parts)]
    assert not dated, (
        "these docs are tracked/public but match the dated-private pattern, so "
        f"the convention now collides with real public files: {dated}"
    )
