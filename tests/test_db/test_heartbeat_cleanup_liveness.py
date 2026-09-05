"""cleanup_stale must not delete the row of a session that is merely IDLE.

The roster's liveness spine renders IDLE-FOR as a duration — but the original
GC deleted any row not updated for 10 minutes, so an alive session sitting
untouched in a terminal lost its row before it could ever render as "idle
25m". The owner's correction that forced this: IDLE IS NOT DEAD. The GC now
deletes only rows it can show are gone: pid observed dead, no pid at all
(legacy rows keep the old window), or a 24h updated_at backstop.
"""

from datetime import UTC, datetime, timedelta

from genesis.db.crud import session_heartbeats


async def _seed(db, sid, *, minutes_ago, pid=None, pid_started_at=None):
    await session_heartbeats.upsert(
        db, cc_session_id=sid, pid=pid, pid_started_at=pid_started_at
    )
    stale = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    await db.execute(
        "UPDATE session_heartbeats SET updated_at = ? WHERE cc_session_id = ?",
        (stale, sid),
    )
    await db.commit()


async def _ids(db) -> set[str]:
    cur = await db.execute("SELECT cc_session_id FROM session_heartbeats")
    return {r[0] for r in await cur.fetchall()}


def _fake_proc(tmp_path, pids):
    proc = tmp_path / "proc"
    for pid in pids:
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "comm").write_text("claude\n")
    return str(proc)


async def test_alive_idle_row_survives_dead_row_deleted(db, tmp_path):
    proc = _fake_proc(tmp_path, [4242])
    await _seed(db, "alive-idle", minutes_ago=25, pid=4242)
    await _seed(db, "dead-proc", minutes_ago=25, pid=9999)  # not in fake proc

    deleted = await session_heartbeats.cleanup_stale(db, proc_root=proc)

    ids = await _ids(db)
    assert "alive-idle" in ids, (
        "an ALIVE session's row was GC'd for being idle — idle is not dead"
    )
    assert "dead-proc" not in ids
    assert deleted == 1


async def test_pidless_row_keeps_old_window(db, tmp_path):
    proc = _fake_proc(tmp_path, [])
    await _seed(db, "legacy-fresh", minutes_ago=5)
    await _seed(db, "legacy-stale", minutes_ago=25)

    await session_heartbeats.cleanup_stale(db, proc_root=proc)

    ids = await _ids(db)
    assert "legacy-fresh" in ids
    assert "legacy-stale" not in ids, "pid-less rows keep the 10-min window"


async def test_24h_backstop_deletes_even_alive_pid(db, tmp_path):
    """A row nothing has touched for 24h is bounded out even when its pid is
    alive — the backstop that keeps a wedged writer from leaking rows forever."""
    proc = _fake_proc(tmp_path, [4242])
    await _seed(db, "ancient", minutes_ago=60 * 25, pid=4242)

    await session_heartbeats.cleanup_stale(db, proc_root=proc)

    assert "ancient" not in await _ids(db)


async def test_wrong_comm_counts_as_dead(db, tmp_path):
    """pid recycled by a non-claude process = the session is gone."""
    proc = tmp_path / "proc" / "7777"
    proc.mkdir(parents=True)
    (proc / "comm").write_text("python3\n")
    await _seed(db, "recycled", minutes_ago=25, pid=7777)

    await session_heartbeats.cleanup_stale(db, proc_root=str(tmp_path / "proc"))

    assert "recycled" not in await _ids(db)


async def test_hostile_comm_bytes_do_not_stall_gc(db, tmp_path):
    """A single undecodable comm must not abort the whole GC scan (the raise
    fired BEFORE any delete, so even the 24h backstop stopped — review)."""
    proc = tmp_path / "proc" / "6666"
    proc.mkdir(parents=True)
    (proc / "comm").write_bytes(b"\xff\xfe\n")
    await _seed(db, "hostile-comm", minutes_ago=25, pid=6666)
    await _seed(db, "plain-dead", minutes_ago=25, pid=9999)

    deleted = await session_heartbeats.cleanup_stale(
        db, proc_root=str(tmp_path / "proc")
    )
    assert deleted == 2, "both rows are dead; neither may stall the other"
