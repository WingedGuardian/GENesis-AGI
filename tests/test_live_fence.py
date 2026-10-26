"""Tests for the live fence (tests/_live_fence.py).

Every probe here is harmless even if the fence were absent: the refused call
is ``sudo -n true``, the answered one ``systemctl --user is-active <a unit
that does not exist>``, and every deploy script run is either a stub under
tmp_path or the checkout's own ``scripts/update.sh``, which refuses itself under
the fence before it does anything (checked before each run, see
``_require_self_guarded``). The fence's own tests must never be the thing that
reaches live state (#2863: a fence's verify-RED once stopped a live server).
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
UPDATE_SH = Path(__file__).resolve().parent.parent / "scripts" / "update.sh"


def _require_self_guarded(script: Path) -> None:
    """The replay tests run the REAL update.sh, which is safe only because it
    sources the live-system guard and refuses itself before its first real step
    (the copy-to-temp block). Check that order before spawning it, so a change
    that moves or drops the guard fails here instead of running the script."""
    text = script.read_text(encoding="utf-8")
    source = text.find("lib/live_system_guard.sh")
    call = text.find("refuse_if_test_fenced_live")
    copy = text.find("# ── Copy-to-temp guard")
    assert 0 <= source < call < copy, (
        f"{script} must source and call the test fence before its copy-to-temp block"
    )


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


def test_os_system_hits_the_shim():
    """os.system runs `sh -c <line>`: the shell itself is allowed, and the
    deny-listed name inside the line hits the PATH shim (exit 3, logged)."""
    rc = os.system(" ".join(PROBE))  # noqa: S605 - the shape under test
    lines = fence._drain()  # consume, so this test's teardown does not fail it
    assert rc != 0
    assert any("\tshim\tsudo" in ln for ln in lines)


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


def test_the_fence_arms_the_deploy_scripts_own_guard():
    """The deploy refusal moved into the scripts: the fence only exports the
    two variables scripts/lib/live_system_guard.sh reads."""
    repo_root = os.path.realpath(Path(__file__).resolve().parent.parent)
    roots = os.environ["GENESIS_TEST_FENCE_LIVE_ROOTS"].split(":")
    assert repo_root in roots
    if fence._S.real_home:
        # Listed whether or not they exist yet: a test that creates one
        # mid-run must not open a gap (CI: a test created ~/.genesis).
        for name in (".genesis", "genesis"):
            assert os.path.realpath(os.path.join(fence._S.real_home, name)) in roots
    assert os.environ["GENESIS_TEST_FENCE_LOG"] == fence._S.log


def test_a_guarded_script_refuses_itself_and_the_log_line_fails(tmp_path):
    """A script wired like a deploy script (source the guard, check its acting
    root) run against a listed root exits 97 and its REFUSED line lands in the
    fence's log — the line teardown would fail an unwrapped test for."""
    lib = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "live_system_guard.sh"
    script = tmp_path / "deploy.sh"
    script.write_text(
        f'#!/bin/bash\n. "{lib}"\nrefuse_if_test_fenced_live "$1" || exit 97\n'
    )
    script.chmod(0o755)
    repo_root = os.path.realpath(Path(__file__).resolve().parent.parent)
    with fence.expect_violation() as v:
        run = subprocess.run(["bash", str(script), repo_root], capture_output=True, text=True)
    assert run.returncode == 97
    assert "REFUSED (test fence):" in run.stderr
    assert any("REFUSED (test fence):" in r for r in v.reasons)


def test_an_unguarded_script_under_tmp_runs(tmp_path):
    """A script without the guard runs wherever it is launched from: refusing
    is the script's own decision now, not the fence's."""
    script = tmp_path / "checkout" / "scripts" / "update.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    assert subprocess.run(["bash", "-e", str(script)], env=env).returncode == 0


@pytest.mark.parametrize(
    ("program", "argv", "refused"),
    [
        ("/opt/somewhere/gh", ["gh", "pr", "list"], False),  # no such file: fails as on CI
        ("/usr/bin/env", ["env"], False),
        ("git", ["git", "status"], False),
        ("bash", ["bash", "-c", "true update.sh"], False),
        # Only the resolved PROGRAM is judged, never argv: a deploy script
        # named here is refused by the script itself, not by the fence.
        ("bash", ["bash", "-e", "/x/scripts/update.sh"], False),
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
    # What the real systemd-run goes on to start is a stated residual of the
    # opt-out: arguments are never judged.
    run = ["systemd-run", "--user", "--scope", "-p", "MemoryMax=1G", "bash", "/x/scripts/update.sh"]
    assert fence.judge("systemd-run", run, stubs) is None


def _wait_for_refusal(after: int) -> None:
    """The update runs `bash update.sh` in a detached child: poll the log for a
    REFUSED appended after offset ``after`` — earlier tests' consumed REFUSED
    lines stay in the file, so the whole file cannot be searched."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if b"REFUSED (test fence):" in Path(fence._S.log).read_bytes()[after:]:
                return
        except OSError:
            pass
        time.sleep(0.1)


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
    """The fallback runs `bash <checkout>/scripts/update.sh`: allowed in-process
    (bash resolves fine), then the SCRIPT refuses itself under the exported
    roots — exit 97, REFUSED logged — which this test consumes. Here the real
    checkout's update.sh stands in for the deployed one: both are listed roots."""
    monkeypatch.setattr(update_routes, "_systemd_run_scope_available", lambda env: False)
    _require_self_guarded(UPDATE_SH)
    monkeypatch.setattr(update_routes, "_UPDATE_SCRIPT", UPDATE_SH)
    with fence.expect_violation() as v:
        mark = os.path.getsize(fence._S.log)
        update_routes._apply_direct(Path("/nonexistent/pid"))
        _wait_for_refusal(mark)
    assert any("REFUSED (test fence):" in r and "update.sh" in r for r in v.reasons), v.reasons


def test_replay_direct_probe_is_answered_and_the_fallback_refused(update_routes, spawned, monkeypatch):
    """The scope probe runs systemd-run: the stub answers "unavailable" (as on CI),
    which steers into the `bash update.sh` fallback. The script then refuses
    itself: a fence that answers a probe must still stop the route it steers
    into."""
    _require_self_guarded(UPDATE_SH)
    monkeypatch.setattr(update_routes, "_UPDATE_SCRIPT", UPDATE_SH)
    with fence.expect_violation() as v:
        mark = os.path.getsize(fence._S.log)
        update_routes._apply_direct(Path("/nonexistent/pid"))
        _wait_for_refusal(mark)
    assert [a[0] for a in spawned] == ["systemd-run", "bash"]
    assert any("REFUSED (test fence):" in r and "update.sh" in r for r in v.reasons), v.reasons
