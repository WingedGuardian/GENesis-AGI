"""Real SIGTERM and request CLI probes, using only disposable owned processes."""

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_scripts.test_codex_validator_serving import (
    SCRIPTS,
    TOKEN,
    VALUES,
    output,
    runtime,
    serving,
    status_script,
)


def _kill_group(pid):
    assert pid > 1 and pid != os.getpgrp()
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


@contextlib.contextmanager
def _process(argv):
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            _kill_group(proc.pid)
        proc.communicate(timeout=5)


def _wait_file(path, proc):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        assert proc.poll() is None, proc.communicate(timeout=5)
        time.sleep(0.005)
    pytest.fail(f"Owned fixture did not become ready: {path.name}")


def _not_live(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return True
    return state in {"Z", "X"}


def _sleeping_script(tmp_path):
    ready = tmp_path / "owned.pids"
    script = (
        "printf 'private child marker\\n' >&2\n/usr/bin/sleep 30 &\n"
        f'printf \'%s %s\\n\' "$$" "$!" > {shlex.quote(str(ready))}\nwait\n'
    )
    return script, ready


def _cleanup_recorded_groups(*paths):
    for path in paths:
        if path.exists() and path.read_text().strip():
            _kill_group(int(path.read_text().split()[0]))


def _custom_handler(signum, frame):
    pass


@pytest.mark.parametrize("prior", [signal.SIG_DFL, signal.SIG_IGN, _custom_handler])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_prior_sigterm_handler_is_restored(tmp_path, monkeypatch, prior, outcome):
    async def capture(*args):
        assert signal.getsignal(signal.SIGTERM) != prior
        if outcome == "cancel":
            raise asyncio.CancelledError
        if outcome == "error":
            raise ValueError("private marker")
        return output()

    monkeypatch.setattr(serving, "_capture", capture)
    original = signal.signal(signal.SIGTERM, prior)
    try:
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                serving.observe(tmp_path)
        else:
            assert serving.observe(tmp_path)["established"] is (outcome == "success")
        assert signal.getsignal(signal.SIGTERM) == prior
    finally:
        signal.signal(signal.SIGTERM, original)


def test_signal_setup_failure_refuses_before_capture(tmp_path, monkeypatch):
    class UnavailableLoop:
        def add_signal_handler(self, *args):
            raise RuntimeError("signals unavailable")

    async def forbidden(*args):
        pytest.fail("Failed signal setup entered child creation")

    monkeypatch.setattr(serving.asyncio, "get_running_loop", lambda: UnavailableLoop())
    monkeypatch.setattr(serving, "_capture", forbidden)
    prior = signal.getsignal(signal.SIGTERM)
    assert serving.observe(tmp_path) == {
        "established": False,
        "reason": "Serving identity unavailable",
    }
    assert signal.getsignal(signal.SIGTERM) == prior


OBSERVER = r"""
import asyncio, json, signal, sys
from pathlib import Path
cfg = json.loads(sys.argv[1])
sys.path[:0] = cfg["paths"]
import codex_validator_serving as serving
root = Path(cfg["scratch"])
original_spawn = asyncio.create_subprocess_exec
original_shield = asyncio.shield
original_reap = serving.reap_bounded
count = 0
def shield(future):
    global count
    count += 1
    (root / ("shield-" + str(count))).write_text("ready")
    return original_shield(future)
async def spawn(*args, **kwargs):
    proc = await original_spawn(*args, **kwargs)
    (root / "spawned.pid").write_text(str(proc.pid))
    if cfg["phase"] == "creation":
        while not (root / "release").exists():
            await asyncio.sleep(0.005)
    return proc
async def reap(proc):
    if cfg["phase"] == "collection" and cfg["repeated"]:
        (root / "reaping").write_text("ready")
        while not (root / "release").exists():
            await asyncio.sleep(0.005)
    await original_reap(proc)
asyncio.create_subprocess_exec = spawn
asyncio.shield = shield
serving.reap_bounded = reap
prior = signal.getsignal(signal.SIGTERM)
try:
    serving.observe(Path(cfg["runtime"]))
except asyncio.CancelledError:
    assert signal.getsignal(signal.SIGTERM) == prior
    print("cancelled", flush=True)
else:
    raise AssertionError("Interrupted observation returned normally")
"""


@pytest.mark.parametrize("phase", ["creation", "collection"])
@pytest.mark.parametrize("repeated", [False, True])
def test_real_sigterm_cleans_owned_group(tmp_path, phase, repeated):
    script, ready = _sleeping_script(tmp_path)
    root = runtime(tmp_path, script)
    spawned = tmp_path / "spawned.pid"
    cfg = {
        "paths": [str(SCRIPTS), str(SCRIPTS.parent / "src")],
        "scratch": str(tmp_path),
        "runtime": str(root),
        "phase": phase,
        "repeated": repeated,
    }
    try:
        with (
            _process(["/usr/bin/sleep", "30"]) as control,
            _process([sys.executable, "-I", "-c", OBSERVER, json.dumps(cfg)]) as parent,
        ):
            group, descendant = map(int, _wait_file(ready, parent).split())
            assert int(_wait_file(spawned, parent)) == group
            assert os.getpgid(descendant) == group != control.pid
            parent.send_signal(signal.SIGTERM)
            if phase == "creation":
                _wait_file(tmp_path / "shield-2", parent)
                if repeated:
                    parent.send_signal(signal.SIGTERM)
                    _wait_file(tmp_path / "shield-3", parent)
                (tmp_path / "release").touch()
            elif repeated:
                _wait_file(tmp_path / "reaping", parent)
                parent.send_signal(signal.SIGTERM)
            stdout, stderr = parent.communicate(timeout=8)
            assert parent.returncode == 0 and stdout == b"cancelled\n", stderr
            assert stderr == b"" and control.poll() is None
            assert _not_live(group) and _not_live(descendant)
    finally:
        _cleanup_recorded_groups(ready, spawned)


def _request_cli(tmp_path, script, operation):
    root = runtime(tmp_path, script)
    for name in ("codex_validator_request.py", "codex_validator_serving.py"):
        shutil.copyfile(SCRIPTS / name, root / "scripts" / name)
    # Only source dependencies are shared. Runtime paths and status program are
    # private fixtures; no Genesis server, database or model is initialized.
    (root / "src").symlink_to(SCRIPTS.parent / "src", target_is_directory=True)
    (root / "scripts/hooks").symlink_to(SCRIPTS / "hooks", target_is_directory=True)
    workspace = tmp_path / "private workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "requests").mkdir(mode=0o700)
    request = workspace / "requests/00000000-0000-0000-0000-000000000001.json"
    payload = {"version": 1, "operation": operation}
    if operation == "serving_verify":
        payload["token"] = TOKEN
    request.write_text(json.dumps(payload))
    request.chmod(0o600)
    argv = [
        sys.executable,
        "-I",
        str(root / "scripts/codex_validator_request.py"),
        "--workspace-root",
        str(workspace),
        "--request",
        str(request),
    ]
    return argv, request


@pytest.mark.parametrize("operation", ["serving_status", "serving_verify"])
def test_actual_request_cli_cancellation_is_private(tmp_path, operation):
    script, ready = _sleeping_script(tmp_path)
    argv, request = _request_cli(tmp_path, script, operation)
    original = request.read_bytes()
    try:
        with _process(["/usr/bin/sleep", "30"]) as control, _process(argv) as parent:
            group, descendant = map(int, _wait_file(ready, parent).split())
            parent.send_signal(signal.SIGTERM)
            stdout, stderr = parent.communicate(timeout=8)
            assert parent.returncode == 2
            assert stdout == b"" and stderr == b"Validator request refused\n"
            assert _not_live(group) and _not_live(descendant)
            assert control.poll() is None
            assert request.read_bytes() == original
    finally:
        _cleanup_recorded_groups(ready)


@pytest.mark.parametrize("operation", ["serving_status", "serving_verify"])
def test_actual_request_cli_uses_one_status_read(tmp_path, operation):
    log = tmp_path / "calls.log"
    argv, request = _request_cli(tmp_path, status_script(log=log), operation)
    original = request.read_bytes()
    result = subprocess.run(argv, capture_output=True, timeout=8)
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout) == {
        "version": 1,
        "operation": operation,
        **VALUES,
        "mainpid": 321,
        "established": True,
        **({"verified": True} if operation == "serving_verify" else {}),
    }
    assert log.read_text().splitlines() == [
        f"status --verify {TOKEN}" if operation == "serving_verify" else "status",
    ]
    assert request.read_bytes() == original


