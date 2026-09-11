"""The ABSENCE of a CHANGELOG merge driver, pinned by behaviour and by its reason.

CHANGELOG.md deliberately has no merge driver. A collision on it CONFLICTS, and
that is the intended outcome: the failure this file guards against is the silent
one, not the visible one.

The collision is real. Two branches that each add a bullet under the same
``[Unreleased]`` heading are not disagreeing — they are inserting at the same
position, which git's default driver calls a conflict. Measured 2026-09-04: of
49 open PRs, 21 could not merge, and 18 of those 21 conflicted on CHANGELOG.md
and nothing else. The structural fix for that is one fragment per change in
``changelog.d/``; see ``.gitattributes`` for the full argument.

``merge=union`` was the obvious shortcut, was applied here, and was removed on
2026-09-10 because it cannot express a REMOVAL: deleted lines come back, at exit
0, with no conflict reported. A release cut moves every unreleased entry under a
version heading, so every branch open across one is exposed — including branches
that never touch CHANGELOG.md deliberately.

Four tests, and NONE of them is redundant — read this before deleting one:

* :func:`test_a_changelog_collision_conflicts_rather_than_merging_silently`
  carries the BEHAVIOURAL guarantee, by copying the real artifact into a fresh
  repo and running a real merge.
* :func:`test_a_union_driver_resurrects_a_deletion` is why the rule is gone, kept
  executable so the reason cannot decay into a comment nobody trusts. It is also
  the MOVING control for the test above: it is the case where this same harness
  produces a clean exit-0 merge, which is what proves the conflict is a property
  of the driver rather than of the fixture.
* :func:`test_no_tracked_path_resolves_to_a_union_driver` grades SCOPE as an
  equality over every tracked file, because a re-added pattern captures precisely
  the paths nobody would have thought to enumerate.
* :func:`test_union_does_not_reach_paths_that_do_not_exist_yet` covers what that
  population check structurally CANNOT: paths absent from the tree. It is not a
  weaker duplicate — it is the test that fails on an unanchored pattern, since a
  root-level ``CHANGELOG.md`` satisfies both spellings. That is exactly the bug
  the original rule shipped with in review.

The tests grade this repository's own attribute surface. A developer's local
``merge.default`` routes unspecified paths to a driver regardless; no committed
file can pin that, which is why every git subprocess here disables ambient
config rather than trying to enumerate it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GITATTRIBUTES = REPO_ROOT / ".gitattributes"

# Paths that must NOT inherit a silent union merge. The population check below
# covers every TRACKED file; these are the cases it structurally cannot reach —
# paths that do not exist in the tree but that a widened or unanchored pattern
# would capture the moment someone added them.
MUST_NOT_BE_UNION = (
    "CHANGELOG.md.bak",  # catches a rule widened to a glob such as CHANGELOG*
    "vendor/dep/CHANGELOG.md",  # catches an UNANCHORED pattern (no leading slash)
    "docs/CHANGELOG.md",  # same, one level down
    "scripts/release.sh",  # catches a rule widened to shell scripts
    "src/genesis/new_module.py",
)


# Ambient git state can decide the outcome of these tests, and naming the
# offending settings one at a time has already failed twice here: first
# `commit.gpgsign`/`core.hooksPath`, then `merge.default`/`core.attributesFile`,
# and each time the NEXT source was the finding. The sources are not a list one
# can keep current — config comes from system, global, XDG, the environment and
# command-scoped `GIT_CONFIG_COUNT` pairs, while ATTRIBUTES come from the system
# file, `core.attributesFile`, its `$XDG_CONFIG_HOME/git/attributes` default,
# `$GIT_DIR/info/attributes` (highest precedence) and a template dir that can
# plant one.
#
# So this is subtractive, not a denylist: drop EVERY `GIT_*` variable inherited
# from the caller, point HOME and XDG_CONFIG_HOME at an empty directory so the
# per-user config and attributes files resolve to nothing, and disable the
# system sources explicitly. Anything new git grows is covered by construction
# unless it is reachable without a `GIT_*` variable and without HOME.
_HERMETIC_ENV: dict[str, str] = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_ATTR_NOSYSTEM": "1",
}


@pytest.fixture(scope="session", autouse=True)
def _empty_home(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Point HOME/XDG_CONFIG_HOME at an empty dir for every git call here.

    Without this, `$XDG_CONFIG_HOME/git/attributes` — which is where
    `core.attributesFile` DEFAULTS to, and needs no config entry to take effect
    — still reaches these subprocesses even with global config disabled.
    """
    empty = tmp_path_factory.mktemp("hermetic-home")
    _HERMETIC_ENV["HOME"] = str(empty)
    _HERMETIC_ENV["XDG_CONFIG_HOME"] = str(empty)


