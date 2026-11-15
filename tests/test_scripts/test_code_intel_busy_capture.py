"""Real captured observer, canonical alert schema/drainer, harmless scratch state."""

from __future__ import annotations

import asyncio
import json
import shlex
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

import pytest

from genesis.env import alert_queue_root
from genesis.guardian.alert.queue import drain, list_queued
from tests.test_scripts.test_code_intel_runner import _inventory, _run_runner, _seed_marker
from tests.test_scripts.test_code_intel_source import ROOT, SELF, case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def prepared(tmp_path, monkeypatch, helper=None, source_hook=""):
    root, module, _ = case(tmp_path, monkeypatch, "runner")
    # Fix only the observed-clock seam in the disposable committed source. The
    # coordinator's real monotonic deadline remains untouched.
    source = root / "scripts/lib/code_intel_busy.py"
    source.write_text(source.read_text() + "\ntime.monotonic_ns = lambda: 3 * 3600 * 10**9\n")
    if helper is not None:
        (root / "scripts/lib/alert_queue.sh").write_text(helper)
    entry = root / SELF
    entry.write_text(entry.read_text() + source_hook)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qam",
            "fixed observation clock",
        ],
        check=True,
        capture_output=True,
    )
    raw = entry.read_bytes()
    module.__dict__["_ENTRY_BYTES"] = raw
    captured = module.capture(root, "runner", raw)
    base = tmp_path / "state"
    base.mkdir()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    (base / "code-intel-runner-busy.json").write_text(
        json.dumps(
            dict(
                version=1,
                active=dict(boot_id=boot, first_busy_ns=0, last_busy_ns=0, due_registered=False),
                pending={},
            )
        )
    )
    return root, module, captured, base


def invoke(captured, base, *arguments, held=True):
    lock = base / "locks/code-intel-runner.lock"
    lock.parent.mkdir(exist_ok=True)
    setup = "exec 8>" + shlex.quote(str(lock)) + "; flock -n 8 || exit 97;" if held else ""
    with captured.sealed() as fd:
        return subprocess.run(
            [
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-p",
                "-c",
                setup
                + "exec "
                + shlex.join(captured.resume_command("tick-observation", *arguments)),
            ],
            stdin=fd,
            capture_output=True,
            text=True,
            timeout=10,
        )


def test_sealed_observer_uses_captured_helper_and_real_consumer_root(tmp_path, monkeypatch):
    root, _, captured, base = prepared(tmp_path, monkeypatch)
    wrong = tmp_path / "unconsumed-override"
    monkeypatch.setenv("GENESIS_ALERT_QUEUE_ROOT", str(wrong))
    # Actual replay must not reopen either source, even after source drift.
    (root / "scripts/lib/code_intel_busy.py").unlink()
    (root / "scripts/lib/alert_queue.sh").write_text("exit 99\n")
    result = invoke(captured, base, "busy", "readiness")
    assert result.returncode == 0, result.stderr
    destination = alert_queue_root()
    assert destination == base / "alerts/queue" and not wrong.exists()
    entries = list_queued(destination)
    assert len(entries) == 1 and entries[0][1]["schema"] == 1
    entry = entries[0][1]
    assert entry["severity"] == "warning" and entry["source"] == "code_intel"
    assert "Enqueue-attempt snapshot time:" in entry["body"]
    assert "Original holders were not retained" in entry["body"]
    record = json.loads((base / "code-intel-runner-busy.json").read_text())
    assert record["active"]["due_registered"] is True and record["pending"] == {}
    seen = []

    async def transient(entry):
        seen.append(entry["dedupe_key"])
        return False

    async def terminal(entry):
        seen.append(entry["dedupe_key"])
        return True

    assert asyncio.run(drain(destination, transient)) == 0
    assert len(list_queued(destination)) == 1
    assert asyncio.run(drain(destination, terminal)) == 1
    assert seen == [entry["dedupe_key"], entry["dedupe_key"]]
    assert list_queued(destination) == []


@pytest.mark.parametrize(
    "arguments,held",
    [
        (("busy", "readiness"), False),
        (("unknown", "readiness"), True),
        (("busy", "injected phase"), True),
        (("busy",), True),
    ],
)
def test_observation_requires_canonical_lock_and_closed_arguments(
    tmp_path, monkeypatch, arguments, held
):
    _, _, captured, base = prepared(tmp_path, monkeypatch)
    path = base / "code-intel-runner-busy.json"
    before = path.read_bytes()
    result = invoke(captured, base, *arguments, held=held)
    assert result.returncode != 0
    assert path.read_bytes() == before and not (base / "alerts/queue").exists()


def test_observation_roles_reject_private_runtime_and_entry_profile(tmp_path, monkeypatch):
    _, _, captured, _ = prepared(tmp_path, monkeypatch)
    executable = captured.with_runtime(
        {
            key: {}
            for key in (
                "context",
                "reservation",
                "policy",
                "provider_options",
                "environment_recipe",
            )
        }
    )
    for role in ("tick-observation",):
        with pytest.raises(ValueError, match="source-only"):
            executable.resume_command(role, "busy", "readiness")
    with pytest.raises(ValueError, match="unsupported"):
        captured.resume_command("tick-observation-worker", "busy", "readiness")
    root = tmp_path / "entry-case"
    root.mkdir()
    _, _, entry = case(root, monkeypatch, "entry")
    with pytest.raises(ValueError, match="source-only"):
        entry.resume_command("tick-observation", "busy", "readiness")


