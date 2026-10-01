"""The repo's pytest config removes a passing test's ``tmp_path`` (#2649).

Runs with their own ``--basetemp`` used to keep every test directory forever,
because pytest clears a basetemp only when the same name is reused. The
``tmp_path_retention_policy = "failed"`` option in ``pyproject.toml`` makes the
``tmp_path`` fixture remove its own directory when its test passes, and keep it
when the test fails, whatever the basetemp.

The behaviour test runs a REAL inner pytest against the repo's own
``pyproject.toml`` (``-c``) on a generated test file that lives outside the
repo, so the repo's ``tests/conftest.py`` (and its box-wide test lock) is not
loaded into the inner run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

_INNER_TESTS = """
def test_pass(tmp_path):
    (tmp_path / "evidence.txt").write_text("pass")


def test_fail(tmp_path):
    (tmp_path / "evidence.txt").write_text("fail")
    assert False, "deliberate failure"
"""


def _run_inner(tmp_path: Path, *extra: str) -> Path:
    project = tmp_path / "inner"
    project.mkdir()
    test_file = project / "test_inner.py"
    test_file.write_text(_INNER_TESTS)
    basetemp = tmp_path / "basetemp"
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "-c",
            str(_PYPROJECT),
            "--rootdir",
            str(project),
            "--basetemp",
            str(basetemp),
            "-p",
            "no:cacheprovider",
            "-q",
            *extra,
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        # The inner run is two trivial tests (~3 s). Bounded because a hung
        # inner pytest would otherwise block a local run forever: pytest-timeout
        # is not installed in a local venv (see the comment on `timeout` in
        # pyproject.toml). 600 s is far past any legitimate run.
        timeout=600,
    )
    out = result.stdout + result.stderr
    # Guard the guard: the inner session must have RUN both tests, one passing
    # and one failing, or the directory assertions below say nothing.
    assert result.returncode == 1, out
    assert "1 failed, 1 passed" in out, out
    return basetemp


def test_passing_test_dir_removed_failing_kept(tmp_path):
    basetemp = _run_inner(tmp_path)
    assert (basetemp / "test_fail0" / "evidence.txt").read_text() == "fail"
    assert not (basetemp / "test_pass0").exists()


def test_harness_sees_both_dirs_without_the_policy(tmp_path):
    # Control arm: overriding the policy back to pytest's default ("all") keeps
    # both directories, so the arm above is not passing because the harness
    # never creates test_pass0 in the first place.
    basetemp = _run_inner(tmp_path, "-o", "tmp_path_retention_policy=all")
    assert (basetemp / "test_pass0" / "evidence.txt").read_text() == "pass"
    assert (basetemp / "test_fail0").is_dir()


def test_pyproject_sets_the_failed_policy():
    ini = tomllib.loads(_PYPROJECT.read_text())["tool"]["pytest"]["ini_options"]
    assert ini["tmp_path_retention_policy"] == "failed"


def test_the_running_suite_uses_the_failed_policy(pytestconfig):
    # The setting pytest actually loaded for THIS run, so a higher-precedence
    # config file (pytest.toml, .pytest.toml) cannot silently override it.
    assert pytestconfig.getini("tmp_path_retention_policy") == "failed"
