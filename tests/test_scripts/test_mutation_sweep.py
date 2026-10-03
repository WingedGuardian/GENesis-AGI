"""Controls for the mutation harness -- including the damage it must never do.

The harness EDITS SOURCE FILES IN PLACE, so its failure modes are not "a wrong
number in a report": a bad restore destroys uncommitted work, and a mis-reported
survival retires a test that was actually fine. Both directions are pinned here.

Install-agnostic: every case operates on files under `tmp_path`. No repo file is
mutated by this suite, no network, no live DB.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.conftest import private_module

_REPO = Path(__file__).resolve().parent.parent.parent
# Under `scripts/ci/` because CI invokes it as a required check: an
# implementation behind a required check sits in the critical review lane, and
# `test_required_check_implementations_are_critical` derives that from ci.yml.
_MOD = _REPO / "scripts" / "ci" / "mutation_sweep.py"

# Via conftest so the module name is restored after loading rather than left
# registered for the rest of the session.
ms = private_module("_mutation_sweep", _MOD)


@pytest.fixture(autouse=True)
def _private_home(tmp_path_factory, monkeypatch):
    """Keep the sweep's snapshot directories out of the real ``~/tmp``.

    `sweep` puts snapshots under ``Path.home() / "tmp"`` and deliberately KEEPS
    them on a crash or a CONFLICT -- which several tests here provoke on
    purpose. Without this, every run of this file left a dozen recovery
    directories in the developer's real home.
    """
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture
def project(tmp_path):
    """A miniature project: one guard, one test that pins it."""
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
    return ms.sweep(cases, cwd=project, python=sys.executable,
                    env={"PYTHONPATH": str(project)}, timeout=120, **kw)


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
# THE FILE MUST COME BACK. This is the half that can destroy work.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case_kw",
    [
        {},                                                    # BIT
        {"test": "test_guard.py::test_allows_ordinary"},       # SURVIVED
        {"anchor": "not present anywhere"},                    # ABORTED (anchor)
        {"replacement": "if False:\n  return ("},              # ABORTED (syntax)
        {"test": "test_guard.py::test_does_not_exist"},        # ABORTED/BIT, no result
    ],
    ids=["bit", "survived", "bad-anchor", "invalid-mutation", "missing-test"],
)
def test_the_target_is_byte_identical_afterwards(project, case_kw):
    """Whatever the outcome -- including the abort paths, and including the
    SURVIVED path where the expected exit code is zero and a trailing restore
    would still have run. The restore is in a `finally` precisely because the
    GOOD outcome here is a nonzero exit."""
    target = project / "guard.py"
    before = target.read_bytes()
    _sweep(project, [_case(project, **case_kw)])
    assert target.read_bytes() == before


def test_a_crash_mid_sweep_still_restores(project, monkeypatch):
    target = project / "guard.py"
    before = target.read_bytes()

    def boom(*a, **k):
        raise RuntimeError("injected")

    monkeypatch.setattr(ms.subprocess, "run", boom)
    with pytest.raises(RuntimeError):
        _sweep(project, [_case(project)])
    assert target.read_bytes() == before, "a crash must not leave the file mutated"


def test_a_concurrent_edit_is_PRESERVED_not_overwritten(project, monkeypatch):
    """THE 6-of-15 CASE. If the file changes while the test runs, that is someone
    else's uncommitted work. Restoring the snapshot over it would destroy the
    edit -- and a final hash check would happily confirm the overwrite
    succeeded."""
    target = project / "guard.py"
    real_run = ms.subprocess.run

    def edit_then_run(*a, **k):
        target.write_text("# a peer session edited this\n", encoding="utf-8")
        return real_run(*a, **k)

    monkeypatch.setattr(ms.subprocess, "run", edit_then_run)
    result = _sweep(project, [_case(project)])
    assert target.read_text(encoding="utf-8") == "# a peer session edited this\n"
    # And it must SAY SO. Asserting only the preservation locked in the silent
    # half: CONFLICT was defined, rendered and never produced, so a verdict
    # computed against a file that changed mid-flight was returned as though it
    # were trustworthy -- invisible whenever no later case touches that path.
    assert result.results[0].outcome == ms.CONFLICT
    assert not result.clean


def test_the_next_case_aborts_after_a_drift(project, monkeypatch):
    """And the drift must be REPORTED, not silently absorbed."""
    target = project / "guard.py"
    real_run = ms.subprocess.run
    calls = {"n": 0}

    def edit_on_first(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            target.write_text("# peer edit\n", encoding="utf-8")
        return real_run(*a, **k)

    monkeypatch.setattr(ms.subprocess, "run", edit_on_first)
    result = _sweep(project, [_case(project, label="first"), _case(project, label="second")])
    assert result.results[1].outcome == ms.ABORTED
    assert "differs from the sweep baseline" in result.results[1].detail


# --------------------------------------------------------------------------
# ABORT IS NOT A PASS. The confident false negative this class is famous for.
# --------------------------------------------------------------------------

def test_an_anchor_matching_twice_aborts(project):
    (project / "guard.py").write_text(
        "def f():\n    x = 1\n    y = 1\n", encoding="utf-8"
    )
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
    # about which of two correct mechanisms got there first.
    assert "the run did not happen" in result.results[0].detail


def test_aborts_are_not_counted_as_clean(project):
    """`clean` must require every case to have BIT. A sweep that could not run
    half its cases has established nothing about them."""
    result = _sweep(project, [_case(project), _case(project, label="bad", anchor="nope")])
    assert result.bit and result.aborted
    assert not result.clean


@pytest.mark.parametrize("stdout,expected", [
    ("1 failed, 2 passed in 3.0s", True),
    ("2 passed in 1.0s", True),
    ("no tests ran in 0.01s", False),
    ("", False),
    ("   ", False),
    ("1 deselected in 0.1s", False),
    ("1 passed, 1 deselected in 0.02s", True),
    # A REAL OUTCOME beats a sibling deselection. MEASURED: the old rule read
    # this as "no result", turning a genuinely-caught mutation into an ABORT.
    # The table covered the `passed` variant and not the `failed` one, which is
    # exactly how it survived.
    ("1 failed, 1 deselected in 0.1s", True),
    ("2 deselected in 0.1s", False),
    # AN ERROR IS NOT A RESULT, and this row previously asserted True --
    # encoding the bug. A fixture setup failure exits 1 with "1 error", which
    # passed the exit-code check (rc in (0,1)) and then read as a result, so the
    # case scored BIT while the test body never ran. A teardown failure produces
    # both words at once.
    ("1 error in 0.24s", False),
    ("1 passed, 1 error in 0.3s", False),
])
def test_the_result_line_detector(stdout, expected):
    assert ms._has_result_line(stdout) is expected


# --------------------------------------------------------------------------
# THE CONTRACT.
# --------------------------------------------------------------------------

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
        ms.Case(label="x", path=Path("a.service.template"), anchor="a",
                replacement="b", test="t", why="w", validator="")


def test_declaring_no_validator_requires_a_reason():
    with pytest.raises(ValueError, match="WHY"):
        ms.Case(label="x", path=Path("a.tmpl"), anchor="a", replacement="b",
                test="t", why="w", validator="none")
    ms.Case(label="x", path=Path("a.tmpl"), anchor="a", replacement="b",
            test="t", why="w",
            validator="none: a systemd template has no validator -- "
                      "systemd-analyze verify needs a rendered unit")


def test_a_bash_target_is_validated_with_bash_n(project):
    sh = project / "thing.sh"
    sh.write_text("#!/bin/bash\nif true; then\n  echo ok\nfi\n", encoding="utf-8")
    bad = ms.Case(label="broken shell", path=sh, anchor="if true; then",
                  replacement="if true; then\ndone", test="test_guard.py::test_allows_ordinary",
                  why="w", validator="bash")
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
        label="two-site", path=project / "guard.py",
        edits=(ms.Edit('if name.startswith("danger"):', "if False:"),
               ms.Edit("nonexistent second anchor", "x")),
        test="test_guard.py::test_blocks_danger", why="w", validator="python",
    )
    result = _sweep(project, [case])
    assert result.results[0].outcome == ms.ABORTED
    assert "edit 2/2" in result.results[0].detail


def test_a_multi_edit_case_applies_all_edits_together(project, monkeypatch):
    case = ms.Case(
        label="two-site", path=project / "guard.py",
        edits=(ms.Edit('if name.startswith("danger"):', "if False:"),
               ms.Edit("return True", "return True  # touched")),
        test="test_guard.py::test_blocks_danger", why="w", validator="python",
    )
    seen = {}
    real_run = ms.subprocess.run

    def capture(*a, **k):
        seen["text"] = (project / "guard.py").read_text(encoding="utf-8")
        return real_run(*a, **k)

    monkeypatch.setattr(ms.subprocess, "run", capture)
    result = _sweep(project, [case])
    # The MUTATED text is what proves both edits landed. Asserting only that
    # "# touched" is absent afterwards tests the RESTORE, and stays true whether
    # or not edit 2 ever applied -- edit 1 bites on its own.
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
        ms.sweep([_case(project)], cwd=project, python=sys.executable,
                 env={"PYTHONPATH": str(project)}, timeout=120, check_baseline=True)


def test_a_green_baseline_lets_the_sweep_proceed(project):
    result = ms.sweep([_case(project)], cwd=project, python=sys.executable,
                      env={"PYTHONPATH": str(project)}, timeout=120,
                      check_baseline=True)
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
    result = _sweep(project, [_case(project, requires=("falkordb-engine",))],
                    available={"falkordb-engine"})
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
    case = _case(project, test="test_env.py::test_flag", env={"EXTRA": "1"},
                 why="the child must see the case's own env")
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
        ms.Case(label="x", path=Path("a.py"), anchor="a", replacement="b",
                test="t", why="  ", validator="python")


def test_a_no_op_mutation_is_refused():
    with pytest.raises(ValueError, match="no-op"):
        ms.Case(label="x", path=Path("a.py"), anchor="same", replacement="same",
                test="t", why="something", validator="python")


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
        capture_output=True, text=True, timeout=180,
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


@pytest.mark.parametrize("rc,expected", [(0, ms.SURVIVED), (1, ms.BIT),
                                         (2, ms.ABORTED), (3, ms.ABORTED),
                                         (4, ms.ABORTED), (5, ms.ABORTED)])
def test_only_pytest_exit_0_and_1_mean_the_tests_ran(project, monkeypatch, rc, expected):
    """pytest's exit codes are an enumerated contract; stdout text is not."""
    class Fake:
        returncode = rc
        stdout = "1 failed, 0 passed in 0.1s"
        stderr = ""

    monkeypatch.setattr(ms.subprocess, "run", lambda *a, **k: Fake())
    result = _sweep(project, [_case(project)])
    assert result.results[0].outcome == expected


