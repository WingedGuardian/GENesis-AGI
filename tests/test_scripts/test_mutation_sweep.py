"""Controls for the mutation harness -- including the damage it must never do.

The harness mutates source files, so its failure modes are not "a wrong number
in a report": a write to the caller's tree destroys uncommitted work, and a
mis-reported survival retires a test that was actually fine. Both directions
are pinned here. Since the isolated-copy redesign the first direction has one
shape: the caller's tree is NEVER written, whatever the test under mutation
does, and the copy it runs in is always removed.

Install-agnostic: every case operates on a throwaway git repository under
`tmp_path`. No repo file is mutated by this suite, no network, no live DB.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.conftest import private_module

_REPO = Path(__file__).resolve().parent.parent.parent
# Under `scripts/ci/` because a CI job invokes it: every script ci.yml invokes
# sits in the critical review lane, and
# `test_required_check_implementations_are_critical` derives that from ci.yml.
# (The job reports; it joins the required-check ruleset once it is stable.)
_MOD = _REPO / "scripts" / "ci" / "mutation_sweep.py"

# Via conftest so the module name is restored after loading rather than left
# registered for the rest of the session.
ms = private_module("_mutation_sweep", _MOD)

# GIT_* is dropped for the fixture's own git calls for the same reason the
# harness drops it: run from inside a git hook, GIT_DIR would aim them at the
# hook's repository.
_GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=_GIT_ENV,
    ).stdout


def _commit_all(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")


def _worktrees(repo: Path) -> list[str]:
    """Registered worktrees, INCLUDING prunable ones whose directory is gone."""
    out = _git(repo, "worktree", "list", "--porcelain")
    return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")]


@pytest.fixture(autouse=True)
def _private_home(tmp_path_factory, monkeypatch):
    """Keep the sweep's copies out of the real ``~/tmp``.

    `sweep` puts its copy under ``Path.home() / "tmp"`` by default. With HOME
    private, "the copy was removed" is checkable as "that directory is empty".
    """
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


def _copies_left() -> list[Path]:
    root = Path.home() / "tmp"
    return sorted(root.iterdir()) if root.exists() else []


@pytest.fixture
def project(tmp_path):
    """A miniature COMMITTED project: one guard, one test that pins it."""
    src = tmp_path / "guard.py"
    src.write_text(
        textwrap.dedent(
            """
            def is_allowed(name):
                if name.startswith("danger"):
                    return False
                return True
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "test_guard.py").write_text(
        textwrap.dedent(
            """
            import guard
            def test_blocks_danger():
                assert guard.is_allowed("dangerous") is False
            def test_allows_ordinary():
                assert guard.is_allowed("ordinary") is True
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q")
    _commit_all(tmp_path)
    return tmp_path


def _case(project, **kw):
    base = dict(
        label="guard removed",
        path=project / "guard.py",
        anchor='if name.startswith("danger"):',
        replacement="if False:",
        test="test_guard.py::test_blocks_danger",
        why="the guard must refuse a dangerous name",
        validator="python",
    )
    base.update(kw)
    return ms.Case(**base)


def _sweep(project, cases, **kw):
    kw.setdefault("check_baseline", False)  # most cases here mutate on purpose
    return ms.sweep(cases, repo=project, python=sys.executable, timeout=120, **kw)


def _around_child(monkeypatch, before=None, after=None):
    """Run ``before(cwd)`` / ``after(cwd)`` around every pytest child the sweep
    starts. ``cwd`` is where the child runs -- the COPY."""
    real = ms._pytest

    def run(cmd, *, cwd, env, timeout):
        if before:
            before(Path(cwd))
        proc = real(cmd, cwd=cwd, env=env, timeout=timeout)
        if after:
            after(Path(cwd))
        return proc

    monkeypatch.setattr(ms, "_pytest", run)


# --------------------------------------------------------------------------
# THE HAPPY PATH, both outcomes.
# --------------------------------------------------------------------------


def test_a_mutation_the_test_notices_is_reported_as_BIT(project):
    result = _sweep(project, [_case(project)])
    assert [r.outcome for r in result.results] == [ms.BIT]
    assert result.clean


def test_a_mutation_no_test_notices_is_reported_as_SURVIVED(project):
    """The other test in the fixture does not exercise the danger branch, so
    pointing the same mutation at it must NOT read as a pass."""
    result = _sweep(project, [_case(project, test="test_guard.py::test_allows_ordinary")])
    assert [r.outcome for r in result.results] == [ms.SURVIVED]
    assert not result.clean


# --------------------------------------------------------------------------
# THE CALLER'S TREE IS NEVER WRITTEN. The sweep runs in an isolated copy.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_kw",
    [
        {},  # BIT
        {"test": "test_guard.py::test_allows_ordinary"},  # SURVIVED
        {"anchor": "not present anywhere"},  # ABORTED (anchor)
        {"replacement": "if False:\n  return ("},  # ABORTED (syntax)
        {"test": "test_guard.py::test_does_not_exist"},  # ABORTED/BIT, no result
    ],
    ids=["bit", "survived", "bad-anchor", "invalid-mutation", "missing-test"],
)
def test_the_target_is_byte_identical_afterwards(project, case_kw):
    """Whatever the outcome -- including the abort paths, and including the
    SURVIVED path where the expected exit code is zero."""
    target = project / "guard.py"
    before = target.read_bytes()
    _sweep(project, [_case(project, **case_kw)])
    assert target.read_bytes() == before


def test_the_child_imports_the_COPY_and_the_shared_file_is_never_written(
    project, monkeypatch, tmp_path_factory
):
    """THE property the redesign exists for, observed from both sides.

    From inside the child: the module the test imported lives in the copy, not
    in the caller's tree. From outside: while the child ran, the caller's file
    still held its original bytes. Together they mean the BIT below can only
    have come from the copy -- the shared file was never mutated, so a child
    importing it would have reported SURVIVED.
    """
    record = tmp_path_factory.mktemp("rec") / "imported-from"
    (project / "test_where.py").write_text(
        "import os, guard\n"
        "def test_blocks_danger():\n"
        "    with open(os.environ['RECORD'], 'a') as fh:\n"
        "        fh.write(guard.__file__ + '\\n')\n"
        "    assert guard.is_allowed('dangerous') is False\n",
        encoding="utf-8",
    )
    target = project / "guard.py"
    before = target.read_bytes()
    seen = []
    _around_child(monkeypatch, before=lambda cwd: seen.append(target.read_bytes()))
    case = _case(project, test="test_where.py::test_blocks_danger", env={"RECORD": str(record)})
    result = _sweep(project, [case])
    # Three children: unmutated, mutated, and (after the BIT) the control.
    assert seen == [before] * 3, "the caller's file was written while a child ran"
    imported = record.read_text(encoding="utf-8").split()
    assert imported and all(not Path(p).is_relative_to(project) for p in imported), imported
    assert all(Path(p).is_relative_to(Path.home() / "tmp") for p in imported), imported
    assert result.results[0].outcome == ms.BIT, result.results[0].detail


@pytest.mark.parametrize("when", ["during-validation", "during-the-test-run"])
def test_a_concurrent_edit_to_the_shared_target_is_untouched_and_the_verdict_stands(
    project, monkeypatch, when
):
    """A peer editing the caller's file mid-case used to be the hard case: a
    window between the drift check and the write was narrowed and never
    closed, and a conflict left the verdict untrustworthy. With nothing written
    to the caller's tree there is no window: the edit stands, and the verdict is
    about the copy, which nobody else can touch."""
    target = project / "guard.py"
    peer = "# a peer session edited this\n"

    def edit(*_):
        target.write_text(peer, encoding="utf-8")

    if when == "during-validation":
        real_validate = ms._validate
        monkeypatch.setattr(
            ms, "_validate", lambda case, text: (edit(), real_validate(case, text))[1]
        )
    else:
        _around_child(monkeypatch, before=edit)
    result = _sweep(project, [_case(project)])
    assert target.read_text(encoding="utf-8") == peer
    assert result.results[0].outcome == ms.BIT, result.results[0].detail
    assert result.clean


def test_the_copy_is_registered_while_it_runs_and_gone_afterwards(project, monkeypatch):
    seen = {}

    def look(cwd):
        seen["cwd"] = cwd
        seen["worktrees"] = _worktrees(project)

    _around_child(monkeypatch, before=look)
    _sweep(project, [_case(project)])
    assert seen["cwd"] != project and not seen["cwd"].is_relative_to(project)
    assert len(seen["worktrees"]) == 2, seen["worktrees"]
    assert not seen["cwd"].exists(), "the copy was left on disk"
    assert _worktrees(project) == [str(project)], "the copy is still registered"
    assert _copies_left() == []


def test_a_crash_still_removes_the_copy_and_its_registration(project, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("injected")

    monkeypatch.setattr(ms, "_pytest", boom)
    target = project / "guard.py"
    before = target.read_bytes()
    with pytest.raises(RuntimeError, match="injected"):
        _sweep(project, [_case(project)])
    assert target.read_bytes() == before
    assert _worktrees(project) == [str(project)]
    assert _copies_left() == []


def test_a_copy_its_test_destroyed_is_still_unregistered(project, monkeypatch):
    """`git worktree remove` refuses a path that is no longer a working tree
    (MEASURED: "is not a working tree", registration left behind as prunable),
    so the harness removes the registration it recorded at creation."""
    import shutil as _shutil

    _around_child(monkeypatch, after=lambda cwd: _shutil.rmtree(cwd))
    _sweep(project, [_case(project)])
    assert _worktrees(project) == [str(project)]
    assert _copies_left() == []


def test_the_copy_matches_the_callers_working_tree_not_just_HEAD(project, monkeypatch):
    """The caller is testing what is ON DISK: a modified tracked file, a new
    untracked one, a deleted one, and a change hidden behind assume-unchanged
    (which `git diff` does not report) must all be what the copy holds."""
    (project / "obsolete.txt").write_text("old\n", encoding="utf-8")
    (project / "flag.txt").write_text("old\n", encoding="utf-8")
    (project / "swap").write_text("a file at HEAD\n", encoding="utf-8")
    _commit_all(project)
    _git(project, "update-index", "--assume-unchanged", "flag.txt")
    (project / "flag.txt").write_text("new\n", encoding="utf-8")
    (project / "obsolete.txt").unlink()
    # A TYPE change: the tracked file is now a directory holding a new file.
    (project / "swap").unlink()
    (project / "swap").mkdir()
    (project / "swap" / "inner.txt").write_text("inner\n", encoding="utf-8")
    (project / "extra.txt").write_text("untracked\n", encoding="utf-8")
    with (project / "guard.py").open("a", encoding="utf-8") as fh:
        fh.write("# local edit\n")
    (project / "test_tree.py").write_text(
        "from pathlib import Path\nimport guard\n"
        "def test_tree():\n"
        "    assert Path('extra.txt').read_text() == 'untracked\\n'\n"
        "    assert not Path('obsolete.txt').exists()\n"
        "    assert Path('flag.txt').read_text() == 'new\\n'\n"
        "    assert Path('swap/inner.txt').read_text() == 'inner\\n'\n"
        "    assert '# local edit' in Path(guard.__file__).read_text()\n"
        "    assert guard.is_allowed('dangerous') is False\n",
        encoding="utf-8",
    )
    cwds = []
    _around_child(monkeypatch, before=cwds.append)
    # The BASELINE is what proves the tree assertions held in the copy; the
    # mutation run alone could not tell a tree mismatch from a catch.
    result = _sweep(project, [_case(project, test="test_tree.py::test_tree")], check_baseline=True)
    assert cwds and all(not c.is_relative_to(project) for c in cwds), cwds
    assert result.results[0].outcome == ms.BIT, result.results[0].detail


def test_an_inherited_GIT_DIR_does_not_redirect_the_copy(project, monkeypatch, tmp_path_factory):
    """Run from a git hook, GIT_DIR names the hook's repository; honouring it
    would copy THAT tree, where the target does not exist."""
    other = tmp_path_factory.mktemp("other")
    (other / "README").write_text("x\n", encoding="utf-8")
    _git(other, "init", "-q")
    _commit_all(other)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    seen = {}
    # WHERE the copy is registered is the observable: the overlay rebuilds the
    # caller's files onto any checkout, so a copy of the wrong repository can
    # still bite -- while its registration, cleanup and ignored files belong to
    # a repository the caller never named. (`_git` here scrubs GIT_* itself.)
    _around_child(
        monkeypatch,
        before=lambda cwd: seen.update(mine=_worktrees(project), theirs=_worktrees(other)),
    )
    result = _sweep(project, [_case(project)])
    assert len(seen["mine"]) == 2 and seen["theirs"] == [str(other)], seen
    assert result.results[0].outcome == ms.BIT, result.results[0].detail


def test_the_copy_goes_under_the_callers_tmp_root(project, monkeypatch, tmp_path_factory):
    root = tmp_path_factory.mktemp("big-disk")
    cwds = []
    _around_child(monkeypatch, before=cwds.append)
    _sweep(project, [_case(project)], tmp_root=root)
    assert cwds and all(c.is_relative_to(root) for c in cwds), cwds
    assert list(root.iterdir()) == []


def test_a_tmp_root_inside_the_repository_is_refused(project):
    """The copy would be written INTO the caller's tree -- the one thing the
    sweep promises never to do."""
    before = sorted(p.name for p in project.iterdir())
    with pytest.raises(ValueError, match="inside the repository"):
        _sweep(project, [_case(project)], tmp_root=project / "scratch")
    assert sorted(p.name for p in project.iterdir()) == before


def test_a_tree_that_is_not_a_git_work_tree_is_refused(tmp_path):
    (tmp_path / "guard.py").write_text("x = 1\n", encoding="utf-8")
    case = ms.Case(
        label="l",
        path=tmp_path / "guard.py",
        anchor="x = 1",
        replacement="x = 2",
        test="t.py",
        why="w",
        validator="python",
    )
    with pytest.raises(RuntimeError, match="git work tree"):
        ms.sweep([case], repo=tmp_path, python=sys.executable, timeout=60)


@pytest.mark.parametrize("scope", ["sweep", "case"])
def test_an_env_pointing_into_the_shared_tree_is_refused(project, scope):
    """A child pointed back at the caller's tree would import the UNMUTATED file
    and report SURVIVED. The copy's import roots are named repo-relative."""
    env = {"PYTHONPATH": str(project / "src")}
    case = _case(project, env=env if scope == "case" else {})
    with pytest.raises(ValueError, match="isolated copy"):
        _sweep(project, [case], env=env if scope == "sweep" else None)


def test_a_target_whose_directory_resolves_outside_the_repository_is_refused(
    project, tmp_path_factory
):
    outside = tmp_path_factory.mktemp("outside")
    victim = outside / "guard.py"
    victim.write_bytes((project / "guard.py").read_bytes())
    (project / "linked").symlink_to(outside, target_is_directory=True)
    _commit_all(project)
    with pytest.raises(ValueError, match="outside"):
        _sweep(project, [_case(project, path=project / "linked" / "guard.py")])
    assert victim.read_bytes() == (project / "guard.py").read_bytes()


# --------------------------------------------------------------------------
# BETWEEN CASES THE COPY IS RESET, whatever the last test did to it.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "damage", ["deleted", "directory", "symlink-to-outside", "hardlink-to-outside"]
)
def test_the_reset_undoes_what_a_test_did_to_the_target(
    project, monkeypatch, tmp_path_factory, damage
):
    """Whatever the first run did to the target, the mutation and the control
    restore are written as FRESH files: no write may go THROUGH what the test
    left, because a symlink or a hard link to a file outside the copy would
    otherwise carry it out of the copy."""
    import os as _os
    import stat as _stat

    _os.chmod(project / "guard.py", 0o755)  # a mode the reset must keep
    victim = tmp_path_factory.mktemp("outside") / "victim.py"
    victim.write_text("victim\n", encoding="utf-8")
    calls = {"n": 0}
    modes = []

    def wreck(cwd):
        calls["n"] += 1
        target = cwd / "guard.py"
        modes.append(_stat.S_IMODE(target.lstat().st_mode))
        if calls["n"] > 1:
            return
        target.unlink()
        if damage == "directory":
            target.mkdir()
        elif damage == "symlink-to-outside":
            target.symlink_to(victim)
        elif damage == "hardlink-to-outside":
            target.hardlink_to(victim)

    _around_child(monkeypatch, after=wreck)
    result = _sweep(project, [_case(project, label="first"), _case(project, label="second")])
    assert victim.read_text(encoding="utf-8") == "victim\n", "a reset wrote outside the copy"
    assert [r.outcome for r in result.results] == [ms.BIT, ms.BIT], [
        r.detail for r in result.results
    ]
    # Unmutated, mutated and control run, for each of the two cases.
    assert modes == [0o755] * 6, "a write used the wrong mode"


def test_damage_done_by_the_baseline_run_is_undone_before_the_case(project, monkeypatch):
    """A baseline test (or its fixture) that deletes the target used to leave
    the shared file gone. The baseline now runs in a copy of its own, so this
    pins that a case's copy still holds the pristine target."""
    calls = {"n": 0}

    def delete_after_baseline(cwd):
        calls["n"] += 1
        if calls["n"] == 1:
            (cwd / "guard.py").unlink()

    _around_child(monkeypatch, after=delete_after_baseline)
    result = _sweep(project, [_case(project)], check_baseline=True)
    assert result.results[0].outcome == ms.BIT, result.results[0].detail
    assert (project / "guard.py").exists()


def test_a_failed_mutation_write_aborts_and_the_next_case_still_runs(project, monkeypatch):
    import errno

    real_put = ms._put
    calls = {"n": 0}

    def fail_first(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_put(*a, **k)

    monkeypatch.setattr(ms, "_put", fail_first)
    result = _sweep(project, [_case(project, label="first"), _case(project, label="second")])
    assert result.results[0].outcome == ms.ABORTED
    assert "could not write the mutation" in result.results[0].detail
    assert result.results[1].outcome == ms.BIT, result.results[1].detail


def test_a_control_whose_target_cannot_be_restored_aborts_only_its_case(project, monkeypatch):
    """The control must run against the PRISTINE file; if it cannot be put back,
    the BIT is unproven and that case ABORTS. Each case has its own copy, so the
    next one is unaffected (with one shared copy it had to abort every later
    case)."""
    real_put = ms._put
    calls = {"n": 0}

    def fail_restore(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = first mutation, 2 = its restore for the control
            raise OSError("injected restore failure")
        return real_put(*a, **k)

    monkeypatch.setattr(ms, "_put", fail_restore)
    result = _sweep(project, [_case(project, label="first"), _case(project, label="second")])
    assert result.results[0].outcome == ms.ABORTED
    assert "control run" in result.results[0].detail
    assert result.results[1].outcome == ms.BIT, result.results[1].detail


def test_a_hard_linked_shared_target_is_swept_and_its_other_link_untouched(project):
    """Refused while the harness wrote the caller's inode. A copy has its own."""
    alias = project / "guard_alias.py"
    alias.hardlink_to(project / "guard.py")
    before = alias.read_bytes()
    result = _sweep(project, [_case(project)])
    assert alias.read_bytes() == before
    assert result.results[0].outcome == ms.BIT, result.results[0].detail


# --------------------------------------------------------------------------
# ABORT IS NOT A PASS. The confident false negative this class is famous for.
# --------------------------------------------------------------------------


def test_an_anchor_matching_twice_aborts(project):
    (project / "guard.py").write_text("def f():\n    x = 1\n    y = 1\n", encoding="utf-8")
    result = _sweep(project, [_case(project, anchor="= 1", replacement="= 2")])
    assert result.results[0].outcome == ms.ABORTED
    assert "matched 2x" in result.results[0].detail


def test_an_invalid_mutation_aborts_rather_than_reading_as_RED(project):
    """A mutation that does not compile breaks COLLECTION, and a nonzero exit
    then reads as a successful RED -- the test looks like it caught something it
    never saw."""
    result = _sweep(project, [_case(project, replacement="if False:\n  return (")])
    assert result.results[0].outcome == ms.ABORTED
    assert "not valid python" in result.results[0].detail


def test_a_run_that_produced_no_result_line_aborts(project):
    result = _sweep(project, [_case(project, test="test_guard.py::test_missing")])
    assert result.results[0].outcome == ms.ABORTED
    # Either guard may catch it -- the exit code fires first and reports a more
    # precise cause. The PROPERTY is that a run which did not happen never reads
    # as a verdict; pinning one specific message would make the test brittle
    # about which of the correct mechanisms got there first. Since the run
    # before the mutation, that is usually the unmutated run finding nothing.
    detail = result.results[0].detail
    assert "the run did not happen" in detail or "ran nothing" in detail, detail


def test_aborts_are_not_counted_as_clean(project):
    """`clean` must require every case to have BIT. A sweep that could not run
    half its cases has established nothing about them."""
    result = _sweep(project, [_case(project), _case(project, label="bad", anchor="nope")])
    assert result.bit and result.aborted
    assert not result.clean


# --------------------------------------------------------------------------
# READING PYTEST'S SUMMARY. One parser, exact outcome keywords, no substrings.
#
# Every string below is REAL pytest 9.0.3 output (`-q --no-header -p
# no:cacheprovider`, the harness's own flags), captured from a probe file with
# one test per outcome, unless the row says otherwise. Two earlier findings on
# this parser were the same defect wearing different words: "1 xfailed"
# CONTAINS "failed", "1 xpassed" CONTAINS "passed", and "1 Error" escaped a
# case-sensitive "error". The generator was substring matching, so the fix is
# a grammar, and the tables pin the grammar rather than a list of words.
# --------------------------------------------------------------------------

_COLOUR_PASSED = "\x1b[32m\x1b[32m\x1b[1m1 passed\x1b[0m\x1b[32m in 0.01s\x1b[0m\x1b[0m"


class _Ran:
    """A finished pytest child, as `subprocess.run` hands it back."""

    def __init__(self, rc: int, stdout: str):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = ""


def _scripted(monkeypatch, rc: int, stdout: str) -> list[str]:
    """Fake every pytest child: the MUTATED run answers ``(rc, stdout)``; the
    unmutated runs either side of it answer a pass, as the unmutated test does.
    Returns a log of "before" / "mutated" / "control" per call. For a ONE-case
    sweep with no baseline: it tells the runs apart by their order."""
    log: list[str] = []
    order = ("before", "mutated", "control")

    def run(cmd, *, cwd, env, timeout):
        kind = order[len(log)]
        log.append(kind)
        return _Ran(rc, stdout) if kind == "mutated" else _Ran(0, "1 passed in 0.01s")

    monkeypatch.setattr(ms, "_pytest", run)
    return log


@pytest.mark.parametrize(
    "stdout,counts",
    [
        ("1 passed in 0.01s", {"passed": 1}),
        ("1 failed in 0.05s", {"failed": 1}),
        ("1 xfailed in 0.05s", {"xfailed": 1}),
        ("1 xpassed in 0.01s", {"xpassed": 1}),
        ("1 skipped in 0.01s", {"skipped": 1}),
        ("1 error in 0.05s", {"error": 1}),
        # pytest pluralises exactly two keys (`_pytest/terminal.py::pluralize`).
        ("2 errors in 0.05s", {"error": 2}),
        ("1 passed, 1 warning in 0.01s", {"passed": 1, "warnings": 1}),
        ("1 passed, 2 warnings in 0.01s", {"passed": 1, "warnings": 2}),
        ("1 passed, 1 error in 0.05s", {"passed": 1, "error": 1}),
        ("9 deselected in 0.01s", {"deselected": 9}),
        (
            "2 failed, 1 passed, 4 deselected, 1 xfailed, 1 xpassed in 0.07s",
            {"failed": 2, "passed": 1, "deselected": 4, "xfailed": 1, "xpassed": 1},
        ),
        ("1 passed, 2 subtests passed in 0.01s", {"passed": 1, "subtests passed": 2}),
        # A plugin's own status key (pytest-rerunfailures' `rerun`) is READ, under
        # its own name, so a verdict can refuse it rather than never seeing it.
        ("1 failed, 1 rerun in 0.05s", {"failed": 1, "rerun": 1}),
        ("no tests ran in 0.01s", {}),
        # Default verbosity wraps the line in separators; --color=yes adds SGR codes.
        ("=" * 30 + " 1 passed in 0.01s " + "=" * 31, {"passed": 1}),
        (_COLOUR_PASSED, {"passed": 1}),
        # Over a minute, `format_session_duration` appends the timedelta.
        ("1 failed in 75.00s (0:01:15)", {"failed": 1}),
        # The FINAL summary wins over everything a run prints above it.
        (
            "F\n=== short test summary info ===\nFAILED t.py::test_a - assert False\n"
            "1 failed in 0.05s\n",
            {"failed": 1},
        ),
        # Unreadable: no summary at all, a near miss pytest never writes, an ERROR
        # report line, and the -qq form, which prints no summary line at all.
        ("", None),
        ("   \n  ", None),
        ("1 Error in 0.2s", None),
        ("ERROR test_guard.py::test_blocks_danger - RuntimeError: boom", None),
        (".                                                                        [100%]", None),
        ("1 passed", None),
    ],
)
def test_the_summary_parser_reads_exact_outcome_keywords(stdout, counts):
    parsed = ms.parse_pytest_summary(stdout)
    if counts is None:
        assert parsed is None
    else:
        assert parsed is not None and dict(parsed) == counts


# The MUTATION run: (exit code, real summary) -> verdict. The baseline gate has
# already proved the target PASSED, so anything other than plain passed/failed
# here means the run was not a clean verdict on the target -- an ABORT, never a
# score. `detail` must name what was refused.
@pytest.mark.parametrize(
    "rc,stdout,outcome,detail",
    [
        (1, "1 failed in 0.05s", ms.BIT, ""),
        (0, "1 passed in 0.01s", ms.SURVIVED, ""),
        (1, "1 failed, 2 passed in 3.00s", ms.BIT, ""),
        (0, "2 passed in 1.00s", ms.SURVIVED, ""),
        (0, "1 passed, 1 warning in 0.01s", ms.SURVIVED, ""),
        (1, "1 failed, 2 warnings in 0.10s", ms.BIT, ""),
        # A REAL OUTCOME beats a sibling deselection (the old table's lesson, kept).
        (1, "1 failed, 1 deselected in 0.10s", ms.BIT, ""),
        (0, "1 passed, 1 deselected in 0.02s", ms.SURVIVED, ""),
        (0, "1 passed, 2 subtests passed in 0.01s", ms.SURVIVED, ""),
        (1, "=" * 30 + " 1 failed in 0.05s " + "=" * 30, ms.BIT, ""),
        (0, _COLOUR_PASSED, ms.SURVIVED, ""),
        (1, "1 failed in 75.00s (0:01:15)", ms.BIT, ""),
        # AN ERROR IS NOT A RESULT: setup failure, teardown failure, several.
        (1, "1 error in 0.05s", ms.ABORTED, "error"),
        (1, "1 passed, 1 error in 0.05s", ms.ABORTED, "error"),
        (1, "1 failed, 2 errors in 0.05s", ms.ABORTED, "error"),
        # xfailed CONTAINS "failed" and xpassed CONTAINS "passed": the defect class.
        (0, "1 xfailed in 0.05s", ms.ABORTED, "xfailed"),
        (1, "1 failed, 1 xfailed in 0.05s", ms.ABORTED, "xfailed"),
        (0, "1 xpassed in 0.01s", ms.ABORTED, "xpassed"),
        (0, "1 passed, 1 xpassed in 0.01s", ms.ABORTED, "xpassed"),
        # A skip that appears only under mutation is the mutation switching the
        # test off, not the test judging it.
        (0, "1 skipped in 0.01s", ms.ABORTED, "skipped"),
        (0, "1 passed, 1 skipped in 0.01s", ms.ABORTED, "skipped"),
        (1, "1 failed, 1 rerun in 0.05s", ms.ABORTED, "rerun"),
        # Nothing ran.
        (0, "no tests ran in 0.01s", ms.ABORTED, "nothing passed or failed"),
        (0, "2 deselected in 0.01s", ms.ABORTED, "deselected"),
        # The exit code and the summary must AGREE; either alone is not a verdict.
        (0, "1 failed in 0.05s", ms.ABORTED, "disagrees"),
        (1, "1 passed in 0.01s", ms.ABORTED, "disagrees"),
        # Unreadable is never scored.
        (1, "1 Error in 0.2s", ms.ABORTED, "no readable pytest summary"),
        (
            1,
            "ERROR test_guard.py::test_blocks_danger - RuntimeError: boom",
            ms.ABORTED,
            "no readable pytest summary",
        ),
        (1, "", ms.ABORTED, "no readable pytest summary"),
    ],
)
def test_the_mutation_run_verdict_table(project, monkeypatch, rc, stdout, outcome, detail):
    log = _scripted(monkeypatch, rc, stdout)
    result = _sweep(project, [_case(project)]).results[0]
    assert result.outcome == outcome, result.detail
    assert detail in result.detail
    # A control runs after a BIT, and only then.
    assert log == (["before", "mutated", "control"] if outcome == ms.BIT else ["before", "mutated"])


# The BASELINE: (exit code, real summary) -> None (green) or a refusal naming
# the outcome. Valid ONLY when the target PASSED; a sibling deselection, a
# warning and a passing subtest ride along without changing that.
@pytest.mark.parametrize(
    "rc,stdout,problem",
    [
        (0, "1 passed in 0.01s", None),
        (0, "1 passed, 1 warning in 0.01s", None),
        (0, "1 passed, 3 deselected in 0.01s", None),
        (0, "1 passed, 2 subtests passed in 0.01s", None),
        (0, "=" * 30 + " 1 passed in 0.01s " + "=" * 31, None),
        (0, _COLOUR_PASSED, None),
        # THE FINDING. A strict xfail baselines as "1 xfailed" rc=0; the mutation
        # then makes it pass, pytest reports the strict XPASS as "1 failed" rc=1,
        # and that scored BIT although the baseline never passed.
        (0, "1 xfailed in 0.05s", "xfailed"),
        (0, "1 passed, 1 xfailed in 0.05s", "xfailed"),
        (0, "1 xpassed in 0.01s", "xpassed"),
        (0, "1 skipped in 0.01s", "skipped"),
        (0, "1 passed, 1 skipped in 0.01s", "skipped"),
        (5, "9 deselected in 0.01s", "deselected"),
        (4, "no tests ran in 0.01s", "no tests ran"),
        (1, "1 failed in 0.05s", "baseline is RED"),
        # A strict XPASS at baseline is reported as `failed` -- RED, correctly.
        (1, "1 failed in 0.01s", "baseline is RED"),
        (1, "1 error in 0.05s", "baseline is RED"),
        (1, "1 passed, 1 error in 0.05s", "baseline is RED"),
        (1, "1 passed in 0.01s", "disagrees"),
        (0, "1 Error in 0.2s", "no readable pytest summary"),
        (0, "", "no readable pytest summary"),
    ],
)
def test_the_baseline_verdict_table(project, monkeypatch, rc, stdout, problem):
    monkeypatch.setattr(ms, "_pytest", lambda *a, **k: _Ran(rc, stdout))
    got = ms.assert_green_baseline(
        [_case(project)], cwd=project, python=sys.executable, env=None, timeout=1
    )
    if problem is None:
        assert got is None
    else:
        assert got is not None and problem in got, got


def test_a_strict_xfail_target_ABORTS_the_sweep_rather_than_scoring_BIT(
    project,
    tmp_path,
):
    """End to end, through the CLI the CI gate runs: the reviewer's repro.

    The target is a strict xfail pinning a known bug. Its baseline is
    "1 xfailed" (rc 0). A mutation that FIXES the bug makes the body pass,
    pytest reports the strict XPASS as "1 failed" (rc 1), and the substring
    parser scored that BIT and exited 0 -- `1 bit, 0 SURVIVED, 0 ABORTED` --
    although the target never passed at baseline.
    """
    (project / "test_known_bug.py").write_text(
        textwrap.dedent(
            """
            import pytest
            import guard
            @pytest.mark.xfail(strict=True, reason="pins a known bug")
            def test_known_bug():
                assert guard.is_allowed("danger-x") is True
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    before = (project / "guard.py").read_bytes()
    manifest = tmp_path / "m.json"
    manifest.write_text(
        '{"cases": [{"label": "l", "path": "guard.py", '
        '"anchor": "if name.startswith(\\"danger\\"):", "replacement": "if False:", '
        '"validator": "python", '
        '"test": "test_known_bug.py::test_known_bug", "why": "w"}]}',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(_MOD), str(manifest), "--repo", str(project)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "1 bit" not in proc.stdout, out
    assert "xfailed" in out, out
    assert (project / "guard.py").read_bytes() == before


# --------------------------------------------------------------------------
# REQUIREMENTS CONTRIBUTED BY THE SESSION THAT WROTE 5 OF THE 15 HARNESSES.
# Each of these is a defect the shared version would otherwise have shipped.
# --------------------------------------------------------------------------


def test_a_validator_is_REQUIRED_never_silently_skipped():
    """The original silently skipped `compile()` for non-.py targets. An
    unvalidatable mutation breaks pytest COLLECTION, and the nonzero exit then
    reads as a successful RED -- the precise false positive the postcondition
    exists to prevent. Raised by the session mutating systemd .service.template
    files, for which no validator exists at all."""
    with pytest.raises(ValueError, match="validator"):
        ms.Case(
            label="x",
            path=Path("a.service.template"),
            anchor="a",
            replacement="b",
            test="t",
            why="w",
            validator="",
        )


def test_declaring_no_validator_requires_a_reason():
    with pytest.raises(ValueError, match="WHY"):
        ms.Case(
            label="x",
            path=Path("a.tmpl"),
            anchor="a",
            replacement="b",
            test="t",
            why="w",
            validator="none",
        )
    ms.Case(
        label="x",
        path=Path("a.tmpl"),
        anchor="a",
        replacement="b",
        test="t",
        why="w",
        validator="none: a systemd template has no validator -- "
        "systemd-analyze verify needs a rendered unit",
    )


def test_a_bash_target_is_validated_with_bash_n(project):
    sh = project / "thing.sh"
    sh.write_text("#!/bin/bash\nif true; then\n  echo ok\nfi\n", encoding="utf-8")
    bad = ms.Case(
        label="broken shell",
        path=sh,
        anchor="if true; then",
        replacement="if true; then\ndone",
        test="test_guard.py::test_allows_ordinary",
        why="w",
        validator="bash",
    )
    result = _sweep(project, [bad])
    assert result.results[0].outcome == ms.ABORTED
    assert "not valid bash" in result.results[0].detail
    assert sh.read_text(encoding="utf-8").startswith("#!/bin/bash")


def test_a_multi_edit_case_counts_EVERY_anchor(project):
    """Sibling-layer masking: removing one `raise` while a second layer still
    converts the error is behaviourally null. The fix is mutating the whole
    mechanism at once -- so a case carries N edits, and a later anchor that
    misses must ABORT rather than shipping a partial mutation that reads as
    complete."""
    case = ms.Case(
        label="two-site",
        path=project / "guard.py",
        edits=(
            ms.Edit('if name.startswith("danger"):', "if False:"),
            ms.Edit("nonexistent second anchor", "x"),
        ),
        test="test_guard.py::test_blocks_danger",
        why="w",
        validator="python",
    )
    result = _sweep(project, [case])
    assert result.results[0].outcome == ms.ABORTED
    assert "edit 2/2" in result.results[0].detail


def test_a_multi_edit_case_applies_all_edits_together(project, monkeypatch):
    case = ms.Case(
        label="two-site",
        path=project / "guard.py",
        edits=(
            ms.Edit('if name.startswith("danger"):', "if False:"),
            ms.Edit("return True", "return True  # touched"),
        ),
        test="test_guard.py::test_blocks_danger",
        why="w",
        validator="python",
    )
    texts = []
    _around_child(
        monkeypatch, before=lambda cwd: texts.append((cwd / "guard.py").read_text(encoding="utf-8"))
    )
    result = _sweep(project, [case])
    # The MUTATED text, read where the child runs, is what proves both edits
    # landed. Edit 1 bites on its own, so the verdict alone cannot. Children
    # run unmutated, mutated, unmutated: the second is the mutated one.
    seen = {"text": texts[1]}
    assert "if False:" in seen["text"]
    assert "# touched" in seen["text"]
    assert result.results[0].outcome == ms.BIT
    assert (project / "guard.py").read_text(encoding="utf-8").count("# touched") == 0


def test_a_RED_baseline_refuses_to_sweep(project):
    """If the test already fails, every mutation 'bites' and the sweep reports
    success while proving nothing -- meaningless in the most flattering
    direction."""
    (project / "test_guard.py").write_text(
        "def test_blocks_danger():\n    assert False\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="baseline is RED"):
        _sweep(project, [_case(project)], check_baseline=True)
    assert _copies_left() == [], "a refused sweep left its copy behind"


def test_a_green_baseline_lets_the_sweep_proceed(project):
    result = _sweep(project, [_case(project)], check_baseline=True)
    assert result.clean


def test_a_case_needing_absent_live_state_ABORTS_rather_than_skipping(project):
    """A skipped case and a killed one are indistinguishable in a tally, so
    '11/11 bit' can silently mean 9 ran. Raised by the session whose engine-gated
    cases were only reachable because the engine happened to be armed."""
    case = _case(project, requires=("falkordb-engine",))
    result = _sweep(project, [case], available=set())
    assert result.results[0].outcome == ms.ABORTED
    assert "falkordb-engine" in result.results[0].detail
    assert not result.clean


def test_a_declared_resource_that_IS_available_runs(project):
    result = _sweep(
        project, [_case(project, requires=("falkordb-engine",))], available={"falkordb-engine"}
    )
    assert result.results[0].outcome == ms.BIT


def test_a_cases_own_env_reaches_the_child(project):
    """THE measured reason five harnesses were forked rather than extended.

    The previous version of this test asserted `case.pytest_args == (...)` -- a
    dataclass field read back from the constructor that just stored it -- and set
    an `env` nothing observed. Deleting BOTH forwarding sites left it green. The
    property is now observed from inside the child process."""
    (project / "test_env.py").write_text(
        'import os\ndef test_flag():\n    assert os.environ["EXTRA"] == "1"\n',
        encoding="utf-8",
    )
    case = _case(
        project,
        test="test_env.py::test_flag",
        env={"EXTRA": "1"},
        why="the child must see the case's own env",
    )
    # SURVIVED == the test ran and PASSED, which it can only do if EXTRA arrived.
    assert _sweep(project, [case]).results[0].outcome == ms.SURVIVED


def test_a_cases_own_pytest_args_reach_the_child(project):
    """A -k that matches nothing deselects everything -> no result -> ABORT."""
    case = _case(project, pytest_args=("-k", "no_such_selector"))
    assert _sweep(project, [case]).results[0].outcome == ms.ABORTED


def test_HOME_and_PATH_are_in_the_allowlist_deliberately():
    """genesis.env composes path resolution from HOME, so dropping it fails in a
    confusing way -- a socket path silently resolving elsewhere rather than
    erroring."""
    env = ms._child_env({})
    assert env["HOME"] and env["PATH"]


def test_a_case_must_say_what_it_breaks():
    """`why` is required because a case whose author cannot name the property is
    usually a behaviourally-null edit -- and a GREEN from one of those is
    indistinguishable from a vacuous test."""
    with pytest.raises(ValueError, match="why"):
        ms.Case(
            label="x",
            path=Path("a.py"),
            anchor="a",
            replacement="b",
            test="t",
            why="  ",
            validator="python",
        )


def test_a_no_op_mutation_is_refused():
    with pytest.raises(ValueError, match="no-op"):
        ms.Case(
            label="x",
            path=Path("a.py"),
            anchor="same",
            replacement="same",
            test="t",
            why="something",
            validator="python",
        )


def test_an_empty_sweep_is_refused(project):
    with pytest.raises(ValueError, match="proves nothing"):
        _sweep(project, [])


def test_the_child_env_is_an_allowlist(monkeypatch):
    """A subtract-list lets a stray inherited lever change what the mutated test
    actually exercises -- a disabled guard, a pointer at another worktree."""
    monkeypatch.setenv("GENESIS_SOMETHING_DANGEROUS", "1")
    env = ms._child_env({"PYTHONPATH": "/x"})
    assert "GENESIS_SOMETHING_DANGEROUS" not in env
    assert env["PYTHONPATH"] == "/x"
    assert env["GENESIS_PYTEST_LOCK_WAIT"] == "1"


def test_the_cli_exits_nonzero_on_a_survivor(project, tmp_path):
    manifest = tmp_path / "m.json"
    manifest.write_text(
        '{"cases": [{"label": "l", "path": "guard.py", '
        '"anchor": "if name.startswith(\\"danger\\"):", "replacement": "if False:", '
        '"validator": "python", '
        '"test": "test_guard.py::test_allows_ordinary", "why": "w"}]}',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(_MOD), str(manifest), "--repo", str(project)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "SURVIVED" in proc.stdout


# --------------------------------------------------------------------------
# THE FALSE GREEN. A mutation that compiles but breaks IMPORT.
# --------------------------------------------------------------------------


def test_a_mutation_that_breaks_import_ABORTS_rather_than_reading_as_BIT(project):
    """`compile()` closes the SyntaxError door ONLY.

    MEASURED before the fix: a module-level statement that raises at import time
    compiles cleanly, pytest exits 2 with "1 error in 0.26s" on stdout, and the
    old rule (has a result line AND rc != 0) called that BIT -- while the test
    never ran. Realistic shapes: an invalid module-level `re.compile`, a deleted
    constant another line references, a changed decorator argument. The shipped
    gate targets a file that carries a module-level re.compile, so this is
    reachable, not hypothetical.
    """
    case = _case(
        project,
        anchor="def is_allowed(name):",
        replacement="raise RuntimeError('mutation broke import')\ndef is_allowed(name):",
        why="a compiling-but-unimportable mutation must not count as caught",
    )
    result = _sweep(project, [case])
    assert result.results[0].outcome == ms.ABORTED
    assert "pytest exit" in result.results[0].detail
    assert not result.clean


@pytest.mark.parametrize(
    "rc,expected",
    [
        (0, ms.SURVIVED),
        (1, ms.BIT),
        (2, ms.ABORTED),
        (3, ms.ABORTED),
        (4, ms.ABORTED),
        (5, ms.ABORTED),
    ],
)
def test_only_pytest_exit_0_and_1_mean_the_tests_ran(project, monkeypatch, rc, expected):
    """pytest's exit codes are an enumerated contract; stdout text is not.

    The summary is the one pytest WRITES for each code (rc=1 "1 failed", every
    other code "1 passed"), because the verdict now also requires the summary to
    AGREE with the code. The old fixed "1 failed, 0 passed" was a line pytest
    never emits (it prints no zero counts), and under rc=0 it is a disagreement.
    """

    _scripted(monkeypatch, rc, "1 failed in 0.10s" if rc == 1 else "1 passed in 0.10s")
    result = _sweep(project, [_case(project)])
    assert result.results[0].outcome == expected


# --------------------------------------------------------------------------
# THE BASELINE MUST RUN UNDER THE CASE'S OWN ENV.
# --------------------------------------------------------------------------


def test_the_green_baseline_uses_the_cases_own_env(project):
    """Running the baseline under the SWEEP's env gave a false RED for any case
    carrying its own env -- and it raises rather than degrading, so per-case env
    and the baseline gate were mutually exclusive."""
    (project / "test_env.py").write_text(
        'import os\ndef test_flag():\n    assert os.environ["EXTRA"] == "1"\n',
        encoding="utf-8",
    )
    case = _case(
        project,
        test="test_env.py::test_flag",
        env={"EXTRA": "1"},
        why="the baseline must see the case's env",
    )
    # check_baseline=True must NOT raise: the case's env makes its test green.
    result = _sweep(project, [case], check_baseline=True)
    assert result.results[0].outcome == ms.SURVIVED


def test_the_cli_can_declare_an_available_resource(project, tmp_path):
    """`requires` was unreachable from the CLI -- the only entry point CI uses --
    so any case declaring a resource aborted unconditionally, and the author's
    fix would have been to delete the declaration."""
    manifest = tmp_path / "m.json"
    manifest.write_text(
        '{"cases": [{"label": "l", "path": "guard.py", '
        '"anchor": "if name.startswith(\\"danger\\"):", "replacement": "if False:", '
        '"validator": "python", "requires": ["an-engine"], '
        '"test": "test_guard.py::test_blocks_danger", "why": "w"}]}',
        encoding="utf-8",
    )
    without = subprocess.run(
        [sys.executable, str(_MOD), str(manifest), "--repo", str(project)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert "ABORTED" in without.stdout and without.returncode == 1

    with_res = subprocess.run(
        [
            sys.executable,
            str(_MOD),
            str(manifest),
            "--repo",
            str(project),
            "--available",
            "an-engine",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert "BIT" in with_res.stdout, with_res.stdout + with_res.stderr


def test_a_gated_case_does_not_kill_the_whole_sweep(project):
    """Requirement 5 and requirement 4, ASSERTED TOGETHER.

    `assert_green_baseline` saw every case including ones gated on state this run
    does not have. Such a case's test fails without that state, the gate read
    that as a RED baseline, and the RuntimeError killed the sweep -- so one
    unavailable resource silently prevented every OTHER case from running.

    The existing requirement-5 test passes check_baseline=False and so could
    never see this: each mechanism was correct alone and wrong together.
    """
    gated = _case(
        project,
        label="needs an engine",
        requires=("an-engine",),
        test="test_guard.py::test_missing_entirely",
    )
    ordinary = _case(project, label="ordinary")
    result = _sweep(project, [gated, ordinary], check_baseline=True, available=set())
    outcomes = {r.case.label: r.outcome for r in result.results}
    assert outcomes["needs an engine"] == ms.ABORTED
    assert outcomes["ordinary"] == ms.BIT, "the ungated case must still have run"


def test_a_baseline_timeout_is_a_rendered_problem_not_a_traceback(project, monkeypatch):
    """Every other outcome in this module is enumerated; this was the one path
    that escaped the contract as a raw traceback."""

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(ms, "_pytest", boom)
    with pytest.raises(RuntimeError, match="timed out"):
        _sweep(project, [_case(project)], check_baseline=True)


def test_the_anti_deadlock_lever_survives_the_allowlist(monkeypatch):
    """`GENESIS_PYTEST_LOCK_HELD` tells a nested pytest not to contend with its
    own parent. Stripping it meant a sweep launched inside a locked session
    queued behind itself and died at the per-case timeout."""
    monkeypatch.delenv("GENESIS_PYTEST_LOCK_HELD", raising=False)
    assert "GENESIS_PYTEST_LOCK_HELD" not in ms._child_env({})
    monkeypatch.setenv("GENESIS_PYTEST_LOCK_HELD", "1")
    assert ms._child_env({})["GENESIS_PYTEST_LOCK_HELD"] == "1"


def test_a_fixture_error_is_an_ABORT_not_a_BIT(project, monkeypatch):
    """rc=1 with "1 error" is a setup failure, not a caught mutation.

    The exit-code check closed rc != 1 and left this open: an error arrives as
    rc=1, passed the result-line test, and scored BIT while the named test body
    never ran — the same false GREEN the exit-code check was added to prevent,
    one layer in.
    """

    class Fake:
        returncode = 1
        stdout = "1 error in 0.24s"
        stderr = ""

    monkeypatch.setattr(ms, "_pytest", lambda *a, **k: Fake())
    result = _sweep(project, [_case(project)])
    assert result.results[0].outcome == ms.ABORTED
    assert not result.clean


def test_a_symlink_target_is_REFUSED(project):
    """Mutating a symlink writes THROUGH to the referent -- which may sit
    outside the copy, in the caller's tree. Refused rather than handled, because
    the correct semantics (the link? the referent?) are genuinely ambiguous and
    a mutation harness must not guess."""
    real = project / "guard.py"
    link = project / "guard_link.py"
    link.symlink_to(real)
    before = real.read_bytes()

    result = _sweep(project, [_case(project, path=link)])
    assert result.results[0].outcome == ms.ABORTED
    assert "SYMLINK" in result.results[0].detail
    assert real.read_bytes() == before, "the referent was mutated"
    assert link.is_symlink(), "the link was replaced by a regular file"


def test_targets_with_colliding_flattened_names_stay_distinct(project):
    """`pkg/guard.py` and `pkg__guard.py` once flattened to one snapshot name,
    so a valid case ran against another file's contents. Pristine content is
    keyed by the real relative path."""
    pkg = project / "pkg"
    pkg.mkdir()
    (pkg / "guard.py").write_text(
        (project / "guard.py").read_text(encoding="utf-8") + "# nested\n", encoding="utf-8"
    )
    (project / "pkg__guard.py").write_text(
        (project / "guard.py").read_text(encoding="utf-8") + "# flat\n", encoding="utf-8"
    )
    (project / "test_pair.py").write_text(
        "import pkg.guard\nimport pkg__guard\n"
        "def test_nested():\n    assert pkg.guard.is_allowed('dangerous') is False\n"
        "def test_flat():\n    assert pkg__guard.is_allowed('dangerous') is False\n",
        encoding="utf-8",
    )
    nested = _case(project, label="nested", path=pkg / "guard.py", test="test_pair.py::test_nested")
    flat = _case(
        project, label="flat", path=project / "pkg__guard.py", test="test_pair.py::test_flat"
    )
    result = _sweep(project, [nested, flat])
    assert [r.outcome for r in result.results] == [ms.BIT, ms.BIT], [
        r.detail for r in result.results
    ]


def test_a_deep_target_path_is_swept(project):
    """Six 50-character directories: the copy adds its own prefix on top, and
    nothing on the path may flatten the whole thing into one filename."""
    deep = project
    for i in range(6):
        deep = deep / (f"d{i}" * 25)
    deep.mkdir(parents=True)
    target = deep / "guard.py"
    target.write_bytes((project / "guard.py").read_bytes())
    rel = target.relative_to(project).as_posix()
    (project / "test_deep.py").write_text(
        "import importlib.util, pathlib\n"
        f"P = pathlib.Path(__file__).parent / {rel!r}\n"
        "def test_blocks():\n"
        "    spec = importlib.util.spec_from_file_location('deep_guard', P)\n"
        "    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "    assert m.is_allowed('dangerous') is False\n",
        encoding="utf-8",
    )
    result = _sweep(project, [_case(project, path=target, test="test_deep.py::test_blocks")])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail


# --------------------------------------------------------------------------
# THE TEST MUST RUN AGAINST THE MUTATED FILE.
# --------------------------------------------------------------------------


def test_a_stale_bytecode_cache_cannot_mask_the_mutation(project, monkeypatch):
    """PYTHONDONTWRITEBYTECODE stops WRITING caches, not READING them. A
    timestamp-valid .pyc for the baseline, plus a same-length mutation written
    inside the source's recorded mtime second, makes the child import the
    BASELINE code -- a real catch reported as SURVIVED. The same-second window
    is made deterministic by putting the recorded mtime back on the copy's file
    before the run (the untracked .pyc is carried into the copy with it)."""
    import os as _os
    import py_compile

    target = project / "guard.py"
    # TIMESTAMP explicitly: under SOURCE_DATE_EPOCH py_compile defaults to a
    # checked-hash pyc, which is never stale and would make this test vacuous.
    # `cfile` explicitly: py_compile honours sys.pycache_prefix, so under an
    # inherited PYTHONPYCACHEPREFIX (MEASURED: a sweep's own child env) the pyc
    # went to the prefix instead of the tree, and this test passed vacuously.
    cfile = project / "__pycache__" / f"guard.{sys.implementation.cache_tag}.pyc"
    py_compile.compile(
        str(target),
        cfile=str(cfile),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    st = target.stat()
    _around_child(
        monkeypatch,
        before=lambda cwd: _os.utime(cwd / "guard.py", ns=(st.st_atime_ns, st.st_mtime_ns)),
    )
    # Same LENGTH as the anchor, so the cached (mtime, size) pair still matches.
    case = _case(project, anchor='startswith("danger")', replacement='startswith("dangeX")')
    result = _sweep(project, [case])
    assert result.results[0].outcome == ms.BIT, result.results[0].stdout


def test_a_relative_repo_argument_still_reaches_the_source(project):
    """`--repo <relative>` gave the child cwd=<repo> but PYTHONPATH=<repo>/src,
    which the child then resolved against its OWN cwd: <repo>/<repo>/src."""
    (project / "src").mkdir()
    (project / "guard.py").rename(project / "src" / "guard.py")
    (project / "tests").mkdir()
    (project / "test_guard.py").rename(project / "tests" / "test_guard.py")
    manifest = project.parent / "m.json"
    manifest.write_text(
        '{"cases": [{"label": "l", "path": "src/guard.py", '
        '"anchor": "if name.startswith(\\"danger\\"):", "replacement": "if False:", '
        '"validator": "python", '
        '"test": "tests/test_guard.py::test_blocks_danger", "why": "w"}]}',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(_MOD), str(manifest), "--repo", project.name],
        cwd=str(project.parent),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 bit" in proc.stdout


# --------------------------------------------------------------------------
# SOURCE ENCODING. A valid target must be mutable, and must come back in its
# own encoding.
# --------------------------------------------------------------------------


def test_a_utf8_bom_target_is_swept_and_keeps_its_bom(project, monkeypatch):
    target = project / "guard.py"
    target.write_bytes(b"\xef\xbb\xbf" + target.read_bytes())
    before = target.read_bytes()
    seen = {}
    _around_child(
        monkeypatch, before=lambda cwd: seen.setdefault("bytes", (cwd / "guard.py").read_bytes())
    )
    result = _sweep(project, [_case(project)])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail
    assert seen["bytes"].startswith(b"\xef\xbb\xbf"), "the mutation dropped the BOM"
    assert target.read_bytes() == before


def test_a_pep263_latin1_target_is_swept_in_its_own_encoding(project, monkeypatch):
    target = project / "guard.py"
    target.write_bytes(b"# -*- coding: latin-1 -*-\n# caf\xe9\n" + target.read_bytes())
    before = target.read_bytes()
    seen = {}
    _around_child(
        monkeypatch, before=lambda cwd: seen.setdefault("bytes", (cwd / "guard.py").read_bytes())
    )
    result = _sweep(project, [_case(project)])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail
    assert b"# caf\xe9\n" in seen["bytes"], "the mutation re-encoded the source"
    assert target.read_bytes() == before


def test_an_undecodable_target_ABORTS_rather_than_crashing_the_sweep(project):
    target = project / "thing.sh"
    target.write_bytes(b"#!/bin/bash\n# \xff\xfe not utf-8\necho ok\n")
    case = ms.Case(
        label="sh",
        path=target,
        anchor="echo ok",
        replacement="echo no",
        test="test_guard.py::test_blocks_danger",
        why="w",
        validator="bash",
    )
    result = _sweep(project, [case, _case(project, label="ordinary")])
    assert result.results[0].outcome == ms.ABORTED
    assert "decode" in result.results[0].detail
    assert result.results[1].outcome == ms.BIT, "one bad case killed the sweep"


# --------------------------------------------------------------------------
# MANIFEST INPUT DOMAIN. A malformed case is refused, never quietly weakened.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {
            "anchor": None,
            "replacement": "x",
            "edits": (("if name", "if not name"),),
        },  # replacement silently ignored
        {"anchor": "if name", "replacement": None},  # omission became a deletion
    ],
    ids=["replacement-without-anchor", "anchor-without-replacement"],
)
def test_anchor_and_replacement_must_come_together(kw):
    edits = tuple(ms.Edit(a, r) for a, r in kw.pop("edits", ()))
    with pytest.raises(ValueError, match="together"):
        ms.Case(
            label="x", path=Path("a.py"), test="t", why="w", validator="python", edits=edits, **kw
        )


def test_an_explicit_empty_replacement_is_still_a_deletion():
    case = ms.Case(
        label="x",
        path=Path("a.py"),
        anchor="a",
        replacement="",
        test="t",
        why="w",
        validator="python",
    )
    assert case.edits == (ms.Edit("a", ""),)


@pytest.mark.parametrize("validator", ["none:", "none:   ", "none : "])
def test_none_requires_a_NONEMPTY_reason(validator):
    with pytest.raises(ValueError, match="WHY"):
        ms.Case(
            label="x",
            path=Path("a.tmpl"),
            anchor="a",
            replacement="b",
            test="t",
            why="w",
            validator=validator,
        )


@pytest.mark.parametrize("path", ["../outside.py", "/etc/hostname", "sub/../../x.py"])
def test_a_manifest_path_may_not_escape_the_repository(tmp_path, path):
    doc = {
        "cases": [
            {
                "label": "l",
                "path": path,
                "anchor": "a",
                "replacement": "b",
                "validator": "python",
                "test": "t",
                "why": "w",
            }
        ]
    }
    with pytest.raises(ValueError, match="outside the repository"):
        ms.cases_from_json(doc, tmp_path)


def test_a_manifest_path_through_a_symlinked_directory_may_not_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    doc = {
        "cases": [
            {
                "label": "l",
                "path": "linked/x.py",
                "anchor": "a",
                "replacement": "b",
                "validator": "python",
                "test": "t",
                "why": "w",
            }
        ]
    }
    with pytest.raises(ValueError, match="outside the repository"):
        ms.cases_from_json(doc, repo)


# --------------------------------------------------------------------------
# RESULT CLASSIFICATION AND POLICY.
# --------------------------------------------------------------------------


def test_the_per_case_timeout_defaults_to_the_house_floor():
    """The repo's timeout floor is 7200s; 900s turned a legitimately slow test
    into an ABORT that reads as a broken gate."""
    import inspect

    for fn in (ms.run_case, ms.sweep):
        assert inspect.signature(fn).parameters["timeout"].default >= 7200, fn
    ap_default = ms.build_parser().parse_args(["m.json"]).timeout
    assert ap_default >= 7200


# --------------------------------------------------------------------------
# THE REWORK (replaces the shape of #1851). One block per finding it answers;
# each test here failed against the harness as it stood at a68beb794.
# --------------------------------------------------------------------------


_MARKER_TESTS = """\
import os, guard
from pathlib import Path

WHERE = {where}

def test_writes_marker_and_pins_guard():
    Path(WHERE, "MARKER").write_text("x")
    assert guard.is_allowed("dangerous") is False

def test_fails_if_marker():
    assert not Path(WHERE, "MARKER").exists()
"""


def _marker_cases(project, where: str):
    """Case A catches a real mutation and leaves a MARKER behind; case B makes a
    behaviourally NULL mutation whose test fails only if a marker exists. A B
    that reads BIT was faked by A's leftovers."""
    (project / "test_marker.py").write_text(_MARKER_TESTS.format(where=where), encoding="utf-8")
    a = _case(project, label="A", test="test_marker.py::test_writes_marker_and_pins_guard")
    b = _case(
        project,
        label="B (null mutation)",
        anchor="return True",
        replacement="return bool(1)",
        test="test_marker.py::test_fails_if_marker",
        why="a null mutation: nothing should notice it",
    )
    return [a, b]


@pytest.mark.parametrize(
    "where,b_outcome",
    [
        ("'.'", ms.SURVIVED),  # the copy's own tree
        ("os.environ['TMPDIR']", ms.SURVIVED),  # the child's temp directory
        ("os.environ['HOME']", ms.ABORTED),  # shared by every copy
    ],
    ids=["repo-tree", "tmpdir", "home"],
)
def test_a_previous_cases_leftovers_never_fake_a_BIT(project, where, b_outcome):
    """Codex P1 on #1851 (`:880`): one copy for every case, and a file case A's
    test wrote made case B's irrelevant mutation read as caught. A fresh copy
    and TMPDIR per case remove the first two channels; HOME is shared by
    design, and the control run turns that leftover into an ABORT."""
    result = _sweep(project, _marker_cases(project, where))
    a, b = result.results
    assert a.outcome == ms.BIT, a.detail
    assert b.outcome == b_outcome, b.detail
    if b_outcome == ms.ABORTED:
        assert "UNMUTATED" in b.detail
    assert not result.clean


def test_a_caller_tree_that_changes_mid_sweep_aborts_the_later_case(project, monkeypatch):
    """Copies are made one per case, so a peer edit to the caller's tree between
    two of them would put different trees under different cases. The snapshot
    fingerprint refuses that rather than judging a second tree."""
    calls = {"n": 0}

    def edit_caller(_cwd):
        calls["n"] += 1
        if calls["n"] == 1:
            (project / "peer.txt").write_text("a peer wrote this\n", encoding="utf-8")

    _around_child(monkeypatch, after=edit_caller)
    result = _sweep(project, [_case(project, label="first"), _case(project, label="second")])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail
    assert result.results[1].outcome == ms.ABORTED
    assert "changed during the sweep" in result.results[1].detail


def test_an_absolute_selector_runs_the_COPYS_test(project):
    """Codex P2 on #1851 (`:697`): an absolute node id collected the caller's
    test file, which imported the caller's unmutated source and read SURVIVED
    for a mutation the same test catches. It is rewritten repository-relative."""
    case = _case(project, test=f"{project / 'test_guard.py'}::test_blocks_danger")
    result = _sweep(project, [case])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail


@pytest.mark.parametrize("test", ["/elsewhere/test_x.py::t", "../outside/test_x.py::t"])
def test_a_selector_outside_the_repository_is_refused(project, test):
    with pytest.raises(ValueError, match="repository"):
        _sweep(project, [_case(project, test=test)])


def _alias(project, tmp_path_factory) -> Path:
    link = tmp_path_factory.mktemp("alias") / "repo"
    link.symlink_to(project, target_is_directory=True)
    return link


@pytest.mark.parametrize(
    "spelling", ["dot-segment", "symlink-alias", "relative-escape", "addopts-rootdir"]
)
def test_every_spelling_of_the_callers_tree_in_env_is_refused(project, tmp_path_factory, spelling):
    """Codex P2 on #1851 (`:677`): the lexical substring check missed
    `/x/./repo/src` and a symlink alias, and the child imported the caller's
    unmutated source. One rule now judges every path-like piece of a value."""
    value = {
        "dot-segment": f"{project.parent}/./{project.name}/src",
        "symlink-alias": f"{_alias(project, tmp_path_factory)}/src",
        "relative-escape": "../../../" + project.name + "/src",
        "addopts-rootdir": f"--rootdir={_alias(project, tmp_path_factory)}",
    }[spelling]
    key = "PYTEST_ADDOPTS" if spelling == "addopts-rootdir" else "PYTHONPATH"
    with pytest.raises(ValueError, match="isolated copy"):
        _sweep(project, [_case(project, env={key: value})])


def test_a_relative_import_root_inside_the_copy_is_accepted(project):
    """`PYTHONPATH=src` resolves inside the COPY (the child's cwd), which is the
    correct way to name an import root; the rule must not refuse it."""
    result = _sweep(project, [_case(project, env={"PYTHONPATH": "src"})])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail


@pytest.mark.parametrize("arg", ["@args.txt", "--rootdir=REPO", "-o=pythonpath=REPO/src"])
def test_a_pytest_arg_pointing_back_is_refused(project, arg):
    with pytest.raises(ValueError, match="isolated copy"):
        _sweep(project, [_case(project, pytest_args=(arg.replace("REPO", str(project)),))])


@pytest.mark.parametrize("mode", [0o000, 0o300, 0o400, 0o500, 0o600])
def test_a_directory_a_test_left_unreadable_is_still_removed(tmp_path, mode):
    """Codex P2 on #1851 (`:576`): the cleanup handler re-called `os.open(p)`
    without flags (TypeError) for a mode-000 directory, which escaped the
    copy's `finally` and left it behind. Modes 0o400 and 0o600 fail through
    `os.lstat` on a child, which chmodding the child alone cannot fix."""
    holder = tmp_path / "holder"
    stuck = holder / "stuck"
    (stuck / "sub").mkdir(parents=True)
    (stuck / "sub" / "f").write_text("x", encoding="utf-8")
    (stuck / "f").write_text("x", encoding="utf-8")
    os.chmod(stuck, mode)
    try:
        ms._force_rmtree(holder)
    finally:
        if stuck.exists():
            os.chmod(stuck, 0o700)
    assert not holder.exists()


def test_a_test_printing_non_utf8_bytes_still_gets_a_verdict(project):
    """Codex P2 on #1851 (`:692`): `text=True` raised UnicodeDecodeError on a
    stray byte (a test writing raw bytes under `-s`) before any verdict, and
    killed every remaining case with a traceback."""
    (project / "test_bytes.py").write_text(
        "import os, guard\n"
        "def test_blocks_danger():\n"
        "    os.write(1, b'\\xff\\xfe raw bytes\\n')\n"
        "    assert guard.is_allowed('dangerous') is False\n",
        encoding="utf-8",
    )
    case = _case(project, test="test_bytes.py::test_blocks_danger", pytest_args=("-s",))
    result = _sweep(project, [case])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail


def test_an_anchor_manufactured_by_an_earlier_edit_aborts(project):
    """Codex P2 on #1851 (`:736`): anchors were counted on the already-mutated
    text, so edit 1 could CREATE edit 2's anchor; the case ran with a mutation
    the manifest never described and scored a clean BIT."""
    case = _case(
        project,
        anchor=None,
        replacement=None,
        edits=(
            ms.Edit('if name.startswith("danger"):', "if False:  # MADE"),
            ms.Edit("# MADE", "# changed"),
        ),
    )
    result = _sweep(project, [case])
    assert result.results[0].outcome == ms.ABORTED
    assert "matched 0x" in result.results[0].detail


@pytest.mark.parametrize(
    "text,edits,problem",
    [
        # `str.count` counts non-overlapping matches: 'aaa'.count('aa') == 1.
        ("x = 'aaa'\n", (ms.Edit("aa", "bb"),), "2x or more"),
        ("ab\n", (ms.Edit("ab", "x"), ms.Edit("b", "y")), "overlap"),
        ("ab\n", (ms.Edit("ab", "x"), ms.Edit("ab\n", "y")), "overlap"),
    ],
)
def test_edits_are_judged_against_the_pristine_text(text, edits, problem):
    out, why = ms.apply_edits(text, edits)
    assert out == text and why is not None and problem in why, why


def test_disjoint_edits_apply_together_by_offset():
    edits = (ms.Edit("b = 2", "b = 3"), ms.Edit("a = 1", "a = 0"))
    out, why = ms.apply_edits("a = 1\nb = 2\n", edits)
    assert why is None and out == "a = 0\nb = 3\n"


def test_an_empty_anchor_is_refused():
    with pytest.raises(ValueError, match="empty anchor"):
        ms.Edit("", "x")


@pytest.mark.parametrize(
    "rc,stdout,outcome",
    [
        # REAL pytest 9.0.3 output for one failing subtest: the subtest and its
        # parent both count as `failed`; only passing subtests get their own key.
        (1, "2 failed, 2 subtests passed in 0.35s", ms.BIT),
        # The key CodeRabbit named, as a plugin would write it: a BIT only with
        # a failed test at exit 1.
        (1, "1 failed, 2 subtests failed in 0.05s", ms.BIT),
        (1, "2 subtests failed in 0.05s", ms.ABORTED),
        (0, "1 passed, 2 subtests failed in 0.05s", ms.ABORTED),
    ],
)
def test_the_subtest_verdict(rc, stdout, outcome):
    assert ms.mutation_verdict(rc, stdout)[0] == outcome


def test_a_real_all_passing_subtest_baseline_is_green():
    """REAL pytest 9.0.3 output for a test whose subtests all pass."""
    assert ms.baseline_problem("t", 0, "1 passed, 2 subtests passed in 0.29s") is None


# --------------------------------------------------------------------------
# FROM THE PRE-PR REVIEW of this rework. Each failed before its fix.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", ["quoted-o", "double-slash"])
def test_a_quoted_or_doubled_spelling_of_the_callers_tree_is_refused(
    project, tmp_path_factory, spelling
):
    """pytest runs `shlex.split` on an `-o` value and puts it at the front of
    `sys.path`, so a QUOTED path is still a path; `//repo` is `/repo`."""
    alias = _alias(project, tmp_path_factory)
    arg = {
        "quoted-o": f"-o=pythonpath='{alias}/src'",
        "double-slash": f"-o=pythonpath={project.parent}//{project.name}/src",
    }[spelling]
    with pytest.raises(ValueError, match="isolated copy"):
        _sweep(project, [_case(project, pytest_args=(arg,))])


@pytest.mark.parametrize("root", ["/abs/src", "../src"])
def test_a_pythonpath_entry_that_is_not_repository_relative_is_refused(project, root):
    """`root / p` with an absolute `p` REPLACES the copy's path, which pointed
    the children back at wherever `p` named."""
    with pytest.raises(ValueError, match="repository-relative"):
        _sweep(project, [_case(project)], pythonpath=(root,))


def test_a_sweep_of_a_subdirectory_still_refuses_the_toplevel(project):
    """Paths are judged against the git toplevel as well as `--repo`, so a
    sweep pointed at a subdirectory cannot be steered at the rest of the tree."""
    sub = project / "pkg"
    sub.mkdir()
    (sub / "guard.py").write_bytes((project / "guard.py").read_bytes())
    (sub / "test_guard.py").write_bytes((project / "test_guard.py").read_bytes())
    _commit_all(project)
    case = _case(sub, env={"PYTHONPATH": f"{project}/vendored"})
    with pytest.raises(ValueError, match="isolated copy"):
        ms.sweep([case], repo=sub, python=sys.executable, timeout=120, check_baseline=False)


def test_a_sibling_that_merely_starts_with_the_repo_path_is_accepted(project):
    """`repo-data` is not `repo`: the substring check is bounded."""
    assert ms._back_pointer(project, f"{project}-data/x") is None
    assert ms._back_pointer(project, f"{project}/x") is not None


def test_a_symlinked_absolute_selector_is_rewritten_into_the_copy(project, tmp_path_factory):
    alias = _alias(project, tmp_path_factory)
    case = _case(project, test=f"{alias / 'test_guard.py'}::test_blocks_danger")
    result = _sweep(project, [case])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail


def test_removing_a_copy_never_chmods_the_temp_root(tmp_path):
    """The cleanup handler chmodded the PARENT of whatever failed, and when the
    failing entry was the copy's holder itself that parent is the caller's
    temp root, which the sweep does not own."""
    root = tmp_path / "tmp-root"
    root.mkdir()
    os.chmod(root, 0o2775)
    holder = root / "holder"
    (holder / "sub").mkdir(parents=True)
    os.chmod(holder, 0o000)
    try:
        ms._force_rmtree(holder)
    finally:
        if holder.exists():
            os.chmod(holder, 0o700)
    assert not holder.exists()
    assert (root.stat().st_mode & 0o7777) == 0o2775


def test_an_interpreter_that_cannot_start_is_an_ABORT_not_a_traceback(project):
    case = _case(project, python=str(project / "no-such-python"))
    result = _sweep(project, [case])
    assert result.results[0].outcome == ms.ABORTED
    assert "could not start" in result.results[0].detail


def test_edits_that_cancel_out_are_refused():
    """Two disjoint edits can restore the original text; the test then passes
    and SURVIVED would blame a test that was never given a mutation."""
    out, why = ms.apply_edits("xaay", (ms.Edit("xa", "x"), ms.Edit("ay", "aay")))
    assert out == "xaay" and why is not None and "cancel out" in why


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("pytest_args", "-x", "list of strings"),
        ("requires", "falkordb-engine", "list of strings"),
        ("env", {"X": 1}, "strings to strings"),
    ],
)
def test_a_manifest_field_of_the_wrong_type_is_refused(tmp_path, field, value, match):
    """A bare string `pytest_args` used to become a tuple of its characters."""
    (tmp_path / "guard.py").write_text("x = 1\n", encoding="utf-8")
    case = {
        "label": "l",
        "path": "guard.py",
        "test": "t.py",
        "why": "w",
        "validator": "python",
        "anchor": "x = 1",
        "replacement": "x = 2",
        field: value,
    }
    with pytest.raises(ValueError, match=match):
        ms.cases_from_json({"cases": [case]}, tmp_path)


def test_a_selector_that_reads_as_an_option_is_refused(project):
    with pytest.raises(ValueError, match="pytest option"):
        _sweep(project, [_case(project, test="-p no:cacheprovider")])


@pytest.mark.parametrize(
    "patch,match",
    [
        ({"edits": [{"anchor": 1, "replacement": "y"}]}, "`edits` must be a list"),
        ({"edits": [{"anchor": "x = 1"}]}, "`edits` must be a list"),
        ({"edits": "x = 1"}, "`edits` must be a list"),
        ({"label": 3}, "`label` must be a string"),
        ({"test": ["t.py"]}, "`test` must be a string"),
        ({"python": 3}, "`python` must be a string"),
    ],
)
def test_every_case_field_of_the_wrong_type_is_refused_up_front(tmp_path, patch, match):
    """CodeRabbit on this PR: a non-string anchor raised TypeError only after the
    copy and the baseline had run, and a missing key raised a bare KeyError."""
    (tmp_path / "guard.py").write_text("x = 1\n", encoding="utf-8")
    case = {
        "label": "l",
        "path": "guard.py",
        "test": "t.py",
        "why": "w",
        "validator": "python",
        "anchor": "x = 1",
        "replacement": "x = 2",
    }
    case.update(patch)
    if "edits" in patch:
        case.pop("anchor"), case.pop("replacement")
    with pytest.raises(ValueError, match=match):
        ms.cases_from_json({"cases": [case]}, tmp_path)


@pytest.mark.parametrize(
    "extra,match",
    [
        ({"available": "an-engine"}, "manifest `available`"),
        ({"env": {"X": 1}}, "manifest `env`"),
    ],
)
def test_a_manifest_level_field_of_the_wrong_type_is_refused(project, tmp_path, extra, match):
    """A bare-string `available` became a set of characters, so a case that
    required `an-engine` ABORTED as undeclared although it was declared."""
    import json

    manifest = tmp_path / "m.json"
    doc = {
        "cases": [
            {
                "label": "l",
                "path": "guard.py",
                "test": "test_guard.py",
                "why": "w",
                "validator": "python",
                "anchor": "return True",
                "replacement": "return bool(1)",
            }
        ],
        **extra,
    }
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        ms.main([str(manifest), "--repo", str(project)])


def test_the_mutation_job_does_not_persist_checkout_credentials():
    """The job runs repository tests and needs no git credential afterwards."""
    import yaml

    doc = yaml.safe_load((_REPO / ".github" / "workflows" / "ci.yml").read_text())
    steps = doc["jobs"]["mutation-gate"]["steps"]
    checkout = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkout, "precondition: the job checks out the repository"
    assert all(s.get("with", {}).get("persist-credentials") is False for s in checkout)


# --------------------------------------------------------------------------
# FROM THE PRE-PR REVIEW of the per-case copy. Each failed before its fix.
# --------------------------------------------------------------------------

_CONSUMED_TESTS = """\
import os, guard
from pathlib import Path

M = Path(os.environ["HOME"], "MARKER")

def test_leaves_debris_when_failing():
    ok = guard.is_allowed("dangerous") is False
    if not ok:
        M.write_text("x")   # cleanup skipped because the test failed
    assert ok

def test_consumes_marker():
    existed = M.exists()
    M.unlink(missing_ok=True)   # a stale-lock style fixture
    assert not existed
"""


def test_leftover_state_the_mutated_run_uses_up_never_fakes_a_BIT(project):
    """Review repro: case A's mutated run leaves a marker in HOME; case B makes
    a NULL mutation, and its test deletes the marker while failing on it. An
    after-only control then passed and B scored a clean BIT. The unmutated run
    BEFORE the mutation sees the marker and ABORTS instead."""
    (project / "test_consumed.py").write_text(_CONSUMED_TESTS, encoding="utf-8")
    a = _case(project, label="A", test="test_consumed.py::test_leaves_debris_when_failing")
    b = _case(
        project,
        label="B (null mutation)",
        anchor="return True",
        replacement="return bool(1)",
        test="test_consumed.py::test_consumes_marker",
        why="a null mutation: nothing should notice it",
    )
    result = _sweep(project, [a, b], check_baseline=True)
    assert result.results[0].outcome == ms.BIT, result.results[0].detail
    assert result.results[1].outcome == ms.ABORTED, result.results[1].detail
    assert "UNMUTATED" in result.results[1].detail
    assert not result.clean


def _prepare_overlay(project, kind: str):
    """Put one path into the overlay BEFORE the sweep, and return the peer edit
    that changes only the property `kind` names (the path set stays the same)."""
    notes = project / "notes.txt"
    if kind == "symlink-target":
        (project / "ln").symlink_to("guard.py")

        def edit():
            (project / "ln").unlink()
            (project / "ln").symlink_to("test_guard.py")

        return edit
    notes.write_text("v0\n", encoding="utf-8")
    _commit_all(project)
    if kind == "assume-unchanged":
        _git(project, "update-index", "--assume-unchanged", "notes.txt")
    notes.write_text("v1\n", encoding="utf-8")
    if kind == "mode":
        return lambda: os.chmod(notes, 0o755)
    return lambda: notes.write_text("v2\n", encoding="utf-8")


@pytest.mark.parametrize("kind", ["content", "mode", "symlink-target", "assume-unchanged"])
def test_every_part_of_the_fingerprint_catches_mid_sweep_drift(project, monkeypatch, kind):
    """Review mutants: dropping the content hash, the mode or the link target
    from the fingerprint survived, because the only drift test changed the PATH
    set. Each edit here changes one property of a path already overlaid."""
    edit = _prepare_overlay(project, kind)
    calls = {"n": 0}

    def drift(_cwd):
        calls["n"] += 1
        if calls["n"] == 1:
            edit()

    _around_child(monkeypatch, after=drift)
    result = _sweep(project, [_case(project, label="first"), _case(project, label="second")])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail
    assert result.results[1].outcome == ms.ABORTED
    assert "changed during the sweep" in result.results[1].detail


def test_drift_during_the_baselines_stops_the_sweep(project, monkeypatch):
    """Review mutant: turning off the baseline-phase drift check survived."""
    edit = _prepare_overlay(project, "content")
    calls = {"n": 0}

    def drift(_cwd):
        calls["n"] += 1
        if calls["n"] == 1:
            edit()

    _around_child(monkeypatch, after=drift)
    other = _case(project, label="other baseline", test="test_guard.py::test_allows_ordinary")
    with pytest.raises(RuntimeError, match="while the sweep ran its baselines"):
        _sweep(project, [_case(project), other], check_baseline=True)


def test_a_case_whose_copy_cannot_be_made_aborts_alone(project, monkeypatch):
    """A file vanishing mid-overlay, or a full disk, raised out of the sweep and
    lost every earlier result; now only that case ABORTS."""
    real = ms._overlay
    calls = {"n": 0}

    def overlay(top, tree):
        calls["n"] += 1
        if calls["n"] == 3:  # 1 = snapshot copy, 2 = first case, 3 = second case
            raise RuntimeError("injected: a file vanished mid-overlay")
        return real(top, tree)

    monkeypatch.setattr(ms, "_overlay", overlay)
    result = _sweep(project, [_case(project, label="first"), _case(project, label="second")])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail
    assert result.results[1].outcome == ms.ABORTED
    assert "could not make this case's copy" in result.results[1].detail
    assert _copies_left() == []


@pytest.mark.parametrize("key", ["TMPDIR", "PYTHONPYCACHEPREFIX"])
@pytest.mark.parametrize("scope", ["sweep", "case"])
def test_env_may_not_override_what_each_copy_owns(project, key, scope):
    """A manifest TMPDIR would quietly share one temp directory between cases."""
    env = {key: "/somewhere/shared"}
    case = _case(project, env=env if scope == "case" else {})
    with pytest.raises(ValueError, match="sets for itself"):
        _sweep(project, [case], env=env if scope == "sweep" else None)