def test_request_cli_with_merged_status_producer(tmp_path, station):
    """Real request -> reader -> deploy status, with the existing station fence."""
    keys = (
        "HOME",
        "PATH",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "GENESIS_DEPLOY_ROOT",
        "GENESIS_DEPLOY_VENV",
        "GENESIS_DEPLOY_PROC_ROOT",
        "GENESIS_DEPLOY_PORT_PROBE",
        "GENESIS_ALERT_QUEUE_ROOT",
        "PYTHONPATH",
    )
    exports = "".join(f"export {key}={shlex.quote(station['env'][key])}\n" for key in keys)
    calls = tmp_path / "observations.log"
    script = (
        exports
        + f'printf "%s\\n" "$*" >> {shlex.quote(str(calls))}\n'
        + f'exec /bin/bash {shlex.quote(str(SCRIPTS / "deploy_code_only.sh"))} "$@"\n'
    )
    argv, request = _request_cli(tmp_path, script, "serving_status")

    def invoke(payload):
        request.write_text(json.dumps(payload))
        original = request.read_bytes()
        result = subprocess.run(argv, capture_output=True, timeout=12)
        assert result.returncode == 0 and result.stderr == b"", result.stderr
        assert request.read_bytes() == original
        return json.loads(result.stdout)

    start = invoke({"version": 1, "operation": "serving_status"})
    assert start["established"] and serving.TOKEN_RE.fullmatch(start["bracket"])
    assert start["mainpid"] == 1111 and start["invocation"] == "1" * 32
    verified = invoke({"version": 1, "operation": "serving_verify", "token": start["bracket"]})
    assert verified["established"] and verified["verified"]
    assert verified["bracket"] == start["bracket"]
    invalid = "b1-" + "0" * 24
    refused = invoke({"version": 1, "operation": "serving_verify", "token": invalid})
    assert refused == {
        "version": 1,
        "operation": "serving_verify",
        "established": False,
        "reason": "Serving identity unavailable",
        "bracket": invalid,
        "verified": False,
    }
    assert calls.read_text().splitlines() == [
        "status",
        f"status --verify {start['bracket']}",
        f"status --verify {invalid}",
    ]
    assert not any(
        "restart " in row or "stop " in row for row in station["calls"].read_text().splitlines()
    )