# --------------------------------------------------------------------------
# THE DATA-LOSS PATH.
# --------------------------------------------------------------------------

def test_a_child_that_DELETES_the_target_does_not_lose_it(project, monkeypatch):
    """`_sha` raises on a missing file. In the restore `finally` that exception
    escaped into `sweep`, whose cleanup then deleted the snapshot -- the only
    remaining copy. MEASURED: FileNotFoundError, target absent, zero surviving
    snapshots. Unrecoverable loss, in the tool sold on never damaging work."""
    target = project / "guard.py"
    before = target.read_bytes()
    real_run = ms.subprocess.run

    def delete_then_run(*a, **k):
        target.unlink()
        return real_run(*a, **k)

    import os as _os
    import stat as _stat
    _os.chmod(target, 0o755)
    mode_before = _stat.S_IMODE(target.stat().st_mode)

    monkeypatch.setattr(ms.subprocess, "run", delete_then_run)
    _sweep(project, [_case(project)])
    assert target.exists(), "the target was destroyed"
    assert target.read_bytes() == before
    # AND its mode. `write_bytes` on a deleted file recreates it at the default
    # creation mode -- MEASURED 0o755 -> 0o644 -- so a restored hook script would
    # come back unrunnable. A quieter version of the same damage.
    assert _stat.S_IMODE(target.stat().st_mode) == mode_before


