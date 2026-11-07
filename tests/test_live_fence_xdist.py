"""The live fence under pytest-xdist.

Each test here runs a real `pytest -n 2` in a subprocess, over a scratch project
whose conftest arms the same fence. Two things broke before the fence knew about
xdist:

- Workers inherited the controller's already-fenced environment and took it for
  the original. A `real_tool` test still resolved the controller's stub, and a
  `user_manager` test got the no-bus override.
- A worker's exit status and terminal summary never reach the run's result, so a
  refusal outside its test (a detached child) left the run green.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import _live_fence as fence

REPO = Path(__file__).resolve().parents[1]

_CONFTEST = f"""
import sys
sys.path.insert(0, {str(REPO)!r})
pytest_plugins = ["tests._live_fence"]


def pytest_configure(config):
    from tests import _live_fence
    _live_fence.install(config)
"""


def _run(
    tmp_path: Path,
    files: dict[str, str],
    *,
    env: dict[str, str] | None = None,
    conftest_tail: str = "",
):
    pytest.importorskip("xdist")
    project = tmp_path / "project"
    project.mkdir()
    (project / "conftest.py").write_text(_CONFTEST + conftest_tail)
    for name, body in files.items():
        (project / name).write_text(textwrap.dedent(body))
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("GENESIS_LIVE_FENCE")}
    # The outer fence's own PATH entry and bus override are this test's
    # environment, not the scratch run's: hand the child the outer originals.
    child_env["PATH"] = fence._S.orig_path
    for key, value in fence._S.saved_bus.items():
        if value is None:
            child_env.pop(key, None)
        else:
            child_env[key] = value
    child_env.update(env or {})
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "2",
            "--dist=loadfile",
            "-p",
            "no:cacheprovider",
            "-q",
            "-rA",
            str(project),
        ],
        cwd=project,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_real_tool_test_in_a_worker_resolves_the_installed_tool(tmp_path):
    if not fence._resolve("systemctl", {"PATH": fence._S.orig_path}):
        pytest.skip("systemctl is not installed here, so there is no stub to escape")
    result = _run(
        tmp_path,
        {
            "test_real.py": """
                import shutil, pytest

                @pytest.mark.real_tool("systemctl")
                def test_real():
                    found = shutil.which("systemctl")
                    assert found and "genesis-live-fence-" not in found, found

                def test_fenced():
                    assert "genesis-live-fence-" in (shutil.which("systemctl") or "")
            """,
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_user_manager_test_in_a_worker_gets_the_original_bus(tmp_path):
    result = _run(
        tmp_path,
        {
            "test_bus.py": """
                import os, pytest

                @pytest.mark.user_manager
                def test_opted_out():
                    assert os.environ.get("XDG_RUNTIME_DIR") == "/run/user/fence-probe"

                def test_fenced():
                    assert "no-bus" in os.environ.get("XDG_RUNTIME_DIR", "")
            """,
        },
        env={"XDG_RUNTIME_DIR": "/run/user/fence-probe"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


_LATE = """
    import os, subprocess, time
    from pathlib import Path

    MARK = Path(os.environ["LATE_MARK"])

    def test_leaves_a_child():
        # `claude` is always stubbed here (GENESIS_CC_BIN is set), and a bare
        # `claude -p` is refused and logged on any machine. The marker is touched
        # after the stub has written its log line.
        subprocess.Popen(["sh", "-c", f"sleep 1; claude -p x; touch {MARK}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_outlives_it():
        deadline = time.monotonic() + 60
        while not MARK.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert MARK.exists()
"""


def _late_env(tmp_path: Path) -> dict[str, str]:
    return {"GENESIS_CC_BIN": "/nonexistent/claude", "LATE_MARK": str(tmp_path / "late.mark")}


def test_a_refusal_outside_its_test_in_a_worker_fails_the_run(tmp_path):
    # The first test leaves a child behind that runs a refused command after the
    # test has ended; the second waits until it has, so the refusal is drained
    # there and counts as "outside its test". The same file keeps both on one
    # worker.
    result = _run(tmp_path, {"test_late.py": _LATE}, env=_late_env(tmp_path))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "live fence: refusals outside their test" in result.stdout


def test_a_worker_that_dies_after_its_last_test_fails_the_run(tmp_path):
    # It never reports its findings, so they are unknown: the run must not pass.
    result = _run(
        tmp_path,
        {"test_fine.py": "def test_fine():\n    pass\n"},
        conftest_tail=(
            "\n\ndef pytest_sessionfinish(session):\n"
            "    if hasattr(session.config, 'workerinput'):\n"
            "        import os\n"
            "        os._exit(1)\n"
        ),
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "went down without reporting its live-fence findings" in result.stdout


def _config(*, worker: bool):
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=None))
    if worker:
        config.workerinput = {}
    return config


def test_a_worker_adopts_originals_only_from_the_controller_that_started_it(monkeypatch):
    mine = {"run": os.getppid(), "path": "/orig/bin", "bus": {}, "cc_bin": None}
    monkeypatch.setenv(fence._ORIG_ENV, json.dumps(mine))
    assert fence._originals(_config(worker=True))["path"] == "/orig/bin"
    # A nested run's workers have a different parent: they read their own env.
    monkeypatch.setenv(fence._ORIG_ENV, json.dumps({**mine, "run": os.getppid() + 1}))
    assert fence._originals(_config(worker=True))["path"] == os.environ["PATH"]
    # A serial process never adopts, even with the variable inherited.
    monkeypatch.setenv(fence._ORIG_ENV, json.dumps(mine))
    assert fence._originals(_config(worker=False))["path"] == os.environ["PATH"]
