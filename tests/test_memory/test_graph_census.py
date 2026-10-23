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


def _proc(
    root: Path, pid: int, argv: list[str], *, started: float, env=None, exe=None,
    ns="mnt:[1]",
    fds=None,
) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    d.joinpath("cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    ticks = int((started - BOOT) * TCK)
    # Fields 3.. after "(comm)": field 22 (starttime) is index 19 of the remainder.
    rest = ["S"] + ["0"] * 18 + [str(ticks)] + ["0"] * 5
    d.joinpath("stat").write_text(f"{pid} (python3) {' '.join(rest)}\n")
    env = env or {}
    d.joinpath("environ").write_bytes(b"".join(f"{k}={v}\0".encode() for k, v in env.items()))
    (d / "exe").symlink_to(exe or "/usr/bin/python3.12")
    (d / "ns").mkdir()
    (d / "ns" / "mnt").symlink_to(ns)
    (d / "fd").mkdir()
    for i, target in enumerate(fds or (), start=3):
        (d / "fd" / str(i)).symlink_to(target)


@pytest.fixture
def proc_root(tmp_path) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    (root / "stat").write_text(f"cpu  1 2 3\nbtime {int(BOOT)}\nprocesses 9\n")
    (root / "self" / "ns").mkdir(parents=True)
    (root / "self" / "ns" / "mnt").symlink_to("mnt:[1]")
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

    assert p["env_switch"] == "1"
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
    live_db.write_text("")
    (tmp_path / "bench.db").write_text("")
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
    # A scratch DB never excuses code that is not proven main's (this argv is
    # not main's venv), so the row is counted, not skipped.
    assert by_pid[42]["foreign"] is False
    assert by_pid[42]["unknown_reasons"] == ["not_main_venv"]
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
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"procs": [], "complete": True}

    monkeypatch.setattr(census, "build_census", fake)

    await census.record_census(db)
    # The live database is the one THIS process's connection holds open.
    assert seen == {"live_held_by": os.getpid()}

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


@pytest.mark.asyncio
async def test_a_kill_switch_added_to_secrets_env_later_is_seen(db, monkeypatch, tmp_path):
    """genesis-server reads secrets.env once, at start; memory servers read it at
    their own start. A switch added in between silences new memory servers, so
    the census reads the file itself each hour."""
    monkeypatch.delenv("GENESIS_GRAPH_TELEMETRY_DISABLED", raising=False)
    secrets = tmp_path / "secrets.env"
    monkeypatch.setenv("SECRETS_PATH", str(secrets))
    monkeypatch.setattr(census, "build_census", lambda **kw: {"v": 1, "procs": [], "complete": True})

    secrets.write_text("OTHER=1\n")
    assert (await census.record_census(db))["complete"] is True

    secrets.write_text("OTHER=1\nGENESIS_GRAPH_TELEMETRY_DISABLED=1\n")
    metrics = await census.record_census(db)
    assert metrics["reasons"] == ["telemetry_disabled"]
    assert metrics["complete"] is False


@pytest.fixture
def main_venv(repo, tmp_path) -> list[str]:
    """Make the fixture checkout a provable main tree (a venv shaped like a live
    install's) and return a memory server's argv on it."""
    r, _, _ = repo
    interp = tmp_path / "sys" / "bin" / "python3.12"
    interp.parent.mkdir(parents=True)
    interp.write_text("")
    venv = r / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(interp)
    (venv / "pyvenv.cfg").write_text(
        f"home = {interp.parent}\ninclude-system-site-packages = false\n"
        f"version = 3.12.3\nexecutable = {interp}\n"
    )
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (r / "src" / "genesis").mkdir(parents=True, exist_ok=True)
    (site / "__editable__.genesis.pth").write_text(f"{r / 'src'}\n")
    (r / "scripts").mkdir(exist_ok=True)
    (r / "scripts" / "genesis_mcp_server.py").write_text("")
    return [str(venv / "bin" / "python"), str(r / "scripts" / "genesis_mcp_server.py"),
            "--server", "memory"]


def _census_one(proc_root, repo, tmp_path, argv, env, *, started=None, secrets=True) -> dict:
    """One server's row. By default it gets a proven, empty secrets file of its
    own: since the router reloads that file with override=True, an unprovable
    one leaves even an exec-time switch unknown."""
    import time as _time

    r, _, _ = repo
    if secrets and "SECRETS_PATH" not in env:
        own = tmp_path / "proven.env"
        own.touch()
        env = {**env, "SECRETS_PATH": str(own)}
    live = tmp_path / "live.db"
    live.touch()
    _proc(proc_root, 80, argv, started=started or _time.time() + 60, env=env,
          exe=str(tmp_path / "sys" / "bin" / "python3.12"))
    [p] = census.build_census(r, proc_root=proc_root, live_db=live)["procs"]
    return p