def test_baselines_are_PRESERVED_when_the_sweep_dies(project, monkeypatch, capsys):
    """An abnormal exit is exactly when the snapshots are worth keeping."""
    monkeypatch.setattr(ms, "run_case", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        _sweep(project, [_case(project)])
    assert "baselines PRESERVED" in capsys.readouterr().err


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
    case = _case(project, test="test_env.py::test_flag", env={"EXTRA": "1"},
                 why="the baseline must see the case's env")
    # check_baseline=True must NOT raise: the case's env makes its test green.
    result = ms.sweep([case], cwd=project, python=sys.executable,
                      env={"PYTHONPATH": str(project)}, timeout=120,
                      check_baseline=True)
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
        capture_output=True, text=True, timeout=180)
    assert "ABORTED" in without.stdout and without.returncode == 1

    with_res = subprocess.run(
        [sys.executable, str(_MOD), str(manifest), "--repo", str(project),
         "--available", "an-engine"],
        capture_output=True, text=True, timeout=180)
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
    gated = _case(project, label="needs an engine", requires=("an-engine",),
                  test="test_guard.py::test_missing_entirely")
    ordinary = _case(project, label="ordinary")
    result = ms.sweep([gated, ordinary], cwd=project, python=sys.executable,
                      env={"PYTHONPATH": str(project)}, timeout=120,
                      check_baseline=True, available=set())
    outcomes = {r.case.label: r.outcome for r in result.results}
    assert outcomes["needs an engine"] == ms.ABORTED
    assert outcomes["ordinary"] == ms.BIT, "the ungated case must still have run"


