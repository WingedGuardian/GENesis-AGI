"""Controls for the mutation harness -- including the damage it must never do.

The harness EDITS SOURCE FILES IN PLACE, so its failure modes are not "a wrong
number in a report": a bad restore destroys uncommitted work, and a mis-reported
survival retires a test that was actually fine. Both directions are pinned here.

Install-agnostic: every case operates on files under `tmp_path`. No repo file is
mutated by this suite, no network, no live DB.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_MOD = _REPO / "scripts" / "mutation_sweep.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mutation_sweep", _MOD)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_mutation_sweep"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop("_mutation_sweep", None)
        raise
    return mod


ms = _load()


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
    # A collection error. The exit-code check catches this first now, but the
    # string still must not read as a result on its own.
    ("1 error in 0.24s", True),
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
