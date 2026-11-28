"""Native CI storage and diagnostic consumers use one run-owned placement."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts.test_live_writes_census_workflow import _prepare, _steps

ROOT = Path(__file__).resolve().parents[2]
SELECT = re.compile(
    r"\$\{\{ steps\.census_prepare\.outcome == 'success' && "
    r"steps\.census_prepare\.outputs\.([a-z_]+) \|\| '([^']*)' \}\}"
)


def _selection(value, outputs, outcome):
    """Resolve only our asserted fixed expression, not the Actions language."""
    match = SELECT.fullmatch(value)
    if match:
        key, fallback = match.groups()
        return (outputs.get(key) if outcome == "success" else None) or fallback
    return value  # Legacy literal paths are exercised by the pre-fix native run.


def _consumers():
    by_id, steps = _steps()
    return (by_id["tests"], *(next(step for step in steps if step.get("name") == name)
            for name in ("Name the failures where truncation cannot reach", "Preserve the test report", "Suite dormancy guard")))


def _outputs(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def _run_consumer(step, outputs, env, summary):
    selected = {key: _selection(value, outputs, "success") for key, value in step.get("env", {}).items()}
    child_env = {**env, **selected, "GITHUB_STEP_SUMMARY": str(summary),
                 "PATH": str(Path(sys.executable).parent) + os.pathsep + env["PATH"]}
    return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                          cwd=ROOT, env=child_env, capture_output=True, timeout=30)


def test_all_storage_paths_require_raw_success_and_share_every_consumer():
    tests, summary, artifact, dormancy = _consumers()
    junit = "${{ steps.census_prepare.outcome == 'success' && steps.census_prepare.outputs.junit_report || 'junit.xml' }}"
    active = "${{ steps.census_prepare.outcome == 'success' && steps.census_prepare.outputs.active_file || 'active-test.txt' }}"
    cache = "${{ steps.census_prepare.outcome == 'success' && steps.census_prepare.outputs.hypothesis_directory || '' }}"
    assert tests["env"]["HYPOTHESIS_STORAGE_DIRECTORY"] == cache
    for step in (tests, summary, dormancy):
        assert step["env"]["GENESIS_JUNIT_REPORT"] == junit
        assert '"$GENESIS_JUNIT_REPORT"' in step["run"]
    assert tests["env"]["GENESIS_ACTIVE_TEST_FILE"] == summary["env"]["GENESIS_ACTIVE_TEST_FILE"] == active
    assert artifact["with"]["path"] == junit
    by_id, _ = _steps()
    assert by_id["census_upload"]["with"]["path"] == "${{ steps.census_prepare.outputs.directory }}/"


def test_prepared_storage_paths_are_siblings_and_audit_upload_excludes_runtime(tmp_path):
    run, env_file, output_file = _prepare(tmp_path)
    assert run.returncode == 0, run.stderr
    outputs = _outputs(output_file)
    audit = Path(outputs["directory"])
    runtime = Path(outputs["junit_report"]).parent
    assert audit.name == "audit" and runtime.name == "runtime"
    assert audit.parent == runtime.parent and audit != runtime
    assert _outputs(env_file)["GENESIS_LIVE_WRITES_REPORT"] == str(audit / "rows.tsv")
    assert Path(outputs["active_file"]).parent == runtime
    assert Path(outputs["hypothesis_directory"]).parent == runtime
    assert audit.is_dir() and runtime.is_dir() and list(audit.iterdir()) == list(runtime.iterdir()) == []


@pytest.mark.parametrize("outcome", ["success", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("value", ["", "/owned complete/path", "/truncated"])
def test_failed_or_partial_storage_handoff_uses_consistent_fallbacks(outcome, value):
    tests, summary, artifact, dormancy = _consumers()
    outputs = {key: value for key in ("hypothesis_directory", "junit_report", "active_file")}
    expected = value if outcome == "success" and value else "junit.xml"
    for step in (tests, summary, dormancy):
        expression = step["env"].get("GENESIS_JUNIT_REPORT", "junit.xml")
        assert SELECT.fullmatch(expression), "the real workflow must guard failed, nonempty outputs"
        assert _selection(expression, outputs, outcome) == expected
    assert _selection(artifact["with"]["path"], outputs, outcome) == expected
    for step in (tests, summary):
        assert _selection(step["env"]["GENESIS_ACTIVE_TEST_FILE"], outputs, outcome) == (value if outcome == "success" and value else "active-test.txt")
    assert _selection(tests["env"]["HYPOTHESIS_STORAGE_DIRECTORY"], outputs, outcome) == (value if outcome == "success" and value else "")


def _native(tmp_path, workers, *, result="pass"):
    prepared = tmp_path / "preparation with spaces"
    prepared.mkdir()
    run, _, output_file = _prepare(prepared)
    assert run.returncode == 0, run.stderr
    outputs = _outputs(output_file)
    step = _consumers()[0]
    selected = {key: _selection(value, outputs, "success") for key, value in step["env"].items()}
    checkout = tmp_path / "checkout with spaces"
    checkout.mkdir()
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    sentinel = tmp_path / "protected" / "sentinel"
    sentinel.parent.mkdir()
    report = Path(outputs["directory"]) / "rows.tsv"
    env = {**os.environ, **selected, "GENESIS_LIVE_WRITES_REPORT": str(report),
           "GENESIS_LIVE_WRITES_RUN_ID": "owned-storage-native", "GENESIS_PYTEST_LOCK": "0",
           "GENESIS_BIG_TMP": str(temporary), "TMPDIR": str(prepared), "CI": "1",
           "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src"))),
           "PYTHONDONTWRITEBYTECODE": "1", "OWNED_SENTINEL": str(sentinel), "OWNED_RESULT": result,
           "OWNED_BRANCH": str(ROOT)}
    # A new, explicitly owned marker belongs to this independent child. It must
    # not inherit the outer run's marker ownership or redirected HOME token.
    for key in ("GENESIS_ACTIVE_TEST_OWNER", "GENESIS_TEST_PRIVATE_HOME", "GENESIS_TEST_HOME_BEFORE",
                "PYTEST_CURRENT_TEST", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        env.pop(key, None)
    if "HYPOTHESIS_STORAGE_DIRECTORY" not in selected:
        env.pop("HYPOTHESIS_STORAGE_DIRECTORY", None)
    (checkout / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
    (checkout / "conftest.py").write_text('''
import os
from pathlib import Path
import pytest
pytest_plugins = ["tests.conftest"]
@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session):
    from tests import _live_writes as audit
    assert audit._S.installed
    assert Path(audit.__file__).resolve() == Path(os.environ["OWNED_BRANCH"]) / "tests/_live_writes.py"
    target = Path(os.environ["OWNED_SENTINEL"])
    home, roots = audit._S.real_home, audit._S.allowed_roots
    try:
        audit._S.real_home, audit._S.allowed_roots = str(target.parent), ()
        target.write_bytes(b"owned sentinel")
    finally:
        audit._S.real_home, audit._S.allowed_roots = home, roots
''')
    (checkout / "test_owned.py").write_text('''
import os
from hypothesis import given, settings, strategies as st
from hypothesis.configuration import storage_directory
from hypothesis.database import DirectoryBasedExampleDatabase
@settings(max_examples=3, deadline=None)
@given(st.integers(-2, 2))
def test_owned_property(value):
    directory = storage_directory("constants", intent_to_write=False)
    directory.create_if_missing()
    (directory.path / "owned-cache").write_bytes(b"owned")
    # CI's default profile disables its example DB. Exercise the real disk
    # store separately without changing that profile or enabling it globally.
    database = DirectoryBasedExampleDatabase(storage_directory("examples", intent_to_write=False).path)
    database.save(b"owned-key", b"owned-value")
    assert b"owned-value" in list(database.fetch(b"owned-key"))
    assert value + 0 == value
def test_owned_result():
    if os.environ["OWNED_RESULT"] == "crash":
        os._exit(7)
    assert os.environ["OWNED_RESULT"] != "failure", "owned expected failure"
''')
    junit = selected.get("GENESIS_JUNIT_REPORT", "junit.xml")
    command = [sys.executable, "-m", "pytest", "test_owned.py", "-q", "-c", "pytest.ini",
               "-p", "timeout", "-o", "timeout=60", "--junit-xml=" + junit,
               "--basetemp=" + str(temporary / "pytest")]
    if workers:
        command += ["-n", str(workers), "--dist=loadfile", "--max-worker-restart=0"]
    child = subprocess.run(command, cwd=checkout, env=env, capture_output=True, text=True, timeout=90)
    return child, outputs, checkout, env, sentinel


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize("result", ["pass", "failure"])
def test_real_pytest_storage_and_diagnostic_consumers(tmp_path, workers, result):
    child, outputs, checkout, env, sentinel = _native(tmp_path, workers, result=result)
    assert child.returncode == (0 if result == "pass" else 1), child.stdout + child.stderr
    assert Path(outputs["junit_report"]).is_file(), child.stdout + child.stderr
    assert "test_owned_result" in Path(outputs["active_file"]).read_text()
    cache = Path(outputs["hypothesis_directory"])
    assert (cache / "constants/owned-cache").is_file() and (cache / "examples").is_dir()
    assert not any((checkout / name).exists() for name in (".hypothesis", "junit.xml", "active-test.txt"))
    from tests._live_writes_report import decode

    report = Path(outputs["directory"]) / "rows.tsv"
    rows = decode(report.read_bytes())
    receipt = json.loads(Path(str(report) + ".status.json").read_text())
    assert receipt["installed"] is True and receipt["disarmed"] == "" and receipt["faults"] == 0
    assert len(receipt["expected_workers"]) == workers
    assert set(receipt["expected_workers"]) == set(receipt["merged_workers"]) == set(receipt["workers"])
    assert receipt["unreported"] == []
    assert any(row[3] == str(sentinel) for row in rows), "real seeded observations were lost"
    assert not any(".hypothesis" in row[3] or row[3].endswith(("/junit.xml", "/active-test.txt")) for row in rows), rows
    _, summary_step, artifact, dormancy = _consumers()
    summary_file = tmp_path / "summary"
    summary = _run_consumer(summary_step, outputs, env, summary_file)
    assert summary.returncode == 0, summary.stderr
    assert ("test_owned_result" in summary_file.read_text()) == (result == "failure"), summary_file.read_text()
    assert ("owned expected failure" in summary_file.read_text()) == (result == "failure"), summary_file.read_text()
    assert _selection(artifact["with"]["path"], outputs, "success") == outputs["junit_report"]
    ceiling = _run_consumer(dormancy, outputs, env, tmp_path / "unused-summary")
    assert ceiling.returncode == 0, ceiling.stdout + ceiling.stderr


def test_real_crash_keeps_relocated_marker_for_missing_report_summary(tmp_path):
    child, outputs, checkout, env, _ = _native(tmp_path, 0, result="crash")
    assert child.returncode == 7, child.stdout + child.stderr
    assert not Path(outputs["junit_report"]).exists()
    assert "test_owned_result" in Path(outputs["active_file"]).read_text()
    summary_file = tmp_path / "crash-summary"
    summary = _run_consumer(_consumers()[1], outputs, env, summary_file)
    assert summary.returncode == 0, summary.stderr
    assert "test_owned_result" in summary_file.read_text(), summary_file.read_text()


def test_real_partial_output_failure_cannot_select_a_truncated_path(tmp_path):
    by_id, _ = _steps()
    source = by_id["census_prepare"]["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    env_file, output_file = tmp_path / "env", tmp_path / "output"
    bootstrap = '''
import builtins, os, sys
original = builtins.open
class Partial:
    def __init__(self, *args, **kwargs): self.stream = original(*args, **kwargs)
    def __enter__(self): return self
    def __exit__(self, *args): self.stream.close()
    def write(self, data):
        end = data.index("junit_report=") + len("junit_report=") + 1
        self.stream.write(data[:end])
        self.stream.flush()
        raise OSError("owned partial output fault")
def opened(path, *args, **kwargs):
    return (Partial if str(path) == os.environ["GITHUB_OUTPUT"] else original)(path, *args, **kwargs)
builtins.open = opened
exec(compile(sys.argv[1], "<actual CI preparation>", "exec"))
'''
    env = {**os.environ, "TMPDIR": str(tmp_path), "GITHUB_ENV": str(env_file), "GITHUB_OUTPUT": str(output_file),
           "GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40}
    run = subprocess.run([sys.executable, "-B", "-c", bootstrap, source], env=env, capture_output=True)
    assert run.returncode != 0 and b"owned partial output fault" in run.stderr, run.stderr
    outputs = _outputs(output_file)
    assert outputs["junit_report"] == "/" and not output_file.read_bytes().endswith(b"\n")
    tests, summary, artifact, dormancy = _consumers()
    for step in (tests, summary, dormancy):
        assert _selection(step["env"]["GENESIS_JUNIT_REPORT"], outputs, "failure") == "junit.xml"
    assert _selection(artifact["with"]["path"], outputs, "failure") == "junit.xml"
    for step in (tests, summary):
        assert _selection(step["env"]["GENESIS_ACTIVE_TEST_FILE"], outputs, "failure") == "active-test.txt"
    assert _selection(tests["env"]["HYPOTHESIS_STORAGE_DIRECTORY"], outputs, "failure") == ""
