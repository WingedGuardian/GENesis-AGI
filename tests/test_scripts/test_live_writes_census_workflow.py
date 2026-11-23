"""Exercise the actual CI preparation script and its advisory wiring."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml


def _steps():
    path = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
    steps = yaml.safe_load(path.read_text())["jobs"]["test"]["steps"]
    return {step["id"]: step for step in steps if "id" in step}, steps


def test_measurement_steps_are_advisory_and_keep_the_existing_test_population():
    by_id, steps = _steps()
    for name in ("census_prepare", "census_readback", "census_upload"):
        assert name in by_id, f"CI does not wire the {name} measurement"
        assert by_id[name].get("continue-on-error") is True
    tests = by_id["tests"]
    assert tests.get("continue-on-error", False) is False
    assert "if" not in tests  # a failed advisory prepare must not suppress tests
    assert steps.index(by_id["census_prepare"]) < steps.index(tests)
    assert steps.index(tests) < steps.index(by_id["census_readback"]) < steps.index(by_id["census_upload"])
    test_args = shlex.split(tests["run"], comments=True)
    census_args = shlex.split(by_id["census_readback"]["run"])
    assert test_args[test_args.index("-n") + 1] == census_args[census_args.index("--workers") + 1] == "4"
    for flag in ("--dist=loadfile", "--max-worker-restart=0", "--junit-xml=junit.xml",
                 "--ignore-glob=**/test_*az*.py", "--ignore-glob=**/test_*shim*.py", "--ignore=tests/test_ui/"):
        assert flag in test_args
    assert by_id["census_readback"]["env"]["TESTS_OUTCOME"] == "${{ steps.tests.outcome }}"
    assert '"$TESTS_OUTCOME"' in by_id["census_readback"]["run"]
    expected_guard = "${{ !cancelled() && steps.census_prepare.outcome == 'success' && (steps.tests.outcome == 'success' || steps.tests.outcome == 'failure') }}"
    for name in ("census_readback", "census_upload"):
        assert " ".join(by_id[name]["if"].split()) == expected_guard
    upload = by_id["census_upload"]["with"]
    assert upload["path"] == "${{ steps.census_prepare.outputs.directory }}/"
    assert upload["retention-days"] == 14
    assert "github.run_attempt" in upload["name"] and "github.run_id" in upload["name"]
    assert "steps.census_readback.outcome" in upload["name"]


def _prepare(tmp_path, *, fail=False):
    by_id, _ = _steps()
    env_file = tmp_path / ("missing/env" if fail else "env")
    output_file = tmp_path / "output"
    env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
           "TMPDIR": str(tmp_path), "GITHUB_ENV": str(env_file),
           "GITHUB_OUTPUT": str(output_file), "GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "2",
           "GITHUB_SHA": "a" * 40}
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", by_id["census_prepare"]["run"]],
                            env=env, cwd=tmp_path, capture_output=True)
    return result, env_file, output_file


def test_actual_preparation_creates_fresh_allowed_temp_paths_and_exact_identity(tmp_path):
    directories = []
    for _ in range(2):
        result, env_file, output_file = _prepare(tmp_path)
        assert result.returncode == 0, result.stderr
        values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
        outputs = dict(line.split("=", 1) for line in output_file.read_text().splitlines())
        directory = Path(outputs["directory"])
        assert directory.is_relative_to(tmp_path) and directory.is_dir()
        assert values["GENESIS_LIVE_WRITES_REPORT"] == str(directory / "rows.tsv")
        assert values["GENESIS_LIVE_WRITES_RUN_ID"] == "101-2-" + "a" * 40
        assert list(directory.iterdir()) == []
        directories.append(directory)
    assert len(set(directories)) == 2


def test_actual_preparation_failure_stays_advisory_and_cannot_publish_a_path(tmp_path):
    result, env_file, output_file = _prepare(tmp_path, fail=True)
    assert result.returncode != 0
    assert b"FileNotFoundError" in result.stderr and os.fsencode(env_file) in result.stderr
    assert not output_file.exists()
    by_id, _ = _steps()
    assert by_id["census_prepare"]["continue-on-error"] is True
    assert "if" not in by_id["tests"]
    assert by_id["census_upload"]["continue-on-error"] is True
