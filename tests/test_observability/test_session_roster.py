"""observe() turns a stored identity row into observed liveness — at read time.

The spine: ALIVE (/proc + comm + start-time match), REACHABLE (alive AND the
cc-socks socket path exists), IDLE-FOR (a duration, never a judgement). Stored
liveness is stale by construction, so none of these are ever written back.
"""

import os
import stat
import time
from datetime import UTC, datetime, timedelta

from genesis.observability.session_roster import observe

_BTIME = 1_700_000_000
_TICKS = 5_000_000  # starttime ticks; epoch = _BTIME + _TICKS / SC_CLK_TCK


def _start_epoch() -> float:
    import os as _os

    return _BTIME + _TICKS / _os.sysconf("SC_CLK_TCK")


def _proc(tmp_path, pid, comm="claude", ticks=_TICKS):
    """Fake /proc with comm AND stat + btime, so the recycled-pid rejection
    (the reason pid_started_at exists) is actually exercised — the first
    fixture wrote no stat file, the verification block short-circuited in
    every test, and deleting the mechanism passed the suite (review find)."""
    d = tmp_path / "proc" / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    if isinstance(comm, bytes):
        (d / "comm").write_bytes(comm + b"\n")
    else:
        (d / "comm").write_text(comm + "\n")
    # After ") ": state ppid pgrp sid tty(5 entries, fields 3-7), 14 zeros
    # (fields 8-21)... no: reader takes idx 19 after the paren split = overall
    # field 22 = starttime. 5 + 14 = idx 0-18, ticks at idx 19. Verified
    # against the reader's rsplit(")",1)[1].split()[19].
    (d / "stat").write_text(f"{pid} ({comm}) S 1 0 0 0 " + "0 " * 14 + f"{ticks} 0")
    (tmp_path / "proc" / "stat").write_text(f"cpu 0 0 0\nbtime {_BTIME}\n")
    return str(tmp_path / "proc")


def _row(pid=None, pid_started_at=None, updated_ago_s=0):
    return {
        "cc_session_id": "abc12345-x",
        "pid": pid,
        "pid_started_at": pid_started_at,
        "updated_at": (
            datetime.now(UTC) - timedelta(seconds=updated_ago_s)
        ).isoformat(),
    }


def test_alive_reachable_and_idle(tmp_path):
    proc = _proc(tmp_path, 4242)
    socks = tmp_path / "socks"
    socks.mkdir()
    os.mknod(socks / "4242.sock", stat.S_IFSOCK | 0o600)

    out = observe(
        _row(pid=4242, updated_ago_s=300),
        proc_root=proc,
        sock_dir=str(socks),
        activity_dir=str(tmp_path / "noactivity"),
    )
    assert out["alive"] is True
    assert out["reachable"] is True
    assert out["liveness"] == "live"
    assert 290 <= out["idle_s"] <= 310


def test_alive_unlinked_socket_is_no_sock(tmp_path):
    proc = _proc(tmp_path, 4242)
    out = observe(
        _row(pid=4242),
        proc_root=proc,
        sock_dir=str(tmp_path / "emptysocks"),
        activity_dir=str(tmp_path / "noactivity"),
    )
    assert out["alive"] is True
    assert out["reachable"] is False
    assert out["liveness"] == "live-no-sock"


def test_dead_pid_is_gone_and_recycled_comm_is_gone(tmp_path):
    proc = str(tmp_path / "proc")
    out = observe(
        _row(pid=9999),
        proc_root=proc,
        sock_dir=str(tmp_path),
        activity_dir=str(tmp_path),
    )
    assert out["alive"] is False and out["liveness"] == "gone"

    proc2 = _proc(tmp_path, 7777, comm="python3")
    out2 = observe(
        _row(pid=7777),
        proc_root=proc2,
        sock_dir=str(tmp_path),
        activity_dir=str(tmp_path),
    )
    assert out2["alive"] is False and out2["liveness"] == "gone"


def test_no_pid_is_unknown(tmp_path):
    out = observe(
        _row(pid=None),
        proc_root=str(tmp_path),
        sock_dir=str(tmp_path),
        activity_dir=str(tmp_path),
    )
    assert out["alive"] is None
    assert out["liveness"] == "unknown"


def test_activity_marker_sharpens_idle(tmp_path):
    """The unthrottled activity marker beats the 60s-granular updated_at."""
    proc = _proc(tmp_path, 4242)
    act = tmp_path / "activity"
    act.mkdir()
    marker = act / "4242"
    marker.write_text("")
    fresh = time.time() - 5
    os.utime(marker, (fresh, fresh))

    out = observe(
        _row(pid=4242, updated_ago_s=120),
        proc_root=proc,
        sock_dir=str(tmp_path),
        activity_dir=str(act),
    )
    assert out["idle_s"] <= 30, "marker mtime (5s ago) must beat updated_at (120s)"


def test_matching_start_time_verifies(tmp_path):
    from datetime import UTC, datetime

    proc = _proc(tmp_path, 4242)
    stored = datetime.fromtimestamp(_start_epoch(), tz=UTC).isoformat(
        timespec="seconds"
    )
    out = observe(
        _row(pid=4242, pid_started_at=stored),
        proc_root=proc,
        sock_dir=str(tmp_path),
        activity_dir=str(tmp_path),
    )
    assert out["alive"] is True
    assert out["alive_verified"] is True


def test_recycled_pid_is_rejected(tmp_path):
    """THE reason pid_started_at exists: same pid, different process start =
    a stranger's process, never this session — renders gone, not live."""
    from datetime import UTC, datetime

    proc = _proc(tmp_path, 4242)
    way_off = datetime.fromtimestamp(_start_epoch() - 3600, tz=UTC).isoformat(
        timespec="seconds"
    )
    out = observe(
        _row(pid=4242, pid_started_at=way_off),
        proc_root=proc,
        sock_dir=str(tmp_path),
        activity_dir=str(tmp_path),
    )
    assert out["alive"] is False
    assert out["liveness"] == "gone"


def test_hostile_comm_bytes_never_raise(tmp_path):
    """comm content is attacker-influenced (prctl arbitrary bytes) and
    read_text() raises UnicodeDecodeError past an OSError catch — one such
    pid must degrade to gone, never empty the roster (probe-confirmed)."""
    proc = _proc(tmp_path, 6666, comm=b"\xff\xfe\x00evil")
    out = observe(
        _row(pid=6666),
        proc_root=proc,
        sock_dir=str(tmp_path),
        activity_dir=str(tmp_path),
    )
    assert out["alive"] is False
    assert out["liveness"] == "gone"


def test_malformed_row_never_raises(tmp_path):
    """observe()'s contract is never-raise WITHOUT a caller net: the PR-2
    MCP/dashboard consumers have no blanket catch (security review). A
    non-int pid string and a non-string stamp must degrade, not explode."""
    proc = _proc(tmp_path, 4242)
    out = observe(
        {
            "cc_session_id": "x",
            "pid": 4242,
            "pid_started_at": 12345,  # non-string: fromisoformat TypeError
            "updated_at": "not-a-timestamp",
        },
        proc_root=proc,
        sock_dir=str(tmp_path),
        activity_dir=str(tmp_path),
    )
    assert out["liveness"] in {"live", "live-no-sock"}  # alive, unverified
    assert out["alive_verified"] is False
    assert out["idle_s"] is None
