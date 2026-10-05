"""scripts/lib/server_sessions.py: the Claude Code sessions a genesis-server
restart would end, found as the server's process descendants in a FAKE /proc.

No test reads the real /proc: every case builds the process table it asserts on,
so the answer depends on nothing running on the machine.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "lib" / "server_sessions.py"

BTIME = 1_700_000_000
TICKS = 100


def _load():
    spec = importlib.util.spec_from_file_location("server_sessions_under_test", LIB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = _load()


def proc(
    root: Path,
    pid: int,
    ppid: int,
    comm: str,
    argv: list[str],
    started_s: float = 0,
    state: str = "S",
) -> None:
    """One /proc/<pid> entry. *started_s* is the start time in seconds after the
    fake host boot (BTIME)."""
    d = root / str(pid)
    d.mkdir(parents=True)
    start_ticks = int(started_s * TICKS)
    # comm is parenthesised; the fields after it: state, ppid, then 17 more up to
    # starttime (index 19 after the last ')').
    rest = [state, str(ppid)] + ["0"] * 17 + [str(start_ticks), "0"]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n")
    (d / "comm").write_text(comm + "\n")
    (d / "cmdline").write_bytes(b"\x00".join(a.encode() for a in argv) + b"\x00")


@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "proc"
    r.mkdir()
    (r / "stat").write_text(f"cpu 0 0 0 0\nbtime {BTIME}\nprocesses 1\n")
    return r


def _found(root, server, caller=None):
    return ss.server_sessions(server, root, caller)


def test_a_direct_child_of_the_server_is_found(root):
    proc(root, 1111, 1, "python", ["python", "-m", "genesis", "serve"])
    proc(root, 5000, 1111, "claude", ["claude", "-p", "hello"])
    assert [f[0] for f in _found(root, 1111)] == [5000]


def test_a_deeper_descendant_is_found(root):
    """A launcher between the server and the session (a shell, systemd-run that
    forked) does not hide it."""
    proc(root, 1111, 1, "python", ["python"])
    proc(root, 1200, 1111, "bash", ["bash", "-c", "x"])
    proc(root, 5000, 1200, "claude", ["claude", "-p", "x"])
    assert [f[0] for f in _found(root, 1111)] == [5000]


def test_a_claude_outside_the_server_tree_is_not_found(root):
    """An interactive terminal session or an ambient judge under a hook worker
    survives a server restart, so it is not reported."""
    proc(root, 1111, 1, "python", ["python"])
    proc(root, 3000, 1, "tmux", ["tmux"])
    proc(root, 3001, 3000, "claude", ["claude", "--dangerously-skip-permissions"])
    proc(root, 4000, 1, "python", ["python", "repo_pulse_worker.py"])
    proc(root, 4001, 4000, "claude", ["claude", "-p", "judge"])
    assert _found(root, 1111) == []


def test_a_non_claude_child_of_the_server_is_not_found(root):
    proc(root, 1111, 1, "python", ["python"])
    proc(root, 5000, 1111, "git", ["git", "fetch"])
    assert _found(root, 1111) == []


def test_an_interpreter_wrapped_install_is_found(root):
    proc(root, 1111, 1, "python", ["python"])
    proc(root, 5000, 1111, "node", ["node", "--enable-source-maps", "/opt/cc/cli.js", "-p", "x"])
    assert [f[0] for f in _found(root, 1111)] == [5000]


def test_age_is_measured_from_host_boot_not_container_uptime(root):
    """/proc/uptime is container-relative under LXC; the age must come from btime."""
    (root / "uptime").write_text("10.00 10.00\n")  # a decoy, deliberately tiny
    started = time.time() - BTIME - 600  # started ten minutes ago
    proc(root, 1111, 1, "python", ["python"])
    proc(root, 5000, 1111, "claude", ["claude", "-p", "x"], started_s=started)
    (pid, age, _resume, _self, start) = _found(root, 1111)[0]
    assert 590 <= age <= 610, age
    assert start == int(started * TICKS), "the start tick is reported as read"


def test_resume_id_is_reported_and_the_prompt_never_is(root):
    proc(root, 1111, 1, "python", ["python"])
    proc(
        root,
        5000,
        1111,
        "claude",
        ["claude", "-p", "SECRET PROMPT TEXT", "--resume", "0123abcd-4567-89ef-0123-456789abcdef"],
    )
    proc(root, 5001, 1111, "claude", ["claude", "-p", "x", "--resume", "not a uuid; rm -rf /"])
    out = {f[0]: f[2] for f in _found(root, 1111)}
    assert out == {5000: "0123abcd-4567-89ef-0123-456789abcdef", 5001: "-"}


def test_the_callers_own_session_is_flagged(root):
    proc(root, 1111, 1, "python", ["python"])
    proc(root, 5000, 1111, "claude", ["claude", "-p", "x"])
    proc(root, 5100, 5000, "bash", ["bash"])
    proc(root, 5200, 5100, "bash", ["bash", "deploy_code_only.sh"])
    proc(root, 6000, 1111, "claude", ["claude", "-p", "y"])
    flags = {f[0]: f[3] for f in _found(root, 1111, caller=5200)}
    assert flags == {5000: True, 6000: False}


def test_a_comm_with_a_paren_and_spaces_parses(root):
    proc(root, 1111, 1, "python", ["python"])
    proc(root, 5000, 1111, "weird) (x y", ["claude", "-p", "x"])
    assert [f[0] for f in _found(root, 1111)] == [5000]


def test_a_cycle_in_a_malformed_table_terminates(root):
    proc(root, 1111, 1, "python", ["python"])
    proc(root, 5000, 5001, "claude", ["claude"])
    proc(root, 5001, 5000, "bash", ["bash"])
    assert _found(root, 1111) == []


def test_an_unlistable_root_is_none(tmp_path):
    assert ss.server_sessions(1111, tmp_path / "missing") is None


def test_the_cli_prints_tab_separated_rows_and_exits_2_when_unreadable(root, tmp_path):
    proc(root, 1111, 1, "python", ["python"])
    proc(root, 5000, 1111, "claude", ["claude", "-p", "x"])
    r = subprocess.run(
        [sys.executable, "-I", "-S", str(LIB), "1111", str(root)], capture_output=True, text=True
    )
    assert r.returncode == 0
    cols = r.stdout.rstrip("\n").split("\t")
    assert len(cols) == 5 and cols[0] == "5000" and cols[3] == "-" and cols[4] == "0", cols
    bad = subprocess.run(
        [sys.executable, "-I", "-S", str(LIB), "1111", str(tmp_path / "missing")],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 2 and bad.stdout == ""


def test_the_claude_rules_match_slot_liveness():
    """This file copies slot_liveness's rules (it cannot import genesis). The
    sets and the verdicts must not drift apart."""
    from genesis.cc import slot_liveness as sl

    assert ss._CLAUDE_NAMES == sl._CLAUDE_NAMES
    assert ss._INTERPRETERS == sl._INTERPRETERS
    assert ss._ENTRY_SCRIPTS == sl._ENTRY_SCRIPTS
    corpus = [
        (b"claude\n", b"claude\x00-p\x00x\x00"),
        (b"node\n", b"node\x00/opt/cc/cli.js\x00"),
        (b"node\n", b"node\x00server.js\x00"),
        (b"bash\n", b"/usr/bin/claude\x00"),
        (b"claude-wrapper\n", b"claude-wrapper\x00"),
        (None, b"bun\x00-r\x00x\x00/a/cli.js\x00"),
        (None, None),
        (b"python\n", b""),
    ]
    for comm, cmdline in corpus:
        assert ss._is_claude(comm, cmdline) == sl._is_claude(comm, cmdline), (comm, cmdline)


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_a_dead_claude_process_is_not_a_session(root, state):
    """Exited but not reaped (Z) or being torn down (X): nothing to lose or kill."""
    proc(root, 1111, 1, "python", ["python"])
    proc(root, 5000, 1111, "claude", [], state=state)
    proc(root, 5001, 1111, "claude", ["claude", "-p", "x"])
    assert [f[0] for f in _found(root, 1111)] == [5001]


# -- the --rows form: background sessions this server boot still runs ---------
BOOT = 1_790_000_000


def _iso(unix: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(unix)) + ".123456+00:00"


@pytest.fixture()
def db(tmp_path):
    import sqlite3

    path = tmp_path / "genesis.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE cc_sessions (id TEXT, session_type TEXT, status TEXT,"
        " source_tag TEXT, started_at TEXT)"
    )
    con.commit()
    con.close()
    return path


def _add(db, *rows) -> None:
    import sqlite3

    con = sqlite3.connect(db)
    con.executemany("INSERT INTO cc_sessions VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()


U1 = "11111111-2222-4333-8444-555555555555"
U2 = "66666666-7777-4888-8999-aaaaaaaaaaaa"


def test_rows_are_active_background_sessions_of_this_boot(db):
    _add(
        db,
        (U1, "background_task", "active", "direct_session", _iso(BOOT + 5)),
        (U2, "background_reflection", "active", "reflection_deep", _iso(BOOT + 0.5)),
        ("old", "background_task", "active", "direct_session", _iso(BOOT - 1)),
        ("done", "background_task", "completed", "direct_session", _iso(BOOT + 9)),
        ("fg", "foreground", "active", "", _iso(BOOT + 9)),
    )
    assert ss.server_rows(str(db), BOOT) == [
        (U2, "reflection_deep", _iso(BOOT + 0.5)),
        (U1, "direct_session", _iso(BOOT + 5)),
    ]


def test_a_row_started_in_the_boot_second_counts(db):
    _add(db, (U1, "background_task", "active", "t", _iso(BOOT)))
    assert [r[0] for r in ss.server_rows(str(db), BOOT)] == [U1]


def test_row_text_is_sanitized_and_a_non_uuid_id_is_withheld(db):
    _add(db, ("x; rm -rf /", "background_task", "active", "a\nb\x1bc" + "z" * 200, _iso(BOOT + 1)))
    ((rid, tag, _started),) = ss.server_rows(str(db), BOOT)
    assert rid == "-"
    assert "\n" not in tag and "\x1b" not in tag and len(tag) <= 60


def test_the_rows_cli_caps_its_output_and_exits_2_when_unreadable(db, tmp_path):
    _add(
        db,
        *[
            (
                f"{i:08d}-0000-4000-8000-000000000000",
                "background_task",
                "active",
                "t",
                _iso(BOOT + 1 + i),
            )
            for i in range(23)
        ],
    )
    r = subprocess.run(
        [sys.executable, "-I", "-S", str(LIB), "--rows", str(db), str(BOOT)],
        capture_output=True,
        text=True,
    )
    lines = r.stdout.splitlines()
    assert r.returncode == 0 and len(lines) == 21
    assert lines[-1] == "more\t3\t-"
    for bad in (tmp_path / "missing.db", tmp_path):
        b = subprocess.run(
            [sys.executable, "-I", "-S", str(LIB), "--rows", str(bad), str(BOOT)],
            capture_output=True,
            text=True,
        )
        assert b.returncode == 2 and b.stdout == "", bad