def test_real_exit_bounds_blocked_notifier_keeps_busy_status_and_replays_debt(
    tmp_path, monkeypatch
):
    marker = tmp_path / "notifier.json"
    code = (
        "import json,os,pathlib,signal,stat,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps(dict(pid=os.getpid(),"
        'fd8=os.path.exists("/proc/self/fd/8"),stdin_character=stat.S_ISCHR(os.fstat(0).st_mode)))); '
        "time.sleep(30)"
    )
    helper = "queue_alert_try() { /usr/bin/python3 -I -S -c " + shlex.quote(code) + "; }\n"
    root, module, captured, base = prepared(tmp_path, monkeypatch, helper)
    # The raw compatibility runner gets its own HOME/GENESIS_HOME from the
    # existing runner fixture. Put its seeded health clock in that actual root.
    active_base = tmp_path / ".genesis"
    _seed_marker(tmp_path)
    clock = active_base / "code-intel-runner-busy.json"
    clock.write_bytes((base / "code-intel-runner-busy.json").read_bytes())
    before = _inventory(tmp_path)
    started = time.monotonic()
    with closing(
        sqlite3.connect(active_base / "index-requests/queue.sqlite3", isolation_level=None)
    ) as db:
        db.execute("BEGIN EXCLUSIVE")
        result = _run_runner(tmp_path, runner=root / "scripts/code_intel_runner.sh")
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 8
    assert _inventory(tmp_path) == before
    info = json.loads(marker.read_text())
    assert info["fd8"] is False and info["stdin_character"] is True
    status = Path(f"/proc/{info['pid']}/stat")
    deadline = time.monotonic() + 2
    while status.exists() and time.monotonic() < deadline:
        try:
            if status.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
        except FileNotFoundError:
            break
        time.sleep(0.02)
    else:
        assert not status.exists(), "owned notifier survived the observer deadline"
    lock = active_base / "locks/code-intel-runner.lock"
    assert subprocess.run(["flock", "-n", str(lock), "true"], timeout=5).returncode == 0
    pending = json.loads(clock.read_text())["pending"]["prolonged_busy"]
    assert not list((active_base / "alerts/queue").glob("*.json"))
    # New source is explicitly recaptured; no live helper is substituted into
    # the prior immutable bundle. Health debt keeps its original dedupe key.
    (root / "scripts/lib/alert_queue.sh").write_bytes(
        (ROOT / "scripts/lib/alert_queue.sh").read_bytes()
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qam",
            "restore canonical notification",
        ],
        check=True,
        capture_output=True,
    )
    current = module.capture(root, "runner", captured.buffers[SELF])
    monkeypatch.setenv("GENESIS_HOME", str(active_base))
    replay = invoke(current, active_base, "nonbusy", "complete")
    assert replay.returncode == 0, replay.stderr
    queued = list_queued(alert_queue_root())
    assert len(queued) == 1 and queued[0][1]["dedupe_key"] == pending["key"]
    assert json.loads(clock.read_text()) == dict(version=1, active=None, pending={})


@pytest.mark.parametrize("phase", ["raw-capture", "restore", "assemble"])
def test_whole_observation_bounds_source_preparation(tmp_path, monkeypatch, phase):
    entered = tmp_path / "source-preparation-entered"
    if phase == "raw-capture":
        hook = """
_fixture_git = _git
def _git(root, *arguments, **kwargs):
 if arguments == ('rev-parse', '--verify', 'HEAD^{commit}'):
  time.sleep(8)
 return _fixture_git(root, *arguments, **kwargs)
"""
    elif phase == "restore":
        hook = """
_fixture_restore = restore
def restore(*arguments):
 time.sleep(8)
 return _fixture_restore(*arguments)
"""
    else:
        hook = """
_fixture_assemble = Snapshot.assemble
def _assemble(self):
 time.sleep(8)
 return _fixture_assemble(self)
Snapshot.assemble = _assemble
"""
    hook = hook.replace("time.sleep(8)", f"Path({str(entered)!r}).touch(); time.sleep(8)")
    root, _, captured, base = prepared(tmp_path, monkeypatch, source_hook=hook)
    if phase == "raw-capture":
        _seed_marker(tmp_path)
        base = tmp_path / ".genesis"
        (base / "code-intel-runner-busy.json").write_bytes(
            (tmp_path / "state/code-intel-runner-busy.json").read_bytes()
        )
    clock = base / "code-intel-runner-busy.json"
    before = clock.read_bytes()
    started = time.monotonic()
    if phase == "raw-capture":
        with closing(
            sqlite3.connect(base / "index-requests/queue.sqlite3", isolation_level=None)
        ) as db:
            db.execute("BEGIN EXCLUSIVE")
            result = _run_runner(tmp_path, runner=root / "scripts/code_intel_runner.sh")
        assert result.returncode == 0, result.stderr
    else:
        result = invoke(captured, base, "busy", "readiness")
        assert result.returncode != 0
    assert time.monotonic() - started < 6
    assert entered.exists(), "the bounded source-preparation seam must actually run"
    assert clock.read_bytes() == before
    assert (
        subprocess.run(
            ["flock", "-n", str(base / "locks/code-intel-runner.lock"), "true"], timeout=5
        ).returncode
        == 0
    )