def _hermetic_env() -> dict[str, str]:
    """The caller's environment with every git-controlling variable removed."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {**base, **_HERMETIC_ENV}


def _git(*args: str, cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        input=stdin,
        env=_hermetic_env(),
    )


def _git_ok(*args: str, cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run git and fail the test on a non-zero exit rather than sailing on."""
    proc = _git(*args, cwd=cwd, stdin=stdin)
    assert proc.returncode == 0, (
        f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
    )
    return proc


def _merge_attr(path: str, cwd: Path) -> str:
    """The merge attribute git resolves for ``path`` ("unspecified" if none).

    Fails loudly on a git error or on output it does not recognise. Folding
    either into ``"unspecified"`` would be fail-OPEN: that string is the PASS
    value for every scope assertion here, so a broken helper — a typo'd
    attribute name, a run outside a repo — would leave the whole scope suite
    green while checking nothing.
    """
    proc = _git_ok("check-attr", "merge", "--", path, cwd=cwd)
    line = proc.stdout.strip()
    prefix = f"{path}: merge: "
    assert line.startswith(prefix), f"unexpected check-attr output for {path!r}: {line!r}"
    return line[len(prefix) :]


def _make_repo(tmp_path: Path, *, attributes: str) -> Path:
    """A repo whose trunk holds a CHANGELOG with an [Unreleased] heading.

    ``attributes`` selects what lands at ``.gitattributes``:

    * ``"real"``    — a copy of THIS repository's file. Grades the real artifact,
      not a reconstruction: if a union line is ever re-added, this copy starts
      carrying the driver and the conflict test goes red.
    * ``"none"``    — no file at all.
    * ``"union"``   — a SYNTHETIC union rule. Deliberately not the real file: the
      point is to exercise the driver that was removed, so it has to be written
      here rather than read from a tree that no longer contains it.

    ``_git`` already disables global and system config, so nothing ambient
    reaches this repo — including the setting that would otherwise decide these
    outcomes (a global ``merge.default=union`` resolves the merge even with no
    attributes file, which would turn the conflict tests green for a reason
    having nothing to do with this repository). An identity is set here because
    with global config off there is no longer one to inherit.
    """
    repo = tmp_path / attributes
    repo.mkdir()
    _git_ok("init", "--quiet", "-b", "trunk", cwd=repo)
    _git_ok("config", "user.email", "test@example.invalid", cwd=repo)
    _git_ok("config", "user.name", "Test", cwd=repo)

    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- a pre-existing entry\n"
    )
    if attributes == "real":
        shutil.copyfile(GITATTRIBUTES, repo / ".gitattributes")
    elif attributes == "union":
        (repo / ".gitattributes").write_text("/CHANGELOG.md merge=union\n")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "--quiet", "-m", "base", cwd=repo)
    return repo


def _branch_adding_bullet(repo: Path, branch: str, bullet: str) -> None:
    """Insert ``bullet`` at the top of the Fixed list — the collision shape."""
    _git_ok("checkout", "--quiet", "-b", branch, "trunk", cwd=repo)
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text().replace("### Fixed\n\n", f"### Fixed\n\n- {bullet}\n", 1)
    )
    _git_ok("add", "CHANGELOG.md", cwd=repo)
    _git_ok("commit", "--quiet", "-m", f"add {bullet}", cwd=repo)


def _branch_removing_the_existing_entry(repo: Path, branch: str) -> None:
    """The release-cut shape: an entry LEAVES the [Unreleased] list.

    A real release cut moves entries under a version heading; from the merge's
    point of view the part that matters is that the lines leave this hunk.
    """
    _git_ok("checkout", "--quiet", "-b", branch, "trunk", cwd=repo)
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(changelog.read_text().replace("- a pre-existing entry\n", "", 1))
    _git_ok("add", "CHANGELOG.md", cwd=repo)
    _git_ok("commit", "--quiet", "-m", f"{branch}: cut the entry", cwd=repo)