def test_a_peer_edit_between_the_drift_check_and_the_write_is_not_clobbered(project, monkeypatch):
    """TOCTOU. The drift check runs BEFORE the validator (and, for a `bash`
    validator, before an out-of-process `bash -n`). A peer edit landing in that
    window was overwritten by the mutation and then "restored" to the baseline --
    silently destroying their work, which is the one thing guarantee (6) exists
    to prevent. Re-checked immediately before the write."""
    target = project / "guard.py"
    real_validate = ms._validate

    def edit_during_validation(case, text):
        target.write_text("# a peer edited during validation\n", encoding="utf-8")
        return real_validate(case, text)

    monkeypatch.setattr(ms, "_validate", edit_during_validation)
    result = _sweep(project, [_case(project)])
    assert target.read_text(encoding="utf-8") == "# a peer edited during validation\n"
    assert result.results[0].outcome == ms.CONFLICT
    assert not result.clean


def test_a_baseline_timeout_is_a_rendered_problem_not_a_traceback(project, monkeypatch):
    """Every other outcome in this module is enumerated; this was the one path
    that escaped the contract as a raw traceback."""
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(ms, "_baseline_run", boom)
    with pytest.raises(RuntimeError, match="timed out"):
        ms.sweep([_case(project)], cwd=project, python=sys.executable,
                 env={"PYTHONPATH": str(project)}, timeout=1, check_baseline=True)


def test_the_anti_deadlock_lever_survives_the_allowlist(monkeypatch):
    """`GENESIS_PYTEST_LOCK_HELD` tells a nested pytest not to contend with its
    own parent. Stripping it meant a sweep launched inside a locked session
    queued behind itself and died at the per-case timeout."""
    monkeypatch.delenv("GENESIS_PYTEST_LOCK_HELD", raising=False)
    assert "GENESIS_PYTEST_LOCK_HELD" not in ms._child_env({})
    monkeypatch.setenv("GENESIS_PYTEST_LOCK_HELD", "1")
    assert ms._child_env({})["GENESIS_PYTEST_LOCK_HELD"] == "1"


# --------------------------------------------------------------------------
# THREE P1s: a fixture error scoring as caught, and two ways to corrupt source.
# --------------------------------------------------------------------------

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

    monkeypatch.setattr(ms.subprocess, "run", lambda *a, **k: Fake())
    result = _sweep(project, [_case(project)])
    assert result.results[0].outcome == ms.ABORTED
    assert not result.clean


def test_a_symlink_target_is_REFUSED(project):
    """Mutating a symlink writes THROUGH to the referent, and the restore cannot
    put the link back — MEASURED: the referent stays mutated permanently while
    the case reports BIT. Refused rather than handled, because the correct
    semantics (restore the link? the referent? one outside the repo?) are
    genuinely ambiguous and a mutation harness must not guess."""
    real = project / "guard.py"
    link = project / "guard_link.py"
    link.symlink_to(real)
    before = real.read_bytes()

    result = _sweep(project, [_case(project, path=link)])
    assert result.results[0].outcome == ms.ABORTED
    assert "SYMLINK" in result.results[0].detail
    assert real.read_bytes() == before, "the referent was mutated"
    assert link.is_symlink(), "the link was replaced by a regular file"


