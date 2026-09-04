"""The CHANGELOG merge driver, pinned by behaviour rather than by its text.

Two branches that each add a bullet under the same ``[Unreleased]`` heading are
not disagreeing — they are inserting at the same position. Git's default driver
calls that a conflict. Measured 2026-09-04 against ``origin/main`` 2d5ea3dd: of
49 open PRs, 21 could not merge, and 18 of those 21 conflicted on CHANGELOG.md
and nothing else.

``.gitattributes`` fixes that with ``/CHANGELOG.md merge=union``. The guarantee
is carried by :func:`test_two_branches_appending_at_the_same_position_merge_without_conflict`,
which copies the real artifact into a fresh repo and runs a real merge, paired
with its negative control — the ``check-attr`` tests below are cheap
corroboration and could pass on a repo-local override alone.

Scope is graded over the complete tracked-file population rather than a list of
paths someone thought to name. ``union`` resolves *silently*, and an unanchored
pattern captures precisely the paths nobody enumerated, so a negative allowlist
is structurally unable to catch the failure it exists for.
"""

from __future__ import annotations

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


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _git_ok(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git and fail the test on a non-zero exit rather than sailing on."""
    proc = _git(*args, cwd=cwd)
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


def _make_repo(tmp_path: Path, *, with_attributes: bool) -> Path:
    """A repo whose trunk holds a CHANGELOG with an [Unreleased] heading.

    Deliberately hermetic: the ambient environment supplies an identity, a hooks
    path and a signing setting, and a contributor whose global config turns on
    ``commit.gpgsign`` without a key would otherwise get a silent no-commit that
    surfaces later as a baffling merge failure in a test about merge drivers.
    """
    repo = tmp_path / ("with_attrs" if with_attributes else "without_attrs")
    repo.mkdir()
    _git_ok("init", "--quiet", "-b", "trunk", cwd=repo)
    _git_ok("config", "user.email", "test@example.invalid", cwd=repo)
    _git_ok("config", "user.name", "Test", cwd=repo)
    _git_ok("config", "commit.gpgsign", "false", cwd=repo)
    _git_ok("config", "core.hooksPath", str(repo / "no-such-hooks"), cwd=repo)

    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- a pre-existing entry\n"
    )
    if with_attributes:
        # Grade the REAL artifact, not a reconstruction of it. If the union line
        # is deleted, misspelled, or its pattern stops matching CHANGELOG.md,
        # this copy stops carrying the driver and the merge below conflicts.
        shutil.copyfile(GITATTRIBUTES, repo / ".gitattributes")
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


def test_two_branches_appending_at_the_same_position_merge_without_conflict(
    tmp_path: Path,
) -> None:
    """The acceptance case: the real collision shape resolves, keeping BOTH."""
    repo = _make_repo(tmp_path, with_attributes=True)
    _branch_adding_bullet(repo, "feature-a", "entry from branch A")
    _branch_adding_bullet(repo, "feature-b", "entry from branch B")

    _git_ok("checkout", "--quiet", "trunk", cwd=repo)
    _git_ok("merge", "--quiet", "--no-edit", "feature-a", cwd=repo)
    second = _git("merge", "--no-edit", "feature-b", cwd=repo)

    assert second.returncode == 0, (
        "the second branch conflicted on CHANGELOG.md — the union merge driver "
        f"is not in effect.\nstdout: {second.stdout}\nstderr: {second.stderr}"
    )
    merged = (repo / "CHANGELOG.md").read_text()
    # Union keeps both sides. Neither entry may be dropped, and no conflict
    # markers may survive into the merged file.
    assert "entry from branch A" in merged
    assert "entry from branch B" in merged
    assert "a pre-existing entry" in merged
    assert "<<<<<<<" not in merged and ">>>>>>>" not in merged


def test_without_the_attribute_the_same_merge_conflicts(tmp_path: Path) -> None:
    """Negative control: the driver is what resolves it, not the content shape.

    Without this, the test above could pass for reasons having nothing to do
    with ``.gitattributes`` and would keep passing after the rule was deleted.
    """
    repo = _make_repo(tmp_path, with_attributes=False)
    _branch_adding_bullet(repo, "feature-a", "entry from branch A")
    _branch_adding_bullet(repo, "feature-b", "entry from branch B")

    _git_ok("checkout", "--quiet", "trunk", cwd=repo)
    _git_ok("merge", "--quiet", "--no-edit", "feature-a", cwd=repo)
    second = _git("merge", "--no-edit", "feature-b", cwd=repo)

    assert second.returncode != 0, (
        "expected a CHANGELOG conflict with no .gitattributes present; if this "
        "passes, the acceptance test above proves nothing about the driver"
    )


def test_union_applies_to_exactly_one_tracked_path() -> None:
    """Grade the whole population, not a hand-picked sample.

    A negative list can only fail on paths a reviewer thought to enumerate, and
    the paths a widened or unanchored pattern actually captures are, by
    definition, the ones nobody thought of. Since ``union`` resolves silently,
    the invariant has to be an equality over every tracked file.

    Scope of the guarantee, stated so it is not read as more than it is: this
    grades the repository's own attribute surface. A developer who sets
    ``merge.default`` locally routes every *unspecified* path to that driver,
    and no committed file can prevent it — that is a per-machine choice, not
    something this repo controls.
    """
    assert GITATTRIBUTES.is_file(), "repo-root .gitattributes is missing"
    files = _git_ok("ls-files", "-z", cwd=REPO_ROOT)
    proc = subprocess.run(
        ["git", "check-attr", "--stdin", "-z", "merge"],
        cwd=REPO_ROOT,
        input=files.stdout,
        capture_output=True,
        text=True,
        check=True,
    )
    # -z output is a flat NUL-separated stream of (path, attribute, value).
    fields = proc.stdout.split("\0")
    union_paths = {fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] == "union"}
    assert union_paths == {"CHANGELOG.md"}, (
        f"merge=union must apply to CHANGELOG.md and nothing else; got {sorted(union_paths)}"
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