def test_a_changelog_collision_conflicts_rather_than_merging_silently(
    tmp_path: Path,
) -> None:
    """The guarantee: with THIS repo's attributes, the collision is visible.

    A conflict here is the intended outcome, not a regression. It is resolved by
    taking the base's CHANGELOG.md and moving the entry into a changelog.d/
    fragment — see .gitattributes for why that is preferred to resolving it
    automatically.
    """
    repo = _make_repo(tmp_path, attributes="real")
    _branch_adding_bullet(repo, "feature-a", "entry from branch A")
    _branch_adding_bullet(repo, "feature-b", "entry from branch B")

    _git_ok("checkout", "--quiet", "trunk", cwd=repo)
    _git_ok("merge", "--quiet", "--no-edit", "feature-a", cwd=repo)
    second = _git("merge", "--no-edit", "feature-b", cwd=repo)

    assert second.returncode != 0, (
        "the collision merged cleanly — a merge driver has been re-applied to "
        "CHANGELOG.md. A driver that resolves this silently also resurrects "
        "deletions; see test_a_union_driver_resurrects_a_deletion and "
        f".gitattributes.\nstdout: {second.stdout}\nstderr: {second.stderr}"
    )


def test_a_union_driver_resurrects_a_deletion(tmp_path: Path) -> None:
    """WHY the driver is gone — kept executable so the reason cannot decay.

    Doubles as the MOVING control for the test above: this is the one case where
    the same fixture yields a clean exit-0 merge, which is what establishes that
    the conflict there is a property of the driver and not of this harness.

    Shape, which is the one a release cut produces: one side removes an entry,
    the other inserts at the same position. Union takes lines from BOTH sides of
    the conflicting hunk, so the removal is silently undone.
    """
    repo = _make_repo(tmp_path, attributes="union")
    _branch_removing_the_existing_entry(repo, "release-cut")
    _branch_adding_bullet(repo, "feature-b", "entry from branch B")

    _git_ok("checkout", "--quiet", "trunk", cwd=repo)
    _git_ok("merge", "--quiet", "--no-edit", "release-cut", cwd=repo)
    assert "a pre-existing entry" not in (repo / "CHANGELOG.md").read_text(), (
        "fixture error: the release-cut branch did not actually remove the entry"
    )

    second = _git("merge", "--no-edit", "feature-b", cwd=repo)

    assert second.returncode == 0, (
        "expected union to resolve this silently; if it conflicts, this test no "
        f"longer demonstrates the hazard.\nstderr: {second.stderr}"
    )
    merged = (repo / "CHANGELOG.md").read_text()
    assert "entry from branch B" in merged
    # The finding: a line deliberately removed by the first merge is back, and
    # nothing about the second merge said so.
    assert "a pre-existing entry" in merged, (
        "union no longer resurrects the deleted line — if git's union driver has "
        "changed behaviour, re-examine whether .gitattributes should still "
        "refuse it"
    )


def test_no_tracked_path_resolves_to_a_union_driver() -> None:
    """Grade the whole population, not a hand-picked sample.

    A negative list can only fail on paths a reviewer thought to enumerate, and
    the paths a widened or unanchored pattern actually captures are, by
    definition, the ones nobody thought of. Since ``union`` resolves silently,
    the invariant has to be an equality over every tracked file.

    Scope of the guarantee, stated so it is not read as more than it is: this
    grades the repository's own attribute surface, under a git environment
    stripped of the caller's config and attribute sources. It says nothing about
    what a merge does on a machine whose own ``merge.default`` routes unspecified
    paths to a driver — that is a per-machine choice no committed file can pin.
    """
    assert GITATTRIBUTES.is_file(), "repo-root .gitattributes is missing"
    files = _git_ok("ls-files", "-z", cwd=REPO_ROOT)
    # Through the same helper as everything else: a second subprocess spelled by
    # hand is how this call silently kept the caller's git environment while the
    # module claimed to have none.
    proc = _git_ok(
        "check-attr",
        "--stdin",
        "-z",
        "merge",
        cwd=REPO_ROOT,
        stdin=files.stdout,
    )
    # -z output is a flat NUL-separated stream of (path, attribute, value).
    fields = proc.stdout.split("\0")
    union_paths = {fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] == "union"}
    assert union_paths == set(), (
        "no tracked path may resolve to merge=union — it resolves silently and "
        f"cannot express a deletion; got {sorted(union_paths)}"
    )


@pytest.mark.parametrize("path", MUST_NOT_BE_UNION)
def test_union_does_not_reach_paths_that_do_not_exist_yet(path: str) -> None:
    """``check-attr`` is pure pattern matching and never stats the path.

    That is what makes these assertions meaningful: they describe files nobody
    has added yet, which is exactly when an over-broad pattern does its damage —
    the rule is already in place and inherited silently.
    """
    assert _merge_attr(path, cwd=REPO_ROOT) != "union", (
        f"{path} inherited merge=union; union resolves silently and must apply "
        "only to the repo-root CHANGELOG.md"
    )