def test_a_baseline_that_DELETES_the_target_is_caught_with_a_copy_surviving(
    project, monkeypatch, capsys
):
    """The snapshot used to be taken AFTER the baseline ran, so a baseline test
    (or a fixture) that deleted a target left no recovery copy anywhere — a
    permanently missing file, before a single mutation was written."""
    target = project / "guard.py"
    real_run = ms.subprocess.run

    def delete_during_baseline(*a, **k):
        if target.exists():
            target.unlink()
        return real_run(*a, **k)

    monkeypatch.setattr(ms.subprocess, "run", delete_during_baseline)
    with pytest.raises(RuntimeError, match="DELETED before any mutation"):
        ms.sweep([_case(project)], cwd=project, python=sys.executable,
                 env={"PYTHONPATH": str(project)}, timeout=120, check_baseline=True)
    err = capsys.readouterr().err
    assert "baselines PRESERVED" in err, "no recovery copy was reported"
    snap_dir = err.split("baselines PRESERVED for recovery:")[1].strip().split()[0]
    assert (Path(snap_dir)).exists(), "the recovery copy was deleted anyway"


def test_a_baseline_that_MODIFIES_the_target_refuses_to_mutate(project, monkeypatch):
    """Mutating a file that no longer matches what it was baselined against makes
    every verdict from it meaningless."""
    target = project / "guard.py"
    real_run = ms.subprocess.run

    def edit_during_baseline(*a, **k):
        target.write_text("# a fixture rewrote this\n", encoding="utf-8")
        return real_run(*a, **k)

    monkeypatch.setattr(ms.subprocess, "run", edit_during_baseline)
    with pytest.raises(RuntimeError, match="MODIFIED before any mutation"):
        ms.sweep([_case(project)], cwd=project, python=sys.executable,
                 env={"PYTHONPATH": str(project)}, timeout=120, check_baseline=True)


# --------------------------------------------------------------------------
# THE FILE'S IDENTITY. Every finding in the third review round sat on one seam:
# the harness identified "the file" by a flattened path string (the snapshot
# name) and its content hash (drift and restore checks), wrote the mutation
# outside the scope that restores it, and restored by copying bytes onto a
# pathname. A file is more than that -- a type, a mode, an inode, a link count --
# and each of these tests is one way the narrower identity damaged it.
# --------------------------------------------------------------------------

def test_a_failed_mutation_write_leaves_the_source_intact(project, monkeypatch):
    """A write that fails part-way (disk full, a file-size limit) used to sit
    BEFORE the restoring `finally`: the in-place write had already truncated the
    source, the exception escaped, and nothing put it back. Reproduced with a
    real EFBIG from RLIMIT_FSIZE rather than a mocked write, so the test does not
    depend on which write primitive the harness uses."""
    import resource
    import signal

    target = project / "guard.py"
    before = target.read_bytes()
    snap = project.parent / "guard.snapshot"
    snap.write_bytes(before)

    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    old_handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    real_validate = ms._validate

    def limit_then_validate(case, text):
        # Armed immediately before the write, after every read the case does.
        resource.setrlimit(resource.RLIMIT_FSIZE, (16, hard))
        return real_validate(case, text)

    monkeypatch.setattr(ms, "_validate", limit_then_validate)
    try:
        result = ms.run_case(_case(project), snap, cwd=project,
                             python=sys.executable, timeout=120)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        signal.signal(signal.SIGXFSZ, old_handler)
    assert target.read_bytes() == before, "a failed write left the source damaged"
    assert result.outcome == ms.ABORTED
    assert "could not write the mutation" in result.detail
    assert not [p for p in project.iterdir() if p.name.startswith(".guard.py.")], (
        "the staging file of a failed write was left behind"
    )