def test_a_fully_proven_server_reads_clean_on_all_four(proc_root, repo, tmp_path, main_venv):
    live = tmp_path / "live.db"
    env = {"GENESIS_GRAPH_TELEMETRY_DISABLED": "0", "GENESIS_DB_PATH": str(live)}
    p = _census_one(proc_root, repo, tmp_path, main_venv, env)
    assert p["role"] == "mcp-memory"
    assert (p["source"], p["unknown_reasons"]) == ("main", [])
    assert p["telemetry"] is True
    assert p["telemetry_off"] is False
    assert p["foreign"] is False
    assert p["db_identity"] == [live.stat().st_dev, live.stat().st_ino]


def test_a_server_on_another_venv_is_source_unknown(proc_root, repo, tmp_path, main_venv):
    argv = ["/venv/bin/python", main_venv[1], "--server", "memory"]
    p = _census_one(proc_root, repo, tmp_path, argv, {"GENESIS_GRAPH_TELEMETRY_DISABLED": "0"})
    assert (p["source"], p["unknown_reasons"]) == (None, ["not_main_venv"])


def test_a_missing_secrets_file_leaves_the_switch_unknown(proc_root, repo, tmp_path, main_venv):
    p = _census_one(proc_root, repo, tmp_path, main_venv, {}, secrets=False)
    assert p["telemetry_off"] is None


def test_a_db_named_only_in_the_secrets_file_never_makes_a_server_foreign(
    proc_root, repo, tmp_path, main_venv
):
    """The primary DB is fixed at import, before any secrets load, so a file's
    GENESIS_DB_PATH only ADDS a database: the live default is still in use."""
    bench = tmp_path / "bench.db"
    bench.write_text("")
    secrets = tmp_path / "own.env"
    secrets.write_text(f"GENESIS_DB_PATH={bench}\n")
    p = _census_one(proc_root, repo, tmp_path, main_venv, {"SECRETS_PATH": str(secrets)})
    assert p["telemetry_off"] is False
    assert p["foreign"] is False


def test_a_bench_server_whose_file_agrees_is_foreign(proc_root, repo, tmp_path, main_venv):
    bench = tmp_path / "bench.db"
    bench.write_text("")
    secrets = tmp_path / "own.env"
    secrets.write_text(f"GENESIS_DB_PATH={bench}\n")
    env = {"SECRETS_PATH": str(secrets), "GENESIS_DB_PATH": str(bench)}
    assert _census_one(proc_root, repo, tmp_path, main_venv, env)["foreign"] is True


def test_a_scratch_db_does_not_excuse_unproven_code(proc_root, repo, tmp_path, main_venv):
    """The DB rules model main's own server code; with it unproven, the row is
    counted (and dirty), never skipped as foreign."""
    bench = tmp_path / "bench.db"
    bench.write_text("")
    argv = ["/venv/bin/python", main_venv[1], "--server", "memory"]
    env = {"GENESIS_DB_PATH": str(bench), "GENESIS_GRAPH_TELEMETRY_DISABLED": "0"}
    p = _census_one(proc_root, repo, tmp_path, argv, env)
    assert p["unknown_reasons"] == ["not_main_venv"]
    assert p["foreign"] is False


def test_a_hardlink_of_the_live_db_is_not_foreign(proc_root, repo, tmp_path, main_venv):
    live = tmp_path / "live.db"
    live.write_text("")
    alias = tmp_path / "alias.db"
    os.link(live, alias)
    env = {"GENESIS_GRAPH_TELEMETRY_DISABLED": "0", "GENESIS_DB_PATH": str(alias)}
    assert _census_one(proc_root, repo, tmp_path, main_venv, env)["foreign"] is False


def test_a_missing_scratch_db_is_counted_not_skipped(proc_root, repo, tmp_path, main_venv):
    env = {"GENESIS_DB_PATH": str(tmp_path / "never-created.db")}
    assert _census_one(proc_root, repo, tmp_path, main_venv, env)["foreign"] is False


def test_a_tilde_db_path_expands_with_that_processs_home(proc_root, repo, tmp_path, main_venv):
    (tmp_path / "bench.db").write_text("")
    env = {"GENESIS_DB_PATH": "~/bench.db", "HOME": str(tmp_path)}
    assert _census_one(proc_root, repo, tmp_path, main_venv, env)["foreign"] is True
    # Without its HOME the census cannot expand it: never called foreign.
    proc_root2 = tmp_path / "proc2"
    proc_root2.mkdir()
    (proc_root2 / "stat").write_text((proc_root / "stat").read_text())
    (proc_root2 / "self" / "ns").mkdir(parents=True)
    (proc_root2 / "self" / "ns" / "mnt").symlink_to("mnt:[1]")
    p = _census_one(proc_root2, repo, tmp_path, main_venv, {"GENESIS_DB_PATH": "~/bench.db"})
    assert p["foreign"] is False


