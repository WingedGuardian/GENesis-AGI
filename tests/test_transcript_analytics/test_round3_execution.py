"""Ownership, installed-state and conditional-derive regression contracts."""

import contextlib
import fcntl
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from genesis.hostmetrics import run
from genesis.transcript_analytics import cli, config, derive, store


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_pending_signal_reaches_group_on_bind(monkeypatch, sig):
    sent, scopes = [], []
    monkeypatch.setattr(run, "kill_group", lambda pid, s: sent.append((pid, s)))
    monkeypatch.setattr(run, "_signal_scope", lambda unit, sig: scopes.append(unit) or True)
    monkeypatch.setattr(run, "_scope_quiescent", lambda unit: True)
    real_waitid = os.waitid
    monkeypatch.setattr(run.os, "waitid", lambda kind, pid, flags: object() if pid == 4242 else real_waitid(kind, pid, flags))
    monkeypatch.setattr(run.os, "waitpid", lambda pid, flags: (pid, 0))
    proc = Mock(pid=4242, returncode=None)
    with run.OwnedProcess() as owned:
        os.kill(os.getpid(), sig)
        owned.bind(proc, "genesis-job-example-1")
        owned.wait()
    assert sent == [(4242, signal.SIGTERM)]
    assert scopes == []  # A requested name without registration is not authority.


def test_reaped_group_never_receives_later_signal(monkeypatch):
    sent = []
    real_waitid = os.waitid
    monkeypatch.setattr(run.os, "waitid", lambda kind, pid, flags: object() if pid == 4242 else real_waitid(kind, pid, flags))
    monkeypatch.setattr(run.os, "waitpid", lambda pid, flags: (pid, 0))
    monkeypatch.setattr(run, "kill_group", lambda *args: sent.append(args))
    with run.OwnedProcess() as owned:
        owned.bind(Mock(pid=4242, returncode=0))
        owned.wait()
        owned.stop()
    assert sent == []


def test_handler_restored_after_spawn_failure():
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(FileNotFoundError), run.OwnedProcess():
        raise FileNotFoundError("synthetic failed spawn")
    assert signal.getsignal(signal.SIGTERM) == previous


def test_if_stale_checks_under_writer_lock(tmp_path, monkeypatch):
    lock = tmp_path / "writer.lock"
    from genesis.transcript_analytics import query

    def current(data, selected):
        # The predicate must only be reached after exclusive writer admission.
        with lock.open("a") as probe, pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True

    monkeypatch.setattr(query, "derived_current", current)
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(store.Busy):
            derive.build(tmp_path / "data", lock_path=lock, if_stale=True)
    assert derive.build(tmp_path / "data", lock_path=lock, if_stale=True) == {
        "derived": "current; nothing to do"
    }


def test_private_opt_in_cannot_resurrect_missing_install(tmp_path, monkeypatch):
    from genesis import _config_overlay

    base = tmp_path / "transcript_analytics.yaml"
    monkeypatch.setattr(config, "_base_path", lambda: base)
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: tmp_path)
    base.with_suffix(".local.yaml").write_text("enabled: true\n")
    with pytest.raises(config.NotInstalled):
        config.load()
    assert cli.main(["status"]) == 0
    assert cli.main(["ingest"]) == 69


def test_display_values_and_column_names_escape_controls(monkeypatch, capsys):
    from genesis.transcript_analytics import query

    monkeypatch.setattr(query, "run_query", lambda *a, **k: (["col\x1b[2J"], [("x\r\x1b[31m\u202e",)]))
    args = Mock(csv=False, max_rows=10, cell_width=120)
    assert cli.cmd_sql(args) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output and "\r" not in output and "\u202e" not in output
    assert r"\u001b" in output and r"\r" in output and r"\u202e" in output


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_launch_signal_reaps_real_worker_group(tmp_path, sig):
    """Exercise the actual analytics launcher and supervisor, without systemd."""
    worker = (
        "import os,time; fd=int(os.environ['GENESIS_TRANSCRIPT_RESOURCE_READY_FD']); "
        "os.write(fd,b'1'); os.close(fd); print(os.getpid(),flush=True); time.sleep(60)"
    )
    script = f'''
import sys
from genesis.hostmetrics import run
from genesis.transcript_analytics import resources
run.scope_argv = lambda *a: ['/usr/bin/env', '--', sys.executable, '-c', {worker!r}]
run._signal_scope = lambda *a: True
run._scope_quiescent = lambda *a: True
with open({str(tmp_path / 'lease')!r}, 'a') as lease:
    raise SystemExit(resources._launch([], 1024, 25, lease))
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
    child = None
    try:
        import select

        assert select.select([proc.stdout], [], [], 15)[0], "worker never started"
        child = int(proc.stdout.readline())
        proc.send_signal(sig)
        _, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 128 + sig, stderr
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if child is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child, signal.SIGKILL)


@pytest.mark.parametrize("key", ["projects_dir", "data_dir"])
@pytest.mark.parametrize("value", [None, False, 1, [], {}, "", "bad\0path"])
def test_persistent_paths_reject_invalid_types(key, value):
    from genesis.transcript_analytics.config import from_values

    with pytest.raises(ValueError):
        from_values({key: value})