def test_a_hard_linked_target_is_REFUSED(project, monkeypatch):
    """A second hard link shares the inode the mutation writes. If the child then
    deletes THIS pathname, the restore recreates only this name and the other
    link stays mutated permanently while the case reports BIT."""
    target = project / "guard.py"
    alias = project / "guard_alias.py"
    alias.hardlink_to(target)
    before = target.read_bytes()
    real_run = ms.subprocess.run

    def delete_then_run(*a, **k):
        if target.exists():
            target.unlink()
        return real_run(*a, **k)

    monkeypatch.setattr(ms.subprocess, "run", delete_then_run)
    result = _sweep(project, [_case(project)])
    # The damage first, so a RED names it rather than a message mismatch.
    assert alias.read_bytes() == before, "the other link was left mutated"
    assert target.read_bytes() == before
    assert result.results[0].outcome == ms.ABORTED
    assert "HARD-LINKED" in result.results[0].detail


def test_a_target_replaced_by_a_DIRECTORY_is_a_conflict_and_keeps_the_snapshot(
    project, monkeypatch, capsys
):
    """`copy2(snapshot, target)` onto a directory writes the snapshot INSIDE it:
    the source stays unusable, the case reports its verdict as though the restore
    worked, and the completed sweep deletes the only copy."""
    target = project / "guard.py"
    before = target.read_bytes()
    real_run = ms.subprocess.run

    def replace_with_dir(*a, **k):
        proc = real_run(*a, **k)
        target.unlink()
        target.mkdir()
        return proc

    monkeypatch.setattr(ms.subprocess, "run", replace_with_dir)
    result = _sweep(project, [_case(project)])
    assert result.results[0].outcome == ms.CONFLICT
    assert list(target.iterdir()) == [], "the restore wrote into the directory"
    err = capsys.readouterr().err
    assert "baselines PRESERVED" in err, "the only copy of the source was deleted"
    snap_dir = Path(err.split("baselines PRESERVED for recovery:")[1].strip().split()[0])
    assert [p.read_bytes() for p in snap_dir.rglob("guard.py")] == [before]


def test_a_mode_only_peer_edit_is_a_CONFLICT_not_overwritten(project, monkeypatch):
    """The content hash cannot see a chmod. A peer making a hook executable while
    the test ran was silently reverted by the restore."""
    import os as _os
    import stat as _stat

    target = project / "guard.py"
    _os.chmod(target, 0o644)
    real_run = ms.subprocess.run

    def chmod_then_run(*a, **k):
        _os.chmod(target, 0o755)
        return real_run(*a, **k)

    monkeypatch.setattr(ms.subprocess, "run", chmod_then_run)
    result = _sweep(project, [_case(project)])
    assert result.results[0].outcome == ms.CONFLICT
    assert _stat.S_IMODE(target.stat().st_mode) == 0o755, "the peer's chmod was reverted"


def test_a_mode_drift_before_the_case_aborts_it(project):
    """The drift check has the same blind spot: a mode change after the snapshot
    was taken is drift, not a file that still matches its baseline."""
    import os as _os
    import shutil as _shutil

    target = project / "guard.py"
    _os.chmod(target, 0o644)
    snap = project.parent / "guard.snapshot"
    _shutil.copy2(target, snap)
    _os.chmod(target, 0o755)
    result = ms.run_case(_case(project), snap, cwd=project,
                         python=sys.executable, timeout=120)
    assert result.outcome == ms.ABORTED
    assert "differs from the sweep baseline" in result.detail


def test_snapshot_names_cannot_collide(project):
    """`pkg/guard.py` and `pkg__guard.py` flattened to the same snapshot name, so
    the later copy overwrote the earlier and a valid case aborted against
    another file's contents."""
    pkg = project / "pkg"
    pkg.mkdir()
    (pkg / "guard.py").write_text(
        (project / "guard.py").read_text(encoding="utf-8") + "# nested\n",
        encoding="utf-8")
    (project / "pkg__guard.py").write_text(
        (project / "guard.py").read_text(encoding="utf-8") + "# flat\n",
        encoding="utf-8")
    (project / "test_pair.py").write_text(
        "import pkg.guard\nimport pkg__guard\n"
        "def test_nested():\n    assert pkg.guard.is_allowed('dangerous') is False\n"
        "def test_flat():\n    assert pkg__guard.is_allowed('dangerous') is False\n",
        encoding="utf-8")
    nested = _case(project, label="nested", path=pkg / "guard.py",
                   test="test_pair.py::test_nested")
    flat = _case(project, label="flat", path=project / "pkg__guard.py",
                 test="test_pair.py::test_flat")
    result = _sweep(project, [nested, flat])
    assert [r.outcome for r in result.results] == [ms.BIT, ms.BIT], [
        r.detail for r in result.results]