def test_a_row_never_carries_another_processs_environment(proc_root, repo, tmp_path, main_venv):
    import json as _json

    env = {
        "API_KEY_X": "sekrit-key",
        "SECRETS_PATH": "/sekrit-dir/s.env",
        "GENESIS_DB_PATH": "/sekrit-db/x.db",
        "PYTHONPATH": "/sekrit-pp",
        "HOME": "/sekrit-home",
    }
    row = _census_one(proc_root, repo, tmp_path, main_venv, env)
    assert "sekrit" not in _json.dumps(row)


def test_a_server_running_another_trees_script_is_not_main(proc_root, repo, tmp_path, main_venv):
    """run-mcp-server can start a worktree's script on main's venv: the package
    is main's, but the script's own loader and startup are not."""
    other = tmp_path / "wt" / "scripts" / "genesis_mcp_server.py"
    other.parent.mkdir(parents=True)
    other.write_text("")
    argv = [main_venv[0], str(other), "--server", "memory"]
    p = _census_one(proc_root, repo, tmp_path, argv, {"GENESIS_GRAPH_TELEMETRY_DISABLED": "0"})
    assert p["unknown_reasons"] == ["script_not_main"]



def test_the_default_secrets_file_is_main_checkouts(proc_root, repo, tmp_path, main_venv):
    """No SECRETS_PATH and no GENESIS_REPO_ROOT: the server loads main's own
    secrets.env, which the census proves by its ctime against the start."""
    r, _, _ = repo
    (r / "secrets.env").write_text("OTHER=1\n")
    env = {"GENESIS_DB_PATH": str(tmp_path / "live.db")}
    p = _census_one(proc_root, repo, tmp_path, main_venv, env, secrets=False)
    assert p["telemetry_off"] is False
    # Changed after the server started (a fresh /proc with an earlier start): unknown.
    proc_root2 = tmp_path / "proc2"
    proc_root2.mkdir()
    (proc_root2 / "stat").write_text((proc_root / "stat").read_text())
    (proc_root2 / "self" / "ns").mkdir(parents=True)
    (proc_root2 / "self" / "ns" / "mnt").symlink_to("mnt:[1]")
    p2 = _census_one(
        proc_root2, repo, tmp_path, main_venv, env, secrets=False, started=1_700_000_100.0
    )
    assert p2["telemetry_off"] is None



def test_a_bench_server_holding_the_live_db_open_is_counted(proc_root, repo, tmp_path, main_venv):
    """Its path names a scratch DB, but its open descriptors include the live
    one: that handle is what its long-lived connection uses."""
    import time as _time

    r, _, _ = repo
    live = tmp_path / "live.db"
    live.write_text("")
    bench = tmp_path / "bench.db"
    bench.write_text("")
    own = tmp_path / "proven.env"
    own.touch()
    env = {"GENESIS_DB_PATH": str(bench), "SECRETS_PATH": str(own)}
    _proc(proc_root, 81, main_venv, started=_time.time() + 60, env=env,
          exe=str(tmp_path / "sys" / "bin" / "python3.12"), fds=[live])
    [p] = census.build_census(r, proc_root=proc_root, live_db=live)["procs"]
    assert p["foreign"] is False


def test_the_census_names_the_live_db_its_writer_holds_open(proc_root, repo, tmp_path):
    """The row is written through the host's open connection: if the path now
    names another file (a symlink retargeted since it opened), the census
    cannot say which database "live" is and reads incomplete."""
    import time as _time

    r, _, _ = repo
    live = tmp_path / "live.db"
    live.write_text("")
    other = tmp_path / "other.db"
    other.write_text("")
    _proc(proc_root, 70, ["sleep", "1"], started=_time.time() - 5, fds=[live])
    _proc(proc_root, 71, ["sleep", "1"], started=_time.time() - 5, fds=[other])
    held = census.build_census(r, proc_root=proc_root, live_db=live, live_held_by=70)
    assert "live_db_moved" not in held["reasons"]
    moved = census.build_census(r, proc_root=proc_root, live_db=live, live_held_by=71)
    assert (moved["complete"], "live_db_moved" in moved["reasons"]) == (False, True)
    gone = census.build_census(r, proc_root=proc_root, live_db=live, live_held_by=99999)
    assert "live_db_moved" in gone["reasons"]
