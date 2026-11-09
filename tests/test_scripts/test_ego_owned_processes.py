"""Process-scope checks use only owned harmless Python children on the host."""

import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/ci"))
    import ego_owned_processes

    return ego_owned_processes


@pytest.mark.parametrize("name", ["plain", "spaces here", "a)b(c λ", ") ) 42 ("])
def test_proc_stat_comm_does_not_shift_identity_fields(module, name):
    text = f"123 ({name}) S 12 123 123 " + " ".join(["0"] * 15 + ["456"])
    assert module.parse_stat(text) == {"pid": 123, "state": "S", "ppid": 12, "pgid": 123, "sid": 123, "start": 456}


@pytest.mark.parametrize("text", ["", "123 name S", "0 (name) S 1 2 3", "123 (name) S 1"])
def test_incomplete_proc_identity_fails_closed(module, text):
    with pytest.raises(ValueError):
        module.parse_stat(text)


@pytest.mark.parametrize("defect", [None, "parent", "outer_group", "child_group", "detached", "receipt", "controller_group"])
def test_scope_population_is_bound_before_signalling(module, monkeypatch, defect):
    outer, child, native = os.getpid() + 10000, os.getpid() + 10001, os.getpid() + 10002
    rows = {outer: {"ppid": os.getpid(), "pgid": outer, "sid": outer},
            child: {"ppid": outer, "pgid": child, "sid": child},
            native: {"ppid": child, "pgid": child, "sid": child}}
    receipt = {"pid": child, "pgid": child}
    process = SimpleNamespace(pid=outer, poll=lambda: None)
    if defect == "parent":
        rows[outer]["ppid"] = 1
    elif defect == "outer_group":
        rows[outer]["pgid"] = child
    elif defect == "child_group":
        rows[child]["pgid"] = outer
    elif defect == "detached":
        rows[native]["pgid"] = native
    elif defect == "receipt":
        receipt["pid"] = True
    elif defect == "controller_group":
        monkeypatch.setattr(module.os, "getpgrp", lambda: outer)
    signals = []
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: signals.append(sig))
    if defect:
        with pytest.raises(module.OwnershipError):
            module.scopes(process, rows, receipt)
    else:
        assert module.scopes(process, rows, receipt) == {outer, child}
    assert all(s == 0 for s in signals)


def test_actual_harmless_direct_child_scope_is_closed_and_reaped(module):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"],
                               start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        groups = module.scopes(process, module.census())
        assert groups == {process.pid} and module.group_alive(process.pid)
        result = module.stop(process, groups)
        assert result["returncode"] == -signal.SIGKILL and result["timed_out"] is False
        assert not module.group_alive(process.pid) and process.pid not in module.census()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=60)


def test_wait_rejects_an_exited_owner_and_a_missing_marker(module):
    with pytest.raises(RuntimeError, match="exited"):
        module.wait_for(lambda: False, SimpleNamespace(poll=lambda: 2))
    with pytest.raises(RuntimeError, match="closure"):
        module.wait_for(lambda: False, seconds=0)


def test_owned_separate_group_writer_closes_before_pipe_drain(module, monkeypatch):
    """Real pipe EOF with two directly owned children; no host orphan needed."""
    outer = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"], start_new_session=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    child = None
    drain = outer.communicate
    try:
        with Path(f"/proc/{outer.pid}/fd/1").open("wb", buffering=0) as writer:
            child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                                     start_new_session=True, stdout=writer, stderr=subprocess.DEVNULL)
        alive = module.group_alive

        def reap_owned_child(pgid):
            if pgid == child.pid:
                child.poll()  # Emulate the external init reaping its child.
            return alive(pgid)

        monkeypatch.setattr(module, "group_alive", reap_owned_child)
        # The .25s bound is the measured negative EOF control, after all fixed
        # cleanup should be complete; it does not cap legitimate child work.
        monkeypatch.setattr(outer, "communicate", lambda *args, **kwargs: drain(timeout=0.25))
        result = module.stop(outer, {outer.pid, child.pid})
        assert result["returncode"] == -signal.SIGKILL and child.poll() is not None
    finally:
        for process in (outer, child):
            if process is not None:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=60)
        drain(timeout=60)


@pytest.mark.parametrize("defect", [None, "init_exe", "init_missing", "tail", "controller_exe", "controller_missing", "script", "parent"])
@pytest.mark.parametrize("script", ["/fixture/ego_owned_recovery.py", "/fixture/ego_owned_timing.py"])
def test_init_controller_identity_is_complete_and_consistent(module, monkeypatch, defect, script):
    controller = [sys.executable, script]
    init = ["/sbin/docker-init", "--", *controller]
    raw = {"/proc/1/cmdline": b"\0".join(v.encode() for v in init) + b"\0",
           "/proc/self/cmdline": b"\0".join(v.encode() for v in controller) + b"\0"}
    links = {"/proc/1/exe": Path("/sbin/docker-init"), "/proc/self/exe": Path(sys.executable).resolve()}
    if defect == "init_exe":
        links["/proc/1/exe"] = Path("/usr/local/bin/python3.12")
    elif defect == "init_missing":
        raw["/proc/1/cmdline"] = b""
    elif defect == "tail":
        raw["/proc/1/cmdline"] = b"/sbin/docker-init\0--\0foreign\0"
    elif defect == "controller_exe":
        links["/proc/self/exe"] = Path("/bin/sh")
    elif defect == "controller_missing":
        raw["/proc/self/cmdline"] = b""
    elif defect == "script":
        raw["/proc/self/cmdline"] = sys.executable.encode() + b"\0/fixture/foreign.py\0"
    monkeypatch.setattr(module.os, "getpid", lambda: 2)
    monkeypatch.setattr(module.os, "getppid", lambda: 3 if defect == "parent" else 1)
    monkeypatch.setattr(module.Path, "read_bytes", lambda path: raw[str(path)])
    monkeypatch.setattr(module.Path, "readlink", lambda path: links[str(path)])
    if defect:
        with pytest.raises((module.OwnershipError, ValueError)):
            module.init_identity(script)
    else:
        result = module.init_identity(script)
        assert result["init_argv"] == init and result["controller_argv"] == controller
        assert result["controller_executable"] == str(Path(sys.executable).resolve())