def test_a_deep_target_path_does_not_overflow_the_snapshot_name(project):
    """The whole absolute path was flattened into ONE filename, which overflows
    the single-component limit long before the path limit."""
    deep = project
    for i in range(6):
        deep = deep / (f"d{i}" * 25)
    deep.mkdir(parents=True)
    target = deep / "guard.py"
    target.write_bytes((project / "guard.py").read_bytes())
    (project / "test_deep.py").write_text(
        "import importlib.util, pathlib\n"
        f"P = pathlib.Path({str(target)!r})\n"
        "def test_blocks():\n"
        "    spec = importlib.util.spec_from_file_location('deep_guard', P)\n"
        "    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "    assert m.is_allowed('dangerous') is False\n",
        encoding="utf-8")
    result = _sweep(project, [_case(project, path=target, test="test_deep.py::test_blocks")])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail


# --------------------------------------------------------------------------
# THE TEST MUST RUN AGAINST THE MUTATED COPY.
# --------------------------------------------------------------------------

def test_a_stale_bytecode_cache_cannot_mask_the_mutation(project, monkeypatch):
    """PYTHONDONTWRITEBYTECODE stops WRITING caches, not READING them. A
    timestamp-valid .pyc for the baseline, plus a same-length mutation written
    inside the source's recorded mtime second, makes the child import the
    BASELINE code -- a real catch reported as SURVIVED. The same-second window
    is made deterministic by putting the original mtime back before the run."""
    import os as _os
    import py_compile

    target = project / "guard.py"
    # TIMESTAMP explicitly: under SOURCE_DATE_EPOCH py_compile defaults to a
    # checked-hash pyc, which is never stale and would make this test vacuous.
    py_compile.compile(str(target), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    st = target.stat()
    real_run = ms.subprocess.run

    def same_second_then_run(*a, **k):
        _os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        return real_run(*a, **k)

    monkeypatch.setattr(ms.subprocess, "run", same_second_then_run)
    # Same LENGTH as the anchor, so the cached (mtime, size) pair still matches.
    case = _case(project, anchor='startswith("danger")',
                 replacement='startswith("dangeX")')
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
        cwd=str(project.parent), capture_output=True, text=True, timeout=180,
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
    real_run = ms.subprocess.run

    def capture(*a, **k):
        seen["bytes"] = target.read_bytes()
        return real_run(*a, **k)

    monkeypatch.setattr(ms.subprocess, "run", capture)
    result = _sweep(project, [_case(project)])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail
    assert seen["bytes"].startswith(b"\xef\xbb\xbf"), "the mutation dropped the BOM"
    assert target.read_bytes() == before


def test_a_pep263_latin1_target_is_swept_in_its_own_encoding(project, monkeypatch):
    target = project / "guard.py"
    target.write_bytes(
        b"# -*- coding: latin-1 -*-\n# caf\xe9\n" + target.read_bytes())
    before = target.read_bytes()
    seen = {}
    real_run = ms.subprocess.run

    def capture(*a, **k):
        seen["bytes"] = target.read_bytes()
        return real_run(*a, **k)

    monkeypatch.setattr(ms.subprocess, "run", capture)
    result = _sweep(project, [_case(project)])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail
    assert b"# caf\xe9\n" in seen["bytes"], "the mutation re-encoded the source"
    assert target.read_bytes() == before


def test_an_undecodable_target_ABORTS_rather_than_crashing_the_sweep(project):
    target = project / "thing.sh"
    target.write_bytes(b"#!/bin/bash\n# \xff\xfe not utf-8\necho ok\n")
    case = ms.Case(label="sh", path=target, anchor="echo ok", replacement="echo no",
                   test="test_guard.py::test_blocks_danger", why="w", validator="bash")
    result = _sweep(project, [case, _case(project, label="ordinary")])
    assert result.results[0].outcome == ms.ABORTED
    assert "decode" in result.results[0].detail
    assert result.results[1].outcome == ms.BIT, "one bad case killed the sweep"


# --------------------------------------------------------------------------
# MANIFEST INPUT DOMAIN. A malformed case is refused, never quietly weakened.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [
    {"anchor": None, "replacement": "x",
     "edits": (("if name", "if not name"),)},          # replacement silently ignored
    {"anchor": "if name", "replacement": None},       # omission became a deletion
], ids=["replacement-without-anchor", "anchor-without-replacement"])
def test_anchor_and_replacement_must_come_together(kw):
    edits = tuple(ms.Edit(a, r) for a, r in kw.pop("edits", ()))
    with pytest.raises(ValueError, match="together"):
        ms.Case(label="x", path=Path("a.py"), test="t", why="w",
                validator="python", edits=edits, **kw)


