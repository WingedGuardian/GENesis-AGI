"""scripts/lib/wg_proc.py: a holder's identity, read through one /proc handle.

What these pin:
  * a real process holding a file for writing is reported with ITS start time
    (stat field 22), its name and its parent;
  * a reader, a descriptor now leading elsewhere, and an exited process are
    not holders;
  * a read through a handle whose process has exited reports "gone", never the
    pid's next owner (the property the handle exists for);
  * names are made safe for a page (control bytes, non-UTF-8, Unicode
    line/paragraph separators, bidi and zero-width format characters -> "?");
  * every candidate of one pid is read through ONE handle, so a pid reused
    between two of its candidates cannot lend the first file its identity;
  * an unreadable stat record falls back to comm/status through the same
    handle, with the start time "?" (cannot tell), never guessed;
  * a parent that changed while it was read is not named.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PY = _ROOT / "scripts" / "lib" / "wg_proc.py"
_spec = importlib.util.spec_from_file_location("wg_proc", _PY)
wg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wg)


def _run(root: str, stdin: str) -> list[list[str]]:
    out = subprocess.run(
        [sys.executable, "-I", "-S", str(_PY), root, "holders"],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return [line.split("\t") for line in out.splitlines()]


def _starttime(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_bytes()
    return raw.rpartition(b")")[2].split()[19].decode()


def _key(path: Path) -> str:
    st = os.stat(path)
    return f"{st.st_dev}:{st.st_ino}"


@pytest.fixture
def writer(tmp_path):
    """A real process holding tmp_path/out open for writing on fd 3."""
    target = tmp_path / "out"
    target.touch()
    p = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sys,time; os.open(sys.argv[1], os.O_WRONLY|os.O_APPEND); time.sleep(60)",
            str(target),
        ],
    )
    deadline = time.time() + 10
    while time.time() < deadline and not any(
        os.path.realpath(f"/proc/{p.pid}/fd/{n}") == str(target)
        for n in os.listdir(f"/proc/{p.pid}/fd")
    ):
        time.sleep(0.05)
    fd = next(
        n
        for n in os.listdir(f"/proc/{p.pid}/fd")
        if os.path.realpath(f"/proc/{p.pid}/fd/{n}") == str(target)
    )
    yield p, target, fd
    if p.poll() is None:
        p.kill()
        p.wait()


def test_a_real_writer_is_reported_with_its_own_start_time(writer):
    p, target, fd = writer
    rows = _run("/proc", f"{_key(target)} {p.pid} {fd}\n")
    assert len(rows) == 1
    key, pid, st, state, comm, ppid, pcomm = rows[0]
    assert (key, pid) == (_key(target), str(p.pid))
    assert st == _starttime(p.pid)
    assert state in {"S", "R", "D"}
    assert comm.startswith("python")
    assert ppid == str(os.getpid())
    assert pcomm != "?"


def test_a_reader_is_not_a_holder(tmp_path):
    target = tmp_path / "out"
    target.touch()
    p = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sys,time; os.open(sys.argv[1], os.O_RDONLY); time.sleep(60)",
            str(target),
        ]
    )
    try:
        time.sleep(0.5)
        fds = [
            n
            for n in os.listdir(f"/proc/{p.pid}/fd")
            if os.path.realpath(f"/proc/{p.pid}/fd/{n}") == str(target)
        ]
        assert fds, "the reader opened the file"
        assert _run("/proc", f"{_key(target)} {p.pid} {fds[0]}\n") == []
    finally:
        p.kill()
        p.wait()


def test_a_descriptor_leading_to_another_file_is_not_a_holder(writer, tmp_path):
    p, _target, fd = writer
    other = tmp_path / "other"
    other.touch()
    assert _run("/proc", f"{_key(other)} {p.pid} {fd}\n") == []


def test_an_exited_process_is_not_a_holder(writer):
    p, target, fd = writer
    p.kill()
    p.wait()
    assert _run("/proc", f"{_key(target)} {p.pid} {fd}\n") == []


def test_a_handle_on_an_exited_process_reads_gone_not_the_next_owner(writer):
    """The binding itself: once the process behind a handle exits, reads through
    the handle fail; nothing can be read from whatever takes the pid next."""
    p, _target, _fd = writer
    dfd = os.open(f"/proc/{p.pid}", os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert wg._stat_fields(dfd) is not None
        p.kill()
        p.wait()
        assert wg._stat_fields(dfd) is None
    finally:
        os.close(dfd)


# ── a fake process table ──────────────────────────────────────────


def _fake(
    proc: Path,
    pid: int,
    target: Path,
    *,
    comm: bytes = b"bash",
    ppid: int = 1,
    st: int = 777,
    stat: bool = True,
    flags: str = "0102001",
) -> None:
    d = proc / str(pid)
    (d / "fd").mkdir(parents=True, exist_ok=True)
    (d / "fdinfo").mkdir(exist_ok=True)
    (d / "fd" / "1").symlink_to(target)
    (d / "fdinfo" / "1").write_text(f"pos:\t0\nflags:\t{flags}\n")
    (d / "comm").write_bytes(comm + b"\n")
    (d / "status").write_bytes(b"Name:\t" + comm + b"\nPPid:\t" + str(ppid).encode() + b"\n")
    if stat:
        rest = ["S", str(ppid)] + ["0"] * 17 + [str(st)] + ["0"] * 10
        (d / "stat").write_bytes(
            f"{pid} (".encode() + comm + b") " + " ".join(rest).encode() + b"\n"
        )


@pytest.fixture
def fake(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    target = tmp_path / "data" / "out"
    target.parent.mkdir()
    target.touch()
    return proc, target


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        (b"evil\nFAKE LINE", "evil?FAKE LINE"),
        (b"tab\there", "tab?here"),
        (b"del\x7f", "del?"),
        (b"c1\xc2\x85x", "c1?x"),
        (b"bad\xffutf8", "bad?utf8"),
        (b"caf\xc3\xa9 (x) y", "café (x) y"),
        (b"ls\xe2\x80\xa8FAKE", "ls?FAKE"),  # U+2028 line separator
        (b"ls\xe2\x80\xa9FAKE", "ls?FAKE"),  # U+2029 paragraph separator
        (b"ls\xe2\x80\xaegpj", "ls?gpj"),  # U+202E right-to-left override
        (b"cl\xe2\x80\x8baude", "cl?aude"),  # U+200B zero-width space
        (b"x\xe2\x81\xa6y", "x?y"),  # U+2066 left-to-right isolate
    ],
)
def test_names_are_made_safe_for_a_page(fake, raw, shown):
    proc, target = fake
    _fake(proc, 4100, target, comm=raw)
    rows = _run(str(proc), f"{_key(target)} 4100 1\n")
    assert rows and rows[0][4] == shown
    assert rows[0][2] == "777", "the start time is read after the LAST ')'"


def test_an_unreadable_stat_record_falls_back_without_guessing(fake):
    proc, target = fake
    _fake(proc, 4101, target, comm=b"grep", ppid=4100, stat=False)
    _fake(proc, 4100, target, comm=b"bash")
    rows = _run(str(proc), f"{_key(target)} 4101 1\n")
    assert rows == [[_key(target), "4101", "?", "?", "grep", "4100", "bash"]]


def test_a_parent_that_changed_while_read_is_not_named(fake, monkeypatch):
    proc, target = fake
    _fake(proc, 4102, target, ppid=4100)
    _fake(proc, 4100, target, comm=b"claude")
    real = wg._stat_fields
    calls = []

    def flaky(dfd):
        calls.append(dfd)
        start, state, comm, ppid = real(dfd)
        # The holder's second read (the re-check) sees a new parent: reparented.
        if comm == "bash" and len(calls) > 1:
            return (start, state, comm, "1")
        return (start, state, comm, ppid)

    monkeypatch.setattr(wg, "_stat_fields", flaky)
    rows = wg.holders(str(proc), [f"{_key(target)} 4102 1"])
    assert rows and rows[0].split("\t")[5:] == ["?", "?"]


def test_every_candidate_of_one_pid_is_read_through_one_handle(fake, monkeypatch):
    """One process holding two files: /proc/<pid> is opened once for both, so
    both rows describe the same process (review finding: per-candidate opens
    let a pid reused in between lend the first file the new owner's identity)."""
    proc, target = fake
    other = target.parent / "other"
    other.touch()
    _fake(proc, 4105, target, comm=b"dd")
    (proc / "4105" / "fd" / "2").symlink_to(other)
    (proc / "4105" / "fdinfo" / "2").write_text("pos:\t0\nflags:\t0102001\n")
    real_open = os.open
    opened = []

    def counting(path, *a, **kw):
        if str(path) == f"{proc}/4105":
            opened.append(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(wg.os, "open", counting)
    rows = wg.holders(
        str(proc), [f"{_key(target)} 4105 1", f"{_key(other)} 4105 2", f"{_key(target)} 4105 1"]
    )
    assert len(opened) == 1, "one handle per pid"
    fields = [r.split("\t") for r in rows]
    assert [f[0] for f in fields] == [_key(target), _key(other)], "each file once, in order"
    assert fields[0][1:] == fields[1][1:], "both rows describe the same process"


def test_malformed_candidates_are_skipped(fake):
    proc, target = fake
    _fake(proc, 4103, target)
    stdin = f"{_key(target)} 4103\nx y z\n{_key(target)} 41a3 1\n{_key(target)} 4103 1\n"
    rows = _run(str(proc), stdin)
    assert [r[1] for r in rows] == ["4103"]


def test_a_read_only_fake_descriptor_is_not_a_holder(fake):
    proc, target = fake
    _fake(proc, 4104, target, flags="0100000")
    assert _run(str(proc), f"{_key(target)} 4104 1\n") == []


def test_usage_error_exits_2():
    r = subprocess.run(
        [sys.executable, "-I", "-S", str(_PY), "/proc"], capture_output=True, timeout=60
    )
    assert r.returncode == 2
