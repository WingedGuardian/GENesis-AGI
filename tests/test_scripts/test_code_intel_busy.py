"""Queue health timing cannot authorize work or lose an already-due notice."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "busy_fixture", ROOT / "scripts/lib/code_intel_busy.py"
)
busy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(busy)
BOOT = "11111111-1111-4111-8111-111111111111"
OTHER_BOOT = "22222222-2222-4222-8222-222222222222"
DUE = busy.THRESHOLD_NS


def read(base):
    return json.loads((base / "code-intel-runner-busy.json").read_text())


def tick(base, at, callback, outcome="busy", boot=BOOT):
    return busy.observe(
        base,
        outcome,
        "readiness",
        callback,
        now_ns=at,
        boot_id=boot,
        proc_root=base / "synthetic-proc",
    )


def test_exact_threshold_queues_once_and_keeps_first_observation(tmp_path):
    calls = []

    def callback(*args):
        calls.append(copy.deepcopy(args))
        return True

    assert tick(tmp_path, 0, callback)
    assert tick(tmp_path, DUE - 1, callback) and not calls
    assert read(tmp_path)["active"]["first_busy_ns"] == 0
    assert tick(tmp_path, DUE, callback)
    assert tick(tmp_path, DUE + 1, callback) and len(calls) == 1
    assert calls[0][0] == "prolonged_busy"
    assert calls[0][1]["first"]["last_ns"] == DUE
    assert read(tmp_path)["pending"] == {}
    assert read(tmp_path)["active"]["due_registered"] is True


@pytest.mark.parametrize("outcome", ["nonbusy", "error"])
def test_ending_clock_replays_failed_notice_instead_of_discarding_it(tmp_path, outcome):
    assert tick(tmp_path, 0, lambda *args: True)
    assert not tick(tmp_path, DUE, lambda *args: False)
    pending = read(tmp_path)["pending"]["prolonged_busy"]
    calls = []
    assert tick(tmp_path, DUE + 1, lambda *args: calls.append(copy.deepcopy(args)) or True, outcome)
    assert read(tmp_path) == dict(version=1, active=None, pending={})
    assert calls[0][1] == pending
    assert tick(tmp_path, DUE + 2, lambda *args: True)
    assert read(tmp_path)["active"]["first_busy_ns"] == DUE + 2


def test_unobserved_tick_does_not_reset_or_retry(tmp_path):
    tick(tmp_path, 0, lambda *args: True)
    tick(tmp_path, DUE, lambda *args: False)
    path = tmp_path / "code-intel-runner-busy.json"
    before = path.read_bytes()
    assert tick(tmp_path, DUE + 1, lambda *args: pytest.fail("unobserved retry"), "unobserved")
    assert path.read_bytes() == before


def test_new_boot_starts_age_again_but_preserves_old_notice(tmp_path):
    tick(tmp_path, 0, lambda *args: True)
    tick(tmp_path, DUE, lambda *args: False)
    old = read(tmp_path)["pending"]["prolonged_busy"]
    assert not tick(tmp_path, 1, lambda *args: False, boot=OTHER_BOOT)
    value = read(tmp_path)
    assert value["active"]["first_busy_ns"] == 1
    assert value["active"]["boot_id"] == OTHER_BOOT
    assert value["pending"]["prolonged_busy"] == old


def test_two_due_intervals_coalesce_under_oldest_pending_identity(tmp_path):
    tick(tmp_path, 0, lambda *args: True)
    tick(tmp_path, DUE, lambda *args: False)
    first = read(tmp_path)["pending"]["prolonged_busy"]
    tick(tmp_path, DUE + 1, lambda *args: False, "nonbusy")
    tick(tmp_path, DUE + 2, lambda *args: False)
    tick(tmp_path, 2 * DUE + 2, lambda *args: False)
    value = read(tmp_path)["pending"]["prolonged_busy"]
    assert value["key"] == first["key"] and value["first"] == first["first"]
    assert value["count"] == 2 and value["latest"]["first_ns"] == DUE + 2
    tick(tmp_path, 2 * DUE + 3, lambda *args: False)
    assert read(tmp_path)["pending"]["prolonged_busy"]["count"] == 2


@pytest.mark.parametrize("corruption", ["json", "deep", "invalid-utf8", "clock", "regression"])
def test_malformed_clock_error_debt_survives_young_and_nonbusy_ticks(tmp_path, corruption):
    tick(tmp_path, 100, lambda *args: True)
    path = tmp_path / "code-intel-runner-busy.json"
    if corruption == "json":
        path.write_text("{")
    elif corruption == "deep":
        path.write_text("[" * 15000 + "0" + "]" * 15000)
    elif corruption == "invalid-utf8":
        path.write_bytes(b"\xff")
    elif corruption == "clock":
        value = read(tmp_path)
        value["active"]["last_busy_ns"] = True
        path.write_text(json.dumps(value))
    at = 99 if corruption == "regression" else 101
    assert not tick(tmp_path, at, lambda *args: False)
    error = read(tmp_path)["pending"]["observation_error"]
    assert not tick(tmp_path, 102, lambda *args: False)
    assert not tick(tmp_path, 103, lambda *args: False, "nonbusy")
    assert read(tmp_path)["pending"]["observation_error"] == error
    assert read(tmp_path)["active"] is None


def test_invalid_active_record_retains_valid_prior_debt(tmp_path):
    tick(tmp_path, 0, lambda *args: True)
    tick(tmp_path, DUE, lambda *args: False)
    value = read(tmp_path)
    old = copy.deepcopy(value["pending"])
    value["active"] = {"broken": True}
    (tmp_path / "code-intel-runner-busy.json").write_text(json.dumps(value))
    assert not tick(tmp_path, DUE + 1, lambda *args: False)
    assert read(tmp_path)["pending"]["prolonged_busy"] == old["prolonged_busy"]
    assert "observation_error" in read(tmp_path)["pending"]


@pytest.mark.parametrize("kind", ["future", "oversized", "symlink", "directory", "fifo"])
def test_unknown_or_nonregular_record_is_not_rewritten(tmp_path, kind):
    path = tmp_path / "code-intel-runner-busy.json"
    if kind == "future":
        path.write_text('{"version":2}')
    elif kind == "oversized":
        path.write_text(" " * 65537)
    elif kind == "symlink":
        destination = tmp_path / "kept"
        destination.write_text("kept")
        path.symlink_to(destination)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    before = path.lstat()
    with pytest.raises(OSError):
        tick(tmp_path, 1, lambda *args: pytest.fail("unpersisted success"))
    after = path.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )


@pytest.mark.parametrize("boundary", ["file-sync", "replace", "directory-sync"])
def test_publication_failure_preserves_actual_boundary_and_never_enqueues(
    tmp_path, monkeypatch, boundary
):
    tick(tmp_path, 0, lambda *args: True)
    path = tmp_path / "code-intel-runner-busy.json"
    old = path.read_bytes()
    calls = 0
    original_sync = busy.os.fsync

    def sync(fd):
        nonlocal calls
        calls += 1
        if calls == (2 if boundary == "directory-sync" else 1):
            raise OSError("injected sync failure")
        original_sync(fd)

    if boundary == "replace":
        monkeypatch.setattr(
            busy.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("replace"))
        )
    else:
        monkeypatch.setattr(busy.os, "fsync", sync)
    with pytest.raises(OSError):
        tick(tmp_path, DUE, lambda *args: pytest.fail("enqueued before durable debt"))
    assert (path.read_bytes() == old) is (boundary != "directory-sync")
    assert not list(tmp_path.glob("*.tmp"))


def test_lost_ack_replays_same_key_and_does_not_recount_interval(tmp_path, monkeypatch):
    tick(tmp_path, 0, lambda *args: True)
    publish = busy._publish
    calls = 0
    keys = []

    def fail_ack(path, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("ack write failed")
        publish(path, value)

    monkeypatch.setattr(busy, "_publish", fail_ack)

    def callback(kind, item, snapshot):
        keys.append(item["key"])
        return True

    with pytest.raises(OSError):
        tick(tmp_path, DUE, callback)
    assert read(tmp_path)["pending"]["prolonged_busy"]["count"] == 1
    assert tick(tmp_path, DUE + 1, callback)
    assert len(keys) == 2 and keys[0] == keys[1]
    assert read(tmp_path)["pending"] == {}


def test_holder_snapshot_ignores_waiters_and_sanitizes_comm(tmp_path):
    directory = tmp_path / "index-requests"
    directory.mkdir()
    database = directory / "queue.sqlite3"
    database.touch()
    info = database.stat()
    device = f"{os.major(info.st_dev):x}:{os.minor(info.st_dev):x}:{info.st_ino}"
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "42/comm").write_bytes(b"owner\nspoof\xe2\x80\xae\xff\n")
    (proc / "locks").write_text(
        f"1: POSIX ADVISORY WRITE 42 {device} 0 EOF\n"
        f"1: -> POSIX ADVISORY WRITE 99 {device} 0 EOF\n"
        f"2: OFDLCK ADVISORY WRITE -1 {device} 0 EOF\n"
    )
    result = busy.holders(tmp_path, proc)
    assert "PID 42, kernel comm owner?spoof??;" in result
    assert "PID 99" not in result and "owner unknown" in result
    assert "namespace" in result and "Original" not in result


@pytest.mark.parametrize("kind", ["flock", "lockf"])
def test_real_scratch_kernel_lock_holder_is_reported(tmp_path, kind):
    directory = tmp_path / "index-requests"
    directory.mkdir()
    path = directory / "queue.sqlite3"
    command = (
        'import fcntl,sys; f=open(sys.argv[1],"a"); '
        f"fcntl.{kind}(f,fcntl.LOCK_EX); "
        'print("locked",flush=True); sys.stdin.readline()'
    )
    child = subprocess.Popen(
        [sys.executable, "-c", command, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        snapshot = busy.holders(tmp_path)
        assert f"queue.sqlite3: PID {child.pid}, kernel comm " in snapshot
    finally:
        child.communicate("\n", timeout=10)
    assert child.returncode == 0


@pytest.mark.parametrize(
    "evidence", ["matching", "open-only", "different-lock", "waiter", "closed"]
)
def test_layered_device_join_requires_the_exact_descriptor_lock(tmp_path, monkeypatch, evidence):
    directory = tmp_path / "index-requests"
    directory.mkdir()
    database = directory / "queue.sqlite3"
    database.touch()
    info = database.stat()
    proc = tmp_path / "proc"
    (proc / "42/fd").mkdir(parents=True)
    (proc / "42/fdinfo").mkdir()
    (proc / "42/comm").write_text("holder\n")
    descriptor = proc / "42/fd/3"
    descriptor.symlink_to(database)
    # Deliberately differs from the visible device; the inode alone is not proof.
    device = f"aa:bb:{info.st_ino}"
    row = f"POSIX ADVISORY WRITE 42 {device} 0 EOF"
    (proc / "locks").write_text("99: " + row + "\n")
    detail = proc / "42/fdinfo/3"
    if evidence == "open-only":
        text = "pos: 0\n"
    elif evidence == "different-lock":
        text = "lock: 1: " + row.replace("0 EOF", "1 EOF") + "\n"
    elif evidence == "waiter":
        text = "lock: 1: -> " + row + "\n"
    else:
        text = "lock: 1: " + row + "\n"
    detail.write_text(text)
    if evidence == "closed":
        original = Path.read_text

        def close_during_read(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == detail:
                descriptor.unlink()
            return value

        monkeypatch.setattr(Path, "read_text", close_during_read)
    result = busy.holders(tmp_path, proc)
    assert ("PID 42" in result) is (evidence == "matching")


def test_next_publication_prunes_only_owned_regular_health_temporaries(tmp_path):
    orphan = tmp_path / ".code-intel-runner-busy.json.leftover.tmp"
    orphan.write_text("interrupted partial health clock")
    other = tmp_path / ".unrelated.tmp"
    other.write_text("kept")
    link = tmp_path / ".code-intel-runner-busy.json.link.tmp"
    link.symlink_to(other)
    assert tick(tmp_path, 0, lambda *args: True)
    assert not orphan.exists() and other.read_text() == "kept"
    assert link.is_symlink()


def test_invalid_other_notice_cannot_discard_valid_due_debt(tmp_path):
    tick(tmp_path, 0, lambda *args: True)
    tick(tmp_path, DUE, lambda *args: False)
    value = read(tmp_path)
    expected = copy.deepcopy(value["pending"]["prolonged_busy"])
    value["pending"]["observation_error"] = {"broken": True}
    (tmp_path / "code-intel-runner-busy.json").write_text(json.dumps(value))
    assert not tick(tmp_path, DUE + 1, lambda *args: False)
    assert read(tmp_path)["pending"]["prolonged_busy"] == expected
    assert read(tmp_path)["pending"]["observation_error"]["count"] == 1


@pytest.mark.parametrize("failure", ["future", "directory", "write"])
def test_unavailable_health_record_reuses_real_queue_identity(tmp_path, monkeypatch, failure):
    path = tmp_path / "code-intel-runner-busy.json"
    if failure == "future":
        path.write_text('{"version":2}')
    elif failure == "directory":
        path.mkdir()
    else:
        publish = busy._publish

        def uncertain_write(*args):
            publish(*args)
            raise OSError("publication acknowledgement unavailable")

        monkeypatch.setattr(busy, "_publish", uncertain_write)
    before = path.lstat() if path.exists() else None
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    now = count(10)
    monkeypatch.setattr(busy.time, "monotonic_ns", lambda: next(now))
    captured = SimpleNamespace(
        buffers={"scripts/lib/alert_queue.sh": (ROOT / "scripts/lib/alert_queue.sh").read_bytes()}
    )
    assert busy.worker(captured, "busy", "readiness") == 76
    assert busy.worker(captured, "nonbusy", "complete") == 76
    notices = list((tmp_path / "alerts/queue").glob("*.json"))
    assert len(notices) == 1, "unchanged unavailable monitoring minted a second real queue notice"
    notice = json.loads(notices[0].read_text())
    assert notice["dedupe_key"] and "observation_error" in notice["body"]
    if before is not None:
        after = path.lstat()
        assert (before.st_ino, before.st_mode, before.st_mtime_ns) == (
            after.st_ino, after.st_mode, after.st_mtime_ns
        )


@pytest.mark.parametrize("relative", [False, True])
def test_unavailable_health_identity_separates_roots_and_boots(tmp_path, monkeypatch, relative):
    keys, boot = [], [BOOT]
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path("/proc/sys/kernel/random/boot_id"):
            return boot[0]
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    now = count(10)
    monkeypatch.setattr(busy.time, "monotonic_ns", lambda: next(now))
    monkeypatch.setattr(
        busy, "_enqueue", lambda captured, kind, item, snapshot: keys.append(item["key"]) or True
    )
    for name, identity in [("one", BOOT), ("two", BOOT), ("one", OTHER_BOOT), ("one", BOOT)]:
        base = tmp_path / name
        base.mkdir(exist_ok=True)
        path = base / "code-intel-runner-busy.json"
        path.write_text('{"version":2}')
        boot[0] = identity
        if relative:
            monkeypatch.chdir(base)
        monkeypatch.setenv("GENESIS_HOME", "." if relative else str(base))
        assert busy.worker(SimpleNamespace(), "busy", "readiness") == 76
        assert path.read_text() == '{"version":2}'
    assert keys[0] == keys[3], "a later attempt in the same root/boot changed identity"
    assert keys[0] != keys[1], "independent health roots cross-suppress each other"
    assert keys[0] != keys[2], "a new boot reuses the prior boot's unavailable identity"
