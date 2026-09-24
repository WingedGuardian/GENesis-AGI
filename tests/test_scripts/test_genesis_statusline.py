"""scripts/genesis_statusline.py — the native Claude Code ``statusLine`` command.

Install-agnostic: the ledger DB is a temp SQLite file, the review-round state
lives under a temp HOME, and the script is driven through its real ``main()``
(or a real subprocess) with a real stdin payload.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "genesis_statusline.py"


def _seed_db(path: Path, charters=(), rows=()):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE session_charters (session_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE session_ledger (id TEXT, session_id TEXT, status TEXT)")
    conn.executemany("INSERT INTO session_charters VALUES (?)", [(c,) for c in charters])
    conn.executemany(
        "INSERT INTO session_ledger VALUES (?, ?, ?)",
        [(f"r{i}", sid, st) for i, (sid, st) in enumerate(rows)],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def sl(tmp_path, monkeypatch):
    mod = private_module("genesis_statusline_under_test", _SCRIPT)
    db = tmp_path / "genesis.db"
    monkeypatch.setattr(mod, "_db_path", lambda: db)
    monkeypatch.setattr(mod, "_review_state", lambda cwd: (2, "feat/x", 3))
    mod.test_db = db
    return mod


def _run(sl, monkeypatch, capsys, payload, argv=()):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    rc = sl.main(list(argv))
    return rc, capsys.readouterr().out


def test_all_fields_render_from_the_canonical_db(sl, tmp_path, monkeypatch, capsys):
    _seed_db(
        sl.test_db,
        charters=["s1", "other"],
        rows=[
            ("s1", "open"),
            ("s1", "in_progress"),
            ("s1", "done"),
            ("s1", "absorbed"),
            ("s1", "dropped"),
            ("other", "open"),
        ],
    )
    rc, out = _run(
        sl,
        monkeypatch,
        capsys,
        {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "pr": {"number": 42, "review_state": "approved"},
        },
    )
    assert rc == 0
    assert out == "feat/x · ledger:2 · streak:2/3 · PR#42:approved\n"


def test_ledger_ignores_a_stale_charter_mirror(sl, tmp_path, monkeypatch, capsys):
    """The mirror can lag the DB (measured live: DB 4 open, mirror 5)."""
    _seed_db(sl.test_db, charters=["s1"], rows=[("s1", "open")])
    stale = tmp_path / "sessions" / "s1"
    stale.mkdir(parents=True)
    (stale / "charter.md").write_text("## Ledger\n\n" + "- [ ] x\n" * 5, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    _, out = _run(sl, monkeypatch, capsys, {"session_id": "s1", "cwd": "/x"})
    assert "ledger:1" in out


def test_session_without_a_charter_is_unknown_not_zero(sl, monkeypatch, capsys):
    _seed_db(sl.test_db, charters=[], rows=[("s9", "open")])
    _, out = _run(sl, monkeypatch, capsys, {"session_id": "s9", "cwd": "/x"})
    assert "ledger:—" in out


def test_chartered_session_with_nothing_open_reads_zero(sl, monkeypatch, capsys):
    _seed_db(sl.test_db, charters=["s3"], rows=[("s3", "done")])
    _, out = _run(sl, monkeypatch, capsys, {"session_id": "s3", "cwd": "/x"})
    assert "ledger:0" in out


def test_missing_db_and_failing_git_render_absent(sl, monkeypatch, capsys):
    monkeypatch.setattr(sl, "_review_state", lambda cwd: (_ for _ in ()).throw(OSError("boom")))
    rc, out = _run(sl, monkeypatch, capsys, {"session_id": "s", "cwd": "/nonexistent"})
    assert rc == 0
    assert out == "— · ledger:— · streak:— · PR:—\n"


def test_unmigrated_db_renders_absent(sl, monkeypatch, capsys):
    sqlite3.connect(sl.test_db).close()  # exists, no tables
    rc, out = _run(sl, monkeypatch, capsys, {"session_id": "s", "cwd": "/x"})
    assert rc == 0 and "ledger:—" in out


def test_unknown_branch_does_not_claim_a_zero_streak(sl, monkeypatch, capsys):
    monkeypatch.setattr(sl, "_review_state", lambda cwd: (0, "unknown", 3))
    _, out = _run(sl, monkeypatch, capsys, {"cwd": "/"})
    assert out.startswith("— · ") and "streak:—" in out


@pytest.mark.parametrize("raw", ["{not json", "[" * 200_000, "[1, 2]", ""])
def test_hostile_stdin_still_prints_a_line(sl, monkeypatch, capsys, raw):
    rc, out = _run(sl, monkeypatch, capsys, raw)
    assert rc == 0
    assert out.startswith("feat/x · ledger:— · ")


def test_bool_pr_number_is_not_a_pr(sl, monkeypatch, capsys):
    _, out = _run(sl, monkeypatch, capsys, {"cwd": "/x", "pr": {"number": True}})
    assert out.rstrip("\n").endswith("PR:—")


def test_pr_without_review_state(sl, monkeypatch, capsys):
    _, out = _run(sl, monkeypatch, capsys, {"cwd": "/x", "pr": {"number": 7}})
    assert out.rstrip("\n").endswith("PR#7")


def test_render_crash_still_prints_a_line(sl, monkeypatch, capsys):
    monkeypatch.setattr(sl, "render", lambda data: 1 / 0)
    rc, out = _run(sl, monkeypatch, capsys, {"cwd": "/x"})
    assert rc == 0 and out == "— · ledger:— · streak:— · PR:—\n"


def _py(code: str) -> str:
    return f'{sys.executable} -c "{code}"'


def test_then_runs_the_chained_command_with_the_same_stdin(sl, monkeypatch, capsys):
    cmd = _py("import sys,json; print('chained:'+json.load(sys.stdin)['marker'])")
    rc, out = _run(sl, monkeypatch, capsys, {"cwd": "/x", "marker": "abc123"}, ["--then", cmd])
    assert rc == 0
    assert out.splitlines() == ["feat/x · ledger:— · streak:2/3 · PR:—", "chained:abc123"]


@pytest.mark.parametrize(
    "code",
    [
        "import sys; sys.stdout.buffer.write(b'\\xff ok\\n')",  # non-UTF-8 stdout
        "import sys; sys.stderr.buffer.write(b'\\xff'); sys.exit(1)",  # non-UTF-8 stderr, fails
        "import sys; sys.exit(3)",
    ],
)
def test_misbehaving_chained_command_never_costs_our_line(sl, monkeypatch, capsys, code):
    rc, out = _run(sl, monkeypatch, capsys, {"cwd": "/x"}, ["--then", _py(code)])
    assert rc == 0
    assert out.splitlines()[0] == "feat/x · ledger:— · streak:2/3 · PR:—"


def test_non_utf8_chained_output_keeps_its_rows_with_replacement(sl, monkeypatch, capsys):
    """Decoded with replacement, not dropped: the operator's other status line
    must survive one bad byte, not vanish behind the catch-all."""
    code = "import sys; sys.stdout.buffer.write(b'\\xff ok\\n')"
    _, out = _run(sl, monkeypatch, capsys, {"cwd": "/x"}, ["--then", _py(code)])
    assert out.splitlines()[1] == "� ok"


def _pid_gone(pid: int, within: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_hung_chained_command_is_killed_with_its_children(sl, tmp_path, monkeypatch, capsys):
    """Past the cap the chained command's WHOLE group dies — including a
    grandchild the shell spawned, which killing the shell alone would orphan."""
    import time

    monkeypatch.setattr(sl, "_CHAINED_TIMEOUT_S", 0.5)
    kid = tmp_path / "kid.pid"
    cmd = f"sleep 30 & echo $! > {kid}; wait"
    t0 = time.monotonic()
    rc, out = _run(sl, monkeypatch, capsys, {"cwd": "/x"}, ["--then", cmd])
    assert time.monotonic() - t0 < 10, "the cap did not bound the wait"
    assert rc == 0
    assert out.splitlines() == ["feat/x · ledger:— · streak:2/3 · PR:—"]
    pid = int(kid.read_text())  # guard-the-guard: the grandchild really existed
    assert _pid_gone(pid), f"grandchild {pid} survived the timeout"


def test_a_descendant_that_escaped_the_group_cannot_hold_the_cap_open(sl, monkeypatch, capsys):
    """`setsid` moves a descendant out of the group we kill while it keeps the
    stdout pipe; draining that pipe after the kill would wait for IT (measured:
    a 1s cap held 20s). The cap must hold anyway."""
    import time

    monkeypatch.setattr(sl, "_CHAINED_TIMEOUT_S", 0.5)
    t0 = time.monotonic()
    rc, out = _run(sl, monkeypatch, capsys, {"cwd": "/x"}, ["--then", "setsid sleep 20 &"])
    assert time.monotonic() - t0 < 5, "an escaped descendant held the cap open"
    assert rc == 0 and out.startswith("feat/x · ")


def test_sigterm_to_the_script_takes_the_chained_group_with_it(tmp_path):
    """Claude Code cancels an in-flight status-line command by signalling it; the
    chained command lives in its own group, so the script must forward the kill."""
    import signal
    import time

    kid = tmp_path / "kid.pid"
    proc = subprocess.Popen(
        [sys.executable, str(_SCRIPT), "--then", f"sleep 30 & echo $! > {kid}; wait"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "GENESIS_DB_PATH": str(tmp_path / "absent.db"),
        },
    )
    proc.stdin.write(b'{"cwd": "/"}')
    proc.stdin.close()
    deadline = time.monotonic() + 30
    while not kid.exists() or not kid.read_text().strip():
        assert time.monotonic() < deadline, "chained command never started"
        time.sleep(0.05)
    pid = int(kid.read_text())
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    assert _pid_gone(pid), f"grandchild {pid} survived SIGTERM to the script"


def _git_repo(path: Path, branch: str) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    return path


def _run_script(tmp_path: Path, payload: dict) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "GENESIS_DB_PATH": str(tmp_path / "absent.db"),
    }
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _write_round(home: Path, repo: Path, branch: str, n: int) -> None:
    key = hashlib.sha256(os.path.realpath(repo).encode()).hexdigest()[:12]
    d = home / ".genesis" / "review_rounds"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.json").write_text(
        json.dumps({"branch": branch, "round": n, "lifetime": n, "last_source": "external"}),
        encoding="utf-8",
    )


def test_streak_is_the_gates_own_counter_end_to_end(tmp_path):
    """Real interpreter + real review_state: a round file the gate would read as
    2 renders as 2, and the same file written for ANOTHER branch renders as 0 —
    the branch-scoping the gate applies, not one this script re-implements."""
    repo = _git_repo(tmp_path / "r", "topic")
    _write_round(tmp_path, repo, "topic", 2)
    proc = _run_script(tmp_path, {"cwd": str(repo), "session_id": "e2e"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "topic · ledger:— · streak:2/3 · PR:—\n"

    _write_round(tmp_path, repo, "some-other-branch", 2)
    proc = _run_script(tmp_path, {"cwd": str(repo), "session_id": "e2e"})
    assert proc.stdout == "topic · ledger:— · streak:0/3 · PR:—\n"


def test_non_git_cwd_end_to_end(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = _run_script(tmp_path, {"cwd": str(plain)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "— · ledger:— · streak:— · PR:—\n"