def test_an_explicit_empty_replacement_is_still_a_deletion():
    case = ms.Case(label="x", path=Path("a.py"), anchor="a", replacement="",
                   test="t", why="w", validator="python")
    assert case.edits == (ms.Edit("a", ""),)


@pytest.mark.parametrize("validator", ["none:", "none:   ", "none : "])
def test_none_requires_a_NONEMPTY_reason(validator):
    with pytest.raises(ValueError, match="WHY"):
        ms.Case(label="x", path=Path("a.tmpl"), anchor="a", replacement="b",
                test="t", why="w", validator=validator)


@pytest.mark.parametrize("path", ["../outside.py", "/etc/hostname", "sub/../../x.py"])
def test_a_manifest_path_may_not_escape_the_repository(tmp_path, path):
    doc = {"cases": [{"label": "l", "path": path, "anchor": "a", "replacement": "b",
                      "validator": "python", "test": "t", "why": "w"}]}
    with pytest.raises(ValueError, match="outside the repository"):
        ms.cases_from_json(doc, tmp_path)


def test_a_manifest_path_through_a_symlinked_directory_may_not_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    doc = {"cases": [{"label": "l", "path": "linked/x.py", "anchor": "a",
                      "replacement": "b", "validator": "python", "test": "t",
                      "why": "w"}]}
    with pytest.raises(ValueError, match="outside the repository"):
        ms.cases_from_json(doc, repo)


# --------------------------------------------------------------------------
# RESULT CLASSIFICATION AND POLICY.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stdout", [
    "ERROR test_guard.py::test_blocks_danger - RuntimeError: boom",
    "1 Error in 0.2s",
])
def test_an_error_line_in_any_case_is_not_a_result(stdout):
    assert ms._has_result_line(stdout) is False


def test_the_per_case_timeout_defaults_to_the_house_floor():
    """The repo's timeout floor is 7200s; 900s turned a legitimately slow test
    into an ABORT that reads as a broken gate."""
    import inspect

    for fn in (ms.run_case, ms.sweep):
        assert inspect.signature(fn).parameters["timeout"].default >= 7200, fn
    ap_default = ms.build_parser().parse_args(["m.json"]).timeout
    assert ap_default >= 7200


def test_the_mutation_job_does_not_persist_checkout_credentials():
    """The job runs repository tests and needs no git credential afterwards."""
    import yaml

    doc = yaml.safe_load((_REPO / ".github" / "workflows" / "ci.yml").read_text())
    steps = doc["jobs"]["mutation-gate"]["steps"]
    checkout = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkout, "precondition: the job checks out the repository"
    assert all(s.get("with", {}).get("persist-credentials") is False for s in checkout)


def test_an_atomic_rewrite_of_the_SAME_bytes_is_still_restored(project, monkeypatch):
    """The control for leaving the inode OUT of the fingerprint. A fixture that
    atomically rewrites the target with the very bytes the mutation wrote has not
    edited anything; reading it as a CONFLICT would leave the mutation in the
    tree."""
    import os as _os

    target = project / "guard.py"
    before = target.read_bytes()
    real_run = ms.subprocess.run

    def atomic_same_bytes_then_run(*a, **k):
        staged = project / "staged.tmp"
        staged.write_bytes(target.read_bytes())
        _os.chmod(staged, target.stat().st_mode & 0o7777)
        _os.replace(staged, target)
        return real_run(*a, **k)

    monkeypatch.setattr(ms.subprocess, "run", atomic_same_bytes_then_run)
    result = _sweep(project, [_case(project)])
    assert result.results[0].outcome == ms.BIT, result.results[0].detail
    assert target.read_bytes() == before
