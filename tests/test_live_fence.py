"""Tests for the live fence (tests/_live_fence.py).

Every probe here is harmless even if the fence were absent: the refused call
is ``sudo -n true``, the answered one ``systemctl --user is-active <a unit
that does not exist>``, and every deploy script run is a stub under tmp_path. The fence's own tests must never be
the thing that reaches live state (#2863: a fence's verify-RED once stopped a
live server).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import _live_fence as fence

PROBE = ["sudo", "-n", "true"]
READ = ["systemctl", "--user", "is-active", "genesis-fence-probe-nonexistent"]


@pytest.fixture(autouse=True)
def _every_stub(monkeypatch):
    """Every deny-listed name gets a stub here, installed on this machine or not,
    so these tests read the same on a CI runner without `claude`."""
    parts = os.environ["PATH"].split(os.pathsep)
    parts = [fence._S.all_stub_dir if p == fence._S.shim_dir else p for p in parts]
    monkeypatch.setenv("PATH", os.pathsep.join(parts))
    monkeypatch.setenv("GENESIS_CC_BIN", os.path.join(fence._S.all_stub_dir, "claude"))


@pytest.fixture()
def spawned(monkeypatch):
    """Counts processes that actually started through subprocess.Popen."""
    started = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            started.append(self.args)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    return started


def test_the_fence_is_armed():
    assert fence._S.installed
    assert os.environ["PATH"].split(os.pathsep)[0] == fence._S.all_stub_dir  # _every_stub


def test_a_stub_exists_only_for_an_installed_tool():
    """Where a tool is not installed, `shutil.which` must keep saying so: a skip
    guarded on it would otherwise run against the refusing stub."""
    for name in fence.DENY_NAMES:
        installed = shutil.which(name, path=fence._S.orig_path) is not None
        assert (name in fence._S.stubbed) == installed or name == "claude", name
        assert os.path.exists(os.path.join(fence._S.shim_dir, name)) == (name in fence._S.stubbed)


def test_a_refusal_in_a_forked_child_exits_the_child(tmp_path):
    """os.spawn*/fork-exec: the exec is refused inside the child, which must exit
    (127) rather than unwind into a second copy of the pytest session."""
    pid = os.fork()
    if pid == 0:
        with contextlib.suppress(Exception):  # the shape under test
            os.execvp(PROBE[0], PROBE)  # noqa: S606 - refused before it runs
        os._exit(99)  # only if the fence let the exec fail some other way
    _, status = os.waitpid(pid, 0)
    lines = fence._drain()  # consume, so this test's teardown does not fail it
    assert os.waitstatus_to_exitcode(status) == 127
    assert any("\taudit\tsudo" in ln for ln in lines), lines


def test_a_deny_listed_program_never_starts(spawned):
    with fence.expect_violation() as v:
        subprocess.run(PROBE, capture_output=True)
    assert spawned == []
    assert "sudo" in v.reasons[0]


def test_an_except_exception_fallback_cannot_catch_the_refusal(spawned):
    """A caller that falls back on any Exception must not get to try a second route."""
    fallback_ran = []

    def caller():
        try:
            subprocess.run(PROBE, capture_output=True)
        except Exception:  # noqa: BLE001 - the shape under test
            fallback_ran.append(True)

    with fence.expect_violation():
        caller()
    assert fallback_ran == []
    assert spawned == []


def test_asyncio_and_threads_are_refused_too():
    async def via_asyncio():
        await asyncio.create_subprocess_exec(*PROBE)

    with fence.expect_violation():
        asyncio.run(via_asyncio())

    async def via_thread():
        await asyncio.to_thread(subprocess.run, PROBE, capture_output=True)

    with fence.expect_violation():
        asyncio.run(via_thread())


def test_os_system_is_refused():
    with fence.expect_violation():
        os.system(" ".join(PROBE))  # noqa: S605 - the shape under test


def test_a_fake_in_tmp_path_runs(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho fake-claude\n")
    fake.chmod(0o755)
    out = subprocess.run([str(fake)], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "fake-claude"
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    out = subprocess.run(["claude"], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == "fake-claude"


def test_a_child_reaching_a_refused_name_hits_the_shim(tmp_path):
    """A script file under test calls a bare name (the in-process check cannot see
    inside the file): the shim refuses (exit 3) and logs."""
    script = tmp_path / "child.sh"
    script.write_text("sudo -n true\n")
    rc = subprocess.run(["sh", str(script)], capture_output=True).returncode
    lines = fence._drain()  # consume, so this test's teardown does not fail it
    assert rc == 3
    assert any("\tshim\tsudo" in ln for ln in lines)


def test_a_live_state_read_is_answered_like_ci_and_reported(spawned):
    """systemctl/systemd-run/gh/loginctl by bare name: the stub answers as the real
    tool does on a CI runner (no bus: exit 1; gh unauthenticated: exit 4), the test
    is not failed, and the call is reported (the sub-run test below checks that)."""
    read = subprocess.run(READ, capture_output=True, text=True)
    assert read.returncode == 1 and "Failed to connect to bus" in read.stderr
    assert subprocess.run(["sh", "-c", "gh repo view"], capture_output=True).returncode == 4
    lines = fence._drain()
    assert any("\tanswered\tsystemctl" in ln for ln in lines)
    assert any("\tanswered\tgh" in ln for ln in lines)
    fence._pending.extend(lines)  # let teardown classify them: answered, not failed
    assert len(spawned) == 2


def test_a_real_path_to_an_answered_tool_is_refused(tmp_path, monkeypatch):
    """Only the stub answers; a real binary by path, outside tmp, is refused."""
    fake = tmp_path / "systemctl"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(fence._S, "allowed_roots", ())  # treat tmp as an install dir
    assert fence.judge(str(fake), [str(fake)], None) is not None


def _stub_scripts(tmp_path: Path) -> Path:
    scripts = tmp_path / "checkout" / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "update.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return script


def test_a_deploy_script_with_the_real_home_is_refused(tmp_path):
    script = _stub_scripts(tmp_path)
    with fence.expect_violation():
        subprocess.run(["bash", str(script)], env={**os.environ, "HOME": fence._S.real_home})
    with fence.expect_violation():
        subprocess.run([str(script)])  # inherits the real HOME


def test_a_deploy_script_with_a_private_home_runs(tmp_path):
    script = _stub_scripts(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    assert subprocess.run(["bash", "-e", str(script)], env=env).returncode == 0


@pytest.mark.parametrize(
    ("program", "argv", "refused"),
    [
        ("/opt/somewhere/gh", ["gh", "pr", "list"], False),  # no such file: fails as on CI
        ("/usr/bin/env", ["env"], False),
        ("bash", ["bash", "-c", "true update.sh"], False),  # an argument, not a command
        ("git", ["git", "status"], False),
        ("bash", ["bash", "-n", "/x/scripts/update.sh"], False),  # parse only
        ("bash", ["bash", "-en", "/x/scripts/update.sh"], False),
        ("bash", ["bash", "-e", "/x/scripts/update.sh"], True),
    ],
)
def test_judge(program, argv, refused):
    assert (fence.judge(program, argv, None) is not None) is refused


@pytest.mark.user_manager
def test_a_user_manager_test_may_run_systemd_run_but_nothing_else(tmp_path, monkeypatch):
    assert fence._S.opted_out
    # systemd-run resolves (if installed) to the real binary, not a stub.
    assert fence.judge("systemd-run", ["systemd-run"], None) is None
    stubs = {"PATH": fence._S.all_stub_dir, "HOME": fence._S.real_home}
    assert fence.judge("claude", ["claude"], stubs) is not None
    # What the real systemd-run would start is judged, under the user
    # manager's PATH: a deploy run with the real HOME stays refused.
    run = ["systemd-run", "--user", "--scope", "-p", "MemoryMax=1G", "bash", "/x/scripts/update.sh"]
    assert fence.judge("systemd-run", run, stubs) is not None
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(fence._S, "allowed_roots", ())  # treat tmp as an install dir
    run = ["systemd-run", "--user", "-u", "x", "--", str(fake), "-p", "hi"]
    assert "payload" in (fence.judge("systemd-run", run, stubs) or "")
    assert fence.judge("systemd-run", ["systemd-run", "--user", "--", "true"], stubs) is None


# ── acceptance: the 2026-10-12 shape, every spawn branch ────────────────────
#
# A copy of the hazardous test's situation: the dashboard update routes with
# only their state files redirected, and nothing else stubbed. Each branch must
# start no deny-listed program.


@pytest.fixture()
def update_routes(tmp_path, monkeypatch):
    from flask import Flask

    from genesis.dashboard.routes import updates

    for name in ("_STATE_FILE", "_SUMMARY_FILE", "_ESCALATION_FILE", "_CONFLICT_FILE"):
        monkeypatch.setattr(updates, name, tmp_path / name)
    # Keep the routes' own log files off the real home; the fence is about
    # processes, and these writes are not what this test is measuring.
    monkeypatch.setattr(updates, "_GENESIS_DIR", tmp_path / "gd")
    monkeypatch.setattr(updates, "_HOME", tmp_path)
    # The spawns run with cwd= the checkout root: a temp root, never the deployed
    # checkout (which a CI runner does not have at all).
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(updates, "_GENESIS_ROOT", root)
    app = Flask(__name__)
    with app.app_context():
        yield updates


def test_replay_supervised_branch_reaches_only_the_stub(
    update_routes, spawned, monkeypatch, tmp_path
):
    """systemd-run by bare name gets the stub, which starts nothing: no orchestrator."""
    monkeypatch.setenv("HOME", str(tmp_path))  # mkstemp lands in HOME/tmp
    (tmp_path / "tmp").mkdir()
    update_routes._apply_supervised(tmp_path / "pid")
    assert [a[0] for a in spawned] == ["systemd-run"]
    proc_pid = int((tmp_path / "pid").read_text())
    with contextlib.suppress(ChildProcessError):
        os.waitpid(proc_pid, 0)
    lines = fence._drain()
    # Only the systemd-run stub ran: no orchestrator, so no `claude` either.
    assert {ln.split("\t")[2].split(" ")[0] for ln in lines} == {"systemd-run"}, lines
    fence._pending.extend(lines)


def test_replay_supervised_fallback_never_reaches_claude(update_routes, monkeypatch, tmp_path):
    """systemd-run missing → the orchestrator starts as a plain python process.
    That process is allowed; the `claude` it starts is not (PATH shim)."""
    root = tmp_path / "root"
    (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (root / ".venv" / "bin" / "python").symlink_to(sys.executable)
    monkeypatch.setattr(update_routes, "_GENESIS_ROOT", root)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "tmp").mkdir()
    real_popen = subprocess.Popen

    def popen(args, *a, **k):
        if args and args[0] == "systemd-run":
            raise FileNotFoundError("systemd-run")
        return real_popen(args, *a, **k)

    monkeypatch.setattr(update_routes.subprocess, "Popen", popen)
    body = update_routes._apply_supervised(tmp_path / "pid")
    pid = body.get_json()["orchestrator_pid"]
    assert pid > 1
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        time.sleep(0.1)
    lines = fence._drain()
    assert any("\tshim\tclaude" in ln for ln in lines), lines
    assert "tier1_cc_error" in (tmp_path / "_SUMMARY_FILE").read_text()


def test_replay_direct_branch_is_refused(update_routes, spawned, monkeypatch):
    monkeypatch.setattr(update_routes, "_systemd_run_scope_available", lambda env: False)
    with fence.expect_violation() as v:
        update_routes._apply_direct(Path("/nonexistent/pid"))
    assert spawned == []
    assert "update.sh" in v.reasons[0]


def test_replay_direct_probe_is_answered_and_the_fallback_refused(update_routes, spawned):
    """The scope probe runs systemd-run: the stub answers "unavailable" (as on CI),
    which steers into the `bash update.sh` fallback. That fallback, with the real
    HOME, is refused: a fence that answers a probe must still stop the route it
    steers into."""
    with fence.expect_violation() as v:
        update_routes._apply_direct(Path("/nonexistent/pid"))
    assert [a[0] for a in spawned] == ["systemd-run"]
    assert any("update.sh" in r for r in v.reasons)
