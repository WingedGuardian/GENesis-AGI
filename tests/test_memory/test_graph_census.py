"""The hourly memory-server census (A1b-1 of the FalkorDB cutover gate).

The cutover clock may only start once no memory server runs code older than the
traversal telemetry, and nothing else records when such a process dies. These
tests pin what one census row says: which memory servers were alive, when each
started, which commit the checkout held then, and whether that commit records
traversals.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from genesis.memory import graph_census as census

BOOT = 1_700_000_000.0
TCK = os.sysconf("SC_CLK_TCK")
MCP = ["/venv/bin/python", "/repo/scripts/genesis_mcp_server.py", "--server"]


def _proc(root: Path, pid: int, argv: list[str], *, started: float, env=None) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    d.joinpath("cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    ticks = int((started - BOOT) * TCK)
    # Fields 3.. after "(comm)": field 22 (starttime) is index 19 of the remainder.
    rest = ["S"] + ["0"] * 18 + [str(ticks)] + ["0"] * 5
    d.joinpath("stat").write_text(f"{pid} (python3) {' '.join(rest)}\n")
    env = env or {}
    d.joinpath("environ").write_bytes(b"".join(f"{k}={v}\0".encode() for k, v in env.items()))


@pytest.fixture
def proc_root(tmp_path) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    (root / "stat").write_text(f"cpu  1 2 3\nbtime {int(BOOT)}\nprocesses 9\n")
    return root


def _git(repo: Path, *args: str, when: str | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    if when:
        env["GIT_COMMITTER_DATE"] = when
    out = subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path) -> tuple[Path, str, str]:
    """A checkout whose reflog moved from a commit WITHOUT the telemetry module
    to one WITH it. Returns (repo, old_sha, new_sha)."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    # The fixture's commits are dated in the past; never-expiring keeps the
    # reader from refusing them as older than git's expiry cutoff.
    _git(r, "config", "gc.reflogExpireUnreachable", "never")
    (r / "README").write_text("x")
    _git(r, "add", "README")
    _git(r, "commit", "-q", "-m", "old", when="2026-01-01T12:00:00+00:00")
    old = _git(r, "rev-parse", "HEAD")
    mod = r / census.TELEMETRY_MODULE
    mod.parent.mkdir(parents=True)
    mod.write_text("# telemetry\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "new", when="2026-01-01T12:10:00+00:00")
    new = _git(r, "rev-parse", "HEAD")
    return r, old, new


# ── pure pieces ────────────────────────────────────────────────────────────


def test_has_telemetry_reads_the_commit_and_caches(repo):
    r, old, new = repo
    cache: dict = {}
    assert census.has_telemetry(r, new, cache) is True
    assert census.has_telemetry(r, old, cache) is False
    assert census.has_telemetry(r, "0" * 40, cache) is None
    assert cache == {new: True, old: False, "0" * 40: None}


# ── the /proc scan ─────────────────────────────────────────────────────────


def test_scan_finds_mcp_servers_and_ignores_processes_that_only_name_the_file(proc_root):
    _proc(proc_root, 10, [*MCP, "memory"], started=BOOT + 100)
    _proc(
        proc_root,
        11,
        ["/venv/bin/python", "/repo/scripts/genesis_mcp_server.py", "--server=health"],
        started=BOOT + 50,
    )
    _proc(proc_root, 12, ["vim", "/repo/scripts/genesis_mcp_server.py"], started=BOOT + 1)
    _proc(
        proc_root,
        13,
        ["bash", "-c", "pgrep -f genesis_mcp_server.py --server memory"],
        started=BOOT + 1,
    )

    found, complete = census.scan_mcp_servers(proc_root)

    assert complete is True
    assert [(p["pid"], p["server"]) for p in found] == [(10, "memory"), (11, "health")]
    assert found[0]["start"] == pytest.approx(BOOT + 100, abs=1 / TCK)


def test_scan_reads_the_kill_switch_and_db_path_from_each_process(proc_root):
    _proc(
        proc_root,
        20,
        [*MCP, "memory"],
        started=BOOT + 5,
        env={"GENESIS_GRAPH_TELEMETRY_DISABLED": "1", "GENESIS_DB_PATH": "/x/bench.db"},
    )

    [p], _ = census.scan_mcp_servers(proc_root)

    assert p["telemetry_off"] is True
    assert p["db_path"] == "/x/bench.db"


def test_a_process_that_vanished_mid_scan_is_skipped_not_incomplete(proc_root):
    _proc(proc_root, 30, [*MCP, "memory"], started=BOOT + 5)
    (proc_root / "30" / "stat").unlink()
    (proc_root / "30" / "cmdline").unlink()
    (proc_root / "30" / "environ").unlink()

    found, complete = census.scan_mcp_servers(proc_root)

    assert found == []
    assert complete is True


def test_a_live_server_whose_start_cannot_be_read_makes_the_scan_incomplete(proc_root):
    _proc(proc_root, 31, [*MCP, "memory"], started=BOOT + 5)
    (proc_root / "31" / "stat").write_text("garbage")

    found, complete = census.scan_mcp_servers(proc_root)

    assert found == []
    assert complete is False


def test_no_btime_means_nothing_can_be_trusted(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "stat").write_text("cpu 1 2 3\n")
    assert census.scan_mcp_servers(root) == ([], False)


# ── one census row ─────────────────────────────────────────────────────────


def _reflog_times(repo_dir: Path) -> list[tuple[float, str]]:
    """(unix time, new commit) per HEAD move, newest first."""
    lines = (repo_dir / ".git" / "logs" / "HEAD").read_text().splitlines()
    moves = [(float(line.split("\t")[0].split()[-2]), line.split()[1]) for line in lines]
    return list(reversed(moves))


def test_build_census_labels_each_memory_server_by_the_commit_it_started_on(
    proc_root, repo, tmp_path
):
    r, old, new = repo
    [(new_at, new_sha), (old_at, old_sha)] = _reflog_times(r)[:2]
    assert (new_sha, old_sha) == (new, old)
    live_db = tmp_path / "live.db"
    assert new_at - old_at == 600  # reflog times follow the committer dates
    _proc(proc_root, 40, [*MCP, "memory"], started=old_at + 60)  # before the move
    _proc(
        proc_root,
        41,
        [*MCP, "memory"],
        started=new_at + 5,  # after it
        env={"GENESIS_DB_PATH": str(live_db)},
    )
    _proc(
        proc_root,
        42,
        [*MCP, "memory"],
        started=new_at + 6,
        env={"GENESIS_DB_PATH": str(tmp_path / "bench.db")},
    )
    _proc(proc_root, 43, [*MCP, "recon"], started=new_at + 7)
    _proc(
        proc_root,
        44,
        ["/venv/bin/python", "/repo/scripts/genesis_mcp_server.py"],
        started=new_at + 8,
    )

    m = census.build_census(r, proc_root=proc_root, live_db=live_db)

    by_pid = {p["pid"]: p for p in m["procs"]}
    assert set(by_pid) == {40, 41, 42}
    # Started on the old commit: it may still run old code, whatever HEAD holds now.
    assert (by_pid[40]["commit"], by_pid[40]["held"], by_pid[40]["telemetry"]) == (
        old,
        [old, new],
        False,
    )
    assert (by_pid[41]["commit"], by_pid[41]["held"], by_pid[41]["telemetry"]) == (
        new,
        [new],
        True,
    )
    assert by_pid[41]["unknown"] is None
    assert by_pid[41]["foreign"] is False
    assert by_pid[42]["foreign"] is True
    assert by_pid[41]["started_at"].endswith("Z")
    assert m["other_servers"] == {"recon": 1}
    assert m["unclassified"] == 1
    assert m["head_telemetry"] is True
    assert m["head_dirty"] is False
    assert m["complete"] is True
    assert m["reasons"] == []
    assert m["v"] == census.CENSUS_SCHEMA


def test_a_server_older_than_the_reflog_has_no_commit_and_no_verdict(proc_root, repo):
    r, _, _ = repo
    oldest = _reflog_times(r)[-1][0]
    _proc(proc_root, 50, [*MCP, "memory"], started=oldest - 3600)

    [p] = census.build_census(r, proc_root=proc_root)["procs"]

    assert p["commit"] is None
    assert p["telemetry"] is None
    assert "starts after" in p["unknown"]


def test_a_server_started_in_the_second_of_a_move_is_unknown_not_guessed(proc_root, repo):
    r, _, _ = repo
    new_at = _reflog_times(r)[0][0]
    _proc(proc_root, 51, [*MCP, "memory"], started=new_at + 0.4)

    [p] = census.build_census(r, proc_root=proc_root)["procs"]

    assert (p["commit"], p["telemetry"]) == (None, None)
    assert "same second" in p["unknown"]


def test_a_later_move_to_older_code_makes_a_new_server_unclean(proc_root, repo):
    """Modules imported after start load whatever is on disk then, so a server
    that started on the telemetry commit but saw HEAD go back to older code may
    be running some of it."""
    r, old, new = repo
    new_at = _reflog_times(r)[0][0]
    _proc(proc_root, 52, [*MCP, "memory"], started=new_at + 60)
    _git(r, "checkout", "-q", old, when="2026-01-01T12:20:00+00:00")
    _git(r, "checkout", "-q", "main", when="2026-01-01T12:21:00+00:00")

    [p] = census.build_census(r, proc_root=proc_root)["procs"]

    assert p["held"] == [new, old, new]
    assert p["telemetry"] is False


def test_a_relative_db_path_is_never_called_foreign(proc_root, repo, tmp_path):
    r, _, new = repo
    _proc(proc_root, 53, [*MCP, "memory"], started=_reflog_times(r)[0][0] + 60,
          env={"GENESIS_DB_PATH": "data/elsewhere.db"})
    [p] = census.build_census(r, proc_root=proc_root, live_db=tmp_path / "live.db")["procs"]
    assert p["foreign"] is False


def test_git_calls_never_take_the_index_lock(monkeypatch, tmp_path):
    seen: list[tuple] = []

    def _fake(repo, *args, timeout):
        seen.append(args)
        return 0, "", ""

    import genesis.observability.git_health as gh

    monkeypatch.setattr(gh, "_run_git", _fake)
    census._git(tmp_path, "status", "--porcelain")
    assert seen == [("--no-optional-locks", "status", "--porcelain")]


@pytest.mark.parametrize(("skew", "disagrees"), [(0.2, False), (-1.9, False), (2.5, True)])
def test_btime_is_cross_checked_against_the_boottime_clock(monkeypatch, skew, disagrees):
    monkeypatch.setattr(census.time, "time", lambda: 1_000_000.0)
    monkeypatch.setattr(census.time, "clock_gettime", lambda _clk: 100.0)
    assert census._boot_disagrees(999_900.0 + skew) is disagrees


def test_a_dirty_memory_tree_and_a_missing_reflog_are_reported(proc_root, tmp_path):
    r = tmp_path / "bare"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")  # no commits: no HEAD, no reflog
    m = census.build_census(r, proc_root=proc_root)
    assert m["complete"] is False
    assert "reflog_unreadable" in m["reasons"]
    assert "head_unreadable" in m["reasons"]


def test_an_edited_memory_package_marks_head_dirty(proc_root, repo):
    r, _, _ = repo
    (r / census.TELEMETRY_MODULE).write_text("# edited\n")
    assert census.build_census(r, proc_root=proc_root)["head_dirty"] is True


# ── the scheduled job ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_census_writes_one_row(db, monkeypatch):
    monkeypatch.delenv("GENESIS_GRAPH_TELEMETRY_DISABLED", raising=False)
    monkeypatch.setattr(census, "build_census", lambda: {"procs": [], "complete": True})

    await census.record_census(db)

    cur = await db.execute(
        "SELECT dimension, metrics_json FROM eval_events WHERE event_type = ?",
        (census.CENSUS_EVENT_TYPE,),
    )
    rows = await cur.fetchall()
    assert [(r[0], json.loads(r[1])) for r in rows] == [("system", {"procs": [], "complete": True})]


def test_an_unloadable_history_reader_leaves_every_server_without_a_verdict(
    proc_root, repo, monkeypatch
):
    r, _, _ = repo
    _proc(proc_root, 54, [*MCP, "memory"], started=_reflog_times(r)[0][0] + 60)
    monkeypatch.setattr(census, "_load_serving_commit", lambda: None)

    m = census.build_census(r, proc_root=proc_root)

    assert m["complete"] is False
    assert "reflog_unreadable" in m["reasons"]
    assert m["procs"][0]["telemetry"] is None


@pytest.mark.asyncio
async def test_with_the_kill_switch_on_the_row_says_so_instead_of_going_silent(db):
    # The suite runs with GENESIS_GRAPH_TELEMETRY_DISABLED=1 (tests/conftest.py).
    assert os.environ.get("GENESIS_GRAPH_TELEMETRY_DISABLED") == "1"

    metrics = await census.record_census(db)

    assert metrics == {
        "v": census.CENSUS_SCHEMA,
        "procs": [],
        "complete": False,
        "reasons": ["telemetry_disabled"],
    }


@pytest.mark.asyncio
async def test_the_job_is_registered_hourly_at_45_and_reports_its_health(db, monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    calls: list[tuple[str, str]] = []

    class _RT:
        _db = db

        def record_job_success(self, name):
            calls.append(("ok", name))

        def record_job_failure(self, name, exc=None):
            calls.append(("fail", name))

    async def _boom(_db):
        raise RuntimeError("no /proc")

    sched = AsyncIOScheduler()
    census._wire_graph_census(sched, _RT())
    job = sched.get_job("graph_traverse_census")
    assert isinstance(job.trigger, CronTrigger)
    assert str(job.trigger.fields[job.trigger.FIELD_NAMES.index("minute")]) == "45"

    sched.start(paused=True)
    try:
        await job.func()
        monkeypatch.setattr(census, "record_census", _boom)
        await job.func()
    finally:
        sched.shutdown(wait=False)

    assert calls == [("ok", "graph_traverse_census"), ("fail", "graph_traverse_census")]


def test_an_unreadable_cmdline_of_our_own_process_makes_the_scan_incomplete(proc_root):
    _proc(proc_root, 60, [*MCP, "memory"], started=BOOT + 5)
    (proc_root / "60" / "cmdline").chmod(0)
    try:
        found, complete = census.scan_mcp_servers(proc_root)
    finally:
        (proc_root / "60" / "cmdline").chmod(0o644)
    assert found == []
    assert complete is False
