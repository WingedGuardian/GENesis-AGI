"""A test that hangs under pytest-xdist must still leave its stack in the log.

pytest-timeout's thread method writes the stacks through the terminal writer and
then ends the process. A pytest-xdist worker's stdout goes to /dev/null, so a
parallel run showed only "worker 'gw0' crashed while running ..." with no stack.
pyproject's `faulthandler_timeout` dumps every thread to stderr shortly before the
per-test timeout, and a worker's stderr is kept.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_the_stack_dump_comes_before_the_per_test_timeout():
    options = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert 0 < options["faulthandler_timeout"] < options["timeout"]
    assert options["timeout_method"] == "thread"


def test_a_hung_test_in_a_worker_leaves_its_stack_on_stderr(tmp_path):
    pytest.importorskip("xdist")
    pytest.importorskip("pytest_timeout")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_hang.py").write_text(
        "import time\n\ndef test_hang():\n    time.sleep(3600)\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "2",
            "--max-worker-restart=0",
            "-p",
            "no:cacheprovider",
            "-o",
            "timeout=8",
            "-o",
            "timeout_method=thread",
            "-o",
            "faulthandler_timeout=3",
            str(tmp_path / "test_hang.py"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "crashed while running" in result.stdout
    # The dump names the hung line, in the worker's stderr.
    assert "test_hang.py" in result.stderr and "line 4" in result.stderr, result.stderr[-2000:]
