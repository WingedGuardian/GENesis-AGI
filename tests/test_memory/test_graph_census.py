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
    assert seen == {
        "host_pid": os.getpid(),
        "include_host": True,
        "live_held_by": os.getpid(),
        "prior": {},  # no earlier census row to carry from
    }

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


# ── PR2: every traversal producer, not just memory servers ────────────────


def _roles_census(proc_root, repo, tmp_path, procs, *, fds=None, **kw) -> dict:
    """A census over several fake processes: (pid, argv, env, started).
    ``fds`` maps a pid to the files it holds open."""
    import time as _time

    r, _, _ = repo
    live = tmp_path / "live.db"
    live.touch()
    for pid, argv, env, started in procs:
        _proc(proc_root, pid, argv, started=started or _time.time() + 60, env=env,
              exe=str(tmp_path / "sys" / "bin" / "python3.12"), fds=(fds or {}).get(pid))
    return census.build_census(r, proc_root=proc_root, live_db=live, **kw)


def _worker_argv(repo, venv_argv, *, tree=None) -> list[str]:
    r, _, _ = repo
    script = (tree or r) / "scripts" / "ambient_awareness_worker.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("")
    return [venv_argv[0], str(script), "--session-id", "s"]


def test_an_ambient_worker_on_main_is_proven_from_its_launch_env(
    proc_root, repo, tmp_path, main_venv
):
    """The worker loads no secrets file and builds no router: its launch
    environment is exactly what it runs with."""
    m = _roles_census(proc_root, repo, tmp_path, [(90, _worker_argv(repo, main_venv), {}, None)])
    [p] = m["procs"]
    assert p["role"] == "ambient"
    assert (p["source"], p["unknown_reasons"]) == ("main", [])
    assert p["telemetry_off"] is False
    assert p["telemetry"] is True


def test_an_ambient_worker_with_its_switch_on_says_so(proc_root, repo, tmp_path, main_venv):
    env = {"GENESIS_GRAPH_TELEMETRY_DISABLED": "1"}
    m = _roles_census(proc_root, repo, tmp_path, [(91, _worker_argv(repo, main_venv), env, None)])
    assert m["procs"][0]["telemetry_off"] is True


def test_an_ambient_worker_from_another_tree_is_not_main(proc_root, repo, tmp_path, main_venv):
    argv = _worker_argv(repo, main_venv, tree=tmp_path / "wt")
    m = _roles_census(proc_root, repo, tmp_path, [(92, argv, {}, None)])
    assert m["procs"][0]["unknown_reasons"] == ["script_not_main"]


def test_a_replay_is_listed_as_excluded_never_as_dirt(proc_root, repo, tmp_path, main_venv):
    """Owner ruling ba877469: replay re-runs historical turns, not live traffic."""
    r, _, _ = repo
    argv = [main_venv[0], str(r / "scripts" / "ambient_replay.py"), "--session-id", "s"]
    m = _roles_census(proc_root, repo, tmp_path, [(93, argv, {}, None)])
    [p] = m["procs"]
    assert (p["role"], p["excluded"]) == ("replay", "replay_not_live_traffic")


@pytest.mark.parametrize(
    "tail", [["-m", "genesis.channels.bridge"], ["-m", "genesis", "serve", "--port", "1"]]
)
def test_another_stack_beside_the_census_host_is_dirty(proc_root, repo, tmp_path, main_venv, tail):
    m = _roles_census(proc_root, repo, tmp_path, [(94, [main_venv[0], *tail], {}, None)])
    [p] = m["procs"]
    assert p["role"] in ("bridge", "server")
    assert p["unknown_reasons"] == ["runs_alongside_host"]


def test_the_census_host_itself_is_never_scanned_as_a_peer(proc_root, repo, tmp_path, main_venv):
    m = _roles_census(
        proc_root, repo, tmp_path,
        [(95, [main_venv[0], "-m", "genesis", "serve"], {}, None)], host_pid=95,
    )
    assert m["procs"] == []


def test_a_generic_venv_process_gets_history_and_scope_only(proc_root, repo, tmp_path, main_venv):
    """Owner ruling ba877469: history plus scope, no source or switch proof."""
    r, old, _ = repo
    old_at = _reflog_times(r)[1][0]
    bench = tmp_path / "bench.db"
    bench.write_text("")
    m = _roles_census(
        proc_root, repo, tmp_path,
        [
            (96, [main_venv[0], "-m", "pytest"], {}, old_at + 60),  # began on old code
            (97, [main_venv[0], "-"], {"GENESIS_DB_PATH": str(bench)}, None),  # scratch DB
        ],
    )
    by_pid = {p["pid"]: p for p in m["procs"]}
    assert by_pid[96]["role"] == "venv-generic"
    assert by_pid[96]["telemetry"] is False
    assert "unknown_reasons" not in by_pid[96]
    assert by_pid[97]["foreign"] is True


def test_a_process_on_another_interpreter_is_not_counted(proc_root, repo, tmp_path, main_venv):
    m = _roles_census(proc_root, repo, tmp_path, [(98, ["/usr/bin/python3", "-m", "pytest"], {}, None)])
    assert m["procs"] == []


def test_head_history_covers_the_last_two_and_a_half_hours(proc_root, repo, tmp_path, main_venv):
    """A process spawned and gone between two censuses left no row, so the
    census also checks every commit HEAD held in the window."""
    r, old, _ = repo
    m = _roles_census(proc_root, repo, tmp_path, [])
    assert m["head_history_telemetry"] is True  # the fixture's moves are months old
    _git(r, "checkout", "-q", old)  # now: HEAD holds code without telemetry
    _git(r, "checkout", "-q", "main")
    m = _roles_census(proc_root, repo, tmp_path, [])
    assert m["head_history_telemetry"] is False


def test_the_host_row_is_proven_from_where_its_modules_loaded(tmp_path):
    from genesis.env import repo_root

    real_src_root = Path(census.__file__).resolve().parents[3]
    assert census.host_source_reasons(real_src_root) == []
    assert census.host_source_reasons(tmp_path) == ["host_source_not_main"]
    assert repo_root()  # resolvable in the test env


def test_build_census_appends_the_host_row_when_asked(proc_root, repo, tmp_path, main_venv):
    import os as _os

    m = _roles_census(proc_root, repo, tmp_path, [], host_pid=_os.getpid(), include_host=True)
    [host] = m["procs"]
    assert host["pid"] == _os.getpid()
    assert host["host"] is True
    # The fixture repo is not where this test's genesis modules came from.
    assert host["unknown_reasons"] == ["host_source_not_main"]


def test_a_venv_python_started_by_name_is_found_through_its_path(
    proc_root, repo, tmp_path, main_venv
):
    """An activated venv runs `python script.py`: a relative argv0 that exec
    resolved through the process's own PATH."""
    venv_bin = str(Path(main_venv[0]).parent)
    Path(main_venv[0]).resolve().chmod(0o755)  # exec resolves only executables
    other_bin = tmp_path / "other-venv" / "bin"
    other_bin.mkdir(parents=True)
    (other_bin / "python3").write_text("")
    (other_bin / "python3").chmod(0o755)
    m = _roles_census(
        proc_root, repo, tmp_path,
        [
            (99, ["python", "job.py"], {"PATH": f"{venv_bin}:/usr/bin"}, None),
            (100, ["python3", "plugin.py"], {"PATH": f"{other_bin}:/usr/bin"}, None),
        ],
    )
    assert [(p["pid"], p["role"]) for p in m["procs"]] == [(99, "venv-generic")]


def test_a_non_python_process_naming_the_worker_is_not_a_producer(
    proc_root, repo, tmp_path, main_venv
):
    r, _, _ = repo
    worker = str(r / "scripts" / "ambient_awareness_worker.py")
    m = _roles_census(proc_root, repo, tmp_path, [(101, ["tail", "-f", worker], {}, None)])
    assert m["procs"] == []


def test_an_empty_reflog_window_means_head_held_only_head(repo):
    """No move since the window start (an empty or expired reflog) is not
    unknown: HEAD held only HEAD."""
    from genesis.memory import graph_census as gc

    reader = gc._load_serving_commit()
    r, old, new = repo
    assert gc._window_held(reader, "", 0.0, new, 0) == [new]
    reflog = (r / ".git" / "logs" / "HEAD").read_text()
    assert gc._window_held(reader, reflog, 9e12, new, 0) == [new]  # window after every move
    held = gc._window_held(reader, reflog, 0.0, new, 0)  # window before every move
    assert held[-1] == new and old in held


def test_an_unreadable_python_on_another_interpreter_never_makes_it_incomplete(
    proc_root, repo, tmp_path, main_venv
):
    """MEASURED on a live box: another user's service python with an unreadable
    environ made every census incomplete. Its absolute interpreter is not main's
    venv, so argv alone settles it."""
    m = _roles_census(
        proc_root, repo, tmp_path,
        [(102, ["/usr/local/other/bin/python", "-m", "service"], {}, None)],
    )
    (proc_root / "102" / "environ").chmod(0)
    try:
        m = census.build_census(repo[0], proc_root=proc_root, live_db=tmp_path / "live.db")
    finally:
        (proc_root / "102" / "environ").chmod(0o644)
    assert m["complete"] is True
    assert m["procs"] == []


def test_a_generic_process_holding_the_live_db_open_is_counted(
    proc_root, repo, tmp_path, main_venv
):
    """Its env names a scratch DB, but it holds the live one open: that handle
    is what it uses, so it is never foreign (the same rule as memory servers)."""
    bench = tmp_path / "bench.db"
    bench.write_text("")
    live = tmp_path / "live.db"
    live.touch()
    m = _roles_census(
        proc_root, repo, tmp_path,
        [(103, [main_venv[0], "-"], {"GENESIS_DB_PATH": str(bench)}, None)],
        fds={103: [live]},
    )
    [p] = m["procs"]
    assert p["foreign"] is False


def test_an_ambient_worker_holding_the_live_db_open_is_counted(
    proc_root, repo, tmp_path, main_venv
):
    bench = tmp_path / "bench.db"
    bench.write_text("")
    live = tmp_path / "live.db"
    live.touch()
    m = _roles_census(
        proc_root, repo, tmp_path,
        [(104, _worker_argv(repo, main_venv), {"GENESIS_DB_PATH": str(bench)}, None)],
        fds={104: [live]},
    )
    [p] = m["procs"]
    assert (p["role"], p["foreign"]) == ("ambient", False)


def _reflog_line(old: str, new: str, ts: int) -> str:
    return f"{old} {new} T <t@example.invalid> {ts} +0000\tcheckout: moving\n"


def test_a_reflog_gap_or_a_foreign_end_inside_the_window_is_unknown(repo):
    """The window is read from the moves themselves, so a move that does not
    start where the last one ended, a clock step back, or a reflog that does not
    end at HEAD cannot be trusted to list every commit HEAD held."""
    from genesis.memory import graph_census as gc

    reader = gc._load_serving_commit()
    a, b, c, d = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
    gap = _reflog_line(a, b, 100) + _reflog_line(c, d, 200)
    with pytest.raises(reader._Unknown):
        gc._window_held(reader, gap, 50.0, d, 0)
    assert gc._window_held(reader, gap, 150.0, d, 0) == [c, d]  # the gap lies before the window
    with pytest.raises(reader._Unknown):
        gc._window_held(reader, _reflog_line(a, b, 100), 50.0, c, 0)  # HEAD is not b
    backwards = _reflog_line(a, b, 200) + _reflog_line(b, c, 100)
    with pytest.raises(reader._Unknown):
        gc._window_held(reader, backwards, 150.0, c, 0)


def test_an_unreadable_history_window_is_never_clean(proc_root, repo, tmp_path, main_venv):
    import time as _time

    r, _, new = repo
    with (r / ".git" / "logs" / "HEAD").open("a") as f:
        f.write(_reflog_line(new, "f" * 40, int(_time.time())))  # no longer ends at HEAD
    m = _roles_census(proc_root, repo, tmp_path, [])
    assert m["head_history_telemetry"] is False
    assert "history_unreadable" in m["reasons"]


def test_an_ambient_worker_from_another_tree_is_never_foreign(
    proc_root, repo, tmp_path, main_venv
):
    """Its database rules model main's worker; with that code unproven, a
    scratch-DB reading is not evidence enough to skip the row."""
    bench = tmp_path / "bench.db"
    bench.write_text("")
    argv = _worker_argv(repo, main_venv, tree=tmp_path / "wt")
    m = _roles_census(proc_root, repo, tmp_path, [(105, argv, {"GENESIS_DB_PATH": str(bench)}, None)])
    [p] = m["procs"]
    assert (p["unknown_reasons"], p["foreign"]) == (["script_not_main"], False)


def test_another_users_python_is_never_read(proc_root, repo, tmp_path, main_venv, monkeypatch):
    """A bare `python` names no interpreter until its PATH is read, and another
    user's environ is not ours to read: it cannot run as this install."""
    venv_bin = str(Path(main_venv[0]).parent)
    Path(main_venv[0]).resolve().chmod(0o755)
    import time as _time

    _proc(proc_root, 106, ["python", "job.py"], started=_time.time() + 60,
          env={"PATH": f"{venv_bin}:/usr/bin"}, exe=str(tmp_path / "sys" / "bin" / "python3.12"))
    (tmp_path / "live.db").touch()
    real = census.os.getuid()
    monkeypatch.setattr(census.os, "getuid", lambda: real + 1)
    m = census.build_census(repo[0], proc_root=proc_root, live_db=tmp_path / "live.db")
    assert m["procs"] == []


def test_a_window_reaching_past_reflog_expiry_is_unknown(repo):
    """Git can expire a detour's two entries together, leaving no gap: a window
    that starts before the expiry cutoff cannot be proven complete."""
    from genesis.memory import graph_census as gc

    reader = gc._load_serving_commit()
    _, _, new = repo
    assert gc._window_held(reader, "", 100.0, new, 50) == [new]
    with pytest.raises(reader._Unknown):
        gc._window_held(reader, "", 100.0, new, 150)


def test_a_python_naming_the_worker_as_an_argument_is_not_a_worker(
    proc_root, repo, tmp_path, main_venv
):
    worker = _worker_argv(repo, main_venv)[1]
    m = _roles_census(proc_root, repo, tmp_path, [(107, [main_venv[0], "-m", "pytest", worker], {}, None)])
    assert [p["role"] for p in m["procs"]] == ["venv-generic"]


def test_a_relative_interpreter_path_is_counted_as_unknown(proc_root, repo, tmp_path, main_venv):
    """`.venv/bin/python job.py` was resolved against the cwd at exec, which the
    process may have left: it may be main's venv, so it is counted, unproven."""
    m = _roles_census(proc_root, repo, tmp_path, [(108, [".venv/bin/python", "job.py"], {}, None)])
    [p] = m["procs"]
    assert (p["role"], p["telemetry"], p["unknown"], p["foreign"]) == (
        "venv-generic", None, "interpreter_unresolved", False,
    )


def test_a_generic_process_in_another_mount_namespace_is_never_foreign(
    proc_root, repo, tmp_path, main_venv
):
    import time as _time

    bench = tmp_path / "bench.db"
    bench.write_text("")
    r, _, _ = repo
    (tmp_path / "live.db").touch()
    _proc(proc_root, 109, [main_venv[0], "-"], started=_time.time() + 60,
          env={"GENESIS_DB_PATH": str(bench)}, exe=str(tmp_path / "sys" / "bin" / "python3.12"),
          ns="mnt:[2]")
    m = census.build_census(r, proc_root=proc_root, live_db=tmp_path / "live.db")
    [p] = m["procs"]
    assert p["foreign"] is False


@pytest.mark.parametrize(
    "rel", ["src/genesis/routing/standalone.py", "scripts/genesis_mcp_server.py"]
)
def test_an_edited_loader_outside_the_memory_package_marks_head_dirty(proc_root, repo, rel):
    """The proofs model the entry scripts and loaders: an uncommitted edit there
    means a process may run code no commit records."""
    r, _, _ = repo
    f = r / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("edited = True\n")
    assert census.build_census(r, proc_root=proc_root)["head_dirty"] is True


def test_a_memory_server_with_another_genesis_home_is_unknown(proc_root, repo, tmp_path, main_venv):
    """Its graph engine socket and lost-writes file come from ITS home, so its
    traversals could be served by another engine."""
    env = {"GENESIS_GRAPH_TELEMETRY_DISABLED": "0", "GENESIS_DB_PATH": str(tmp_path / "live.db"),
           "GENESIS_HOME": str(tmp_path / "other-home")}
    p = _census_one(proc_root, repo, tmp_path, main_venv, env)
    assert "other_genesis_home" in p["unknown_reasons"]


def test_a_generic_process_with_another_genesis_home_is_unknown(proc_root, repo, tmp_path, main_venv):
    m = _roles_census(proc_root, repo, tmp_path,
                      [(110, [main_venv[0], "-"], {"GENESIS_HOME": str(tmp_path / "other-home")}, None)])
    [p] = m["procs"]
    assert (p["telemetry"], p["unknown"]) == (None, "other_genesis_home")


def test_a_generic_process_with_an_unmodelled_option_is_unknown(proc_root, repo, tmp_path, main_venv):
    """An attached module option may be a second server: never history-only."""
    m = _roles_census(proc_root, repo, tmp_path, [(111, [main_venv[0], '-mgenesis', "serve"], {}, None)])
    [p] = m["procs"]
    assert (p["telemetry"], p["unknown"]) == (None, "interpreter_options_unmodelled")


def test_a_sandboxed_process_with_its_own_home_does_not_reset_the_clock(
    proc_root, repo, tmp_path, main_venv
):
    """The test sandbox runs in another mount namespace with its own home: the
    documented residual. Reading it unknown would reset the 14-day clock on
    every census that lands during a sandboxed test run."""
    import time as _time

    r, _, _ = repo
    (tmp_path / "live.db").touch()
    _proc(proc_root, 112, [main_venv[0], "-m", "pytest"], started=_time.time() + 60,
          env={"GENESIS_HOME": str(tmp_path / "sandbox-home")},
          exe=str(tmp_path / "sys" / "bin" / "python3.12"), ns="mnt:[2]")
    [p] = census.build_census(r, proc_root=proc_root, live_db=tmp_path / "live.db")["procs"]
    assert p["unknown"] != "other_genesis_home"


def test_a_relative_path_entry_before_the_match_is_unresolved(proc_root, repo, tmp_path, main_venv):
    """`PATH=.venv/bin:/usr/bin python job.py`: exec searched the cwd at exec
    first, which may have been main's checkout, so the process is counted."""
    m = _roles_census(
        proc_root, repo, tmp_path,
        [(113, ["python", "job.py"], {"PATH": ".venv/bin:/usr/bin"}, None)],
    )
    [p] = m["procs"]
    assert (p["role"], p["unknown"]) == ("venv-generic", "interpreter_unresolved")


def test_a_detour_in_the_windows_boundary_second_is_counted(repo):
    """Reflog times are whole seconds: two moves at second 100 may both be after
    a window that starts at 100.4."""
    from genesis.memory import graph_census as gc

    reader = gc._load_serving_commit()
    a, b = "a" * 40, "b" * 40
    detour = _reflog_line(a, b, 100) + _reflog_line(b, a, 100)
    assert b in gc._window_held(reader, detour, 100.4, a, 0)
# ── carry-forward: a secrets.env edit no longer leaves every server unknown ──
# A server's switch and database come partly from secrets.env, proven only when
# the file is unchanged since the server started. Any later edit, even to an
# unrelated key, made every running server unknown and reset the cutover clock.
# Now a row records a digest of the census-relevant keys it proved; the next
# census carries a server whose file changed only in OTHER keys.

from genesis.memory import graph_census_proof as proof  # noqa: E402

_PAST = 1_700_000_100.0  # a start before any file this test writes: "changed since"


def _census_with_prior(proc_root, repo, tmp_path, argv, env, *, started, prior):
    import time as _time

    r, _, _ = repo
    live = tmp_path / "live.db"
    live.touch()
    _proc(
        proc_root,
        80,
        argv,
        started=started or _time.time() + 60,
        env=env,
        exe=str(tmp_path / "sys" / "bin" / "python3.12"),
    )
    [p] = census.build_census(r, proc_root=proc_root, live_db=live, prior=prior)["procs"]
    return p


def test_a_proven_row_records_its_digest(
    proc_root, repo, tmp_path, main_venv
):
    secrets = tmp_path / "own.env"
    secrets.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=0\n")
    env = {"SECRETS_PATH": str(secrets)}
    p = _census_one(proc_root, repo, tmp_path, main_venv, env)
    assert p["secrets_digest"] == proof.secrets_digest({"GENESIS_GRAPH_TELEMETRY_DISABLED": "0"})
    assert p["carried"] is False


def test_an_edit_to_another_key_is_carried(proc_root, repo, tmp_path, main_venv):
    secrets = tmp_path / "own.env"
    secrets.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=0\nOTHER=1\n")
    env = {"SECRETS_PATH": str(secrets)}
    digest = proof.secrets_digest({"GENESIS_GRAPH_TELEMETRY_DISABLED": "0"})
    secrets.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=0\nOTHER=2\n")
    started_at = census._iso(_PAST)
    p = _census_with_prior(
        proc_root, repo, tmp_path, main_venv, env, started=_PAST, prior={(80, started_at): digest}
    )
    assert (p["carried"], p["telemetry_off"], p["secrets_digest"]) == (True, False, digest)


def test_an_edit_to_a_census_key_is_not_carried(proc_root, repo, tmp_path, main_venv):
    secrets = tmp_path / "own.env"
    env = {"SECRETS_PATH": str(secrets)}
    digest = proof.secrets_digest({"GENESIS_GRAPH_TELEMETRY_DISABLED": "0"})
    secrets.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=1\n")
    p = _census_with_prior(
        proc_root,
        repo,
        tmp_path,
        main_venv,
        env,
        started=_PAST,
        prior={(80, census._iso(_PAST)): digest},
    )
    assert (p["carried"], p["telemetry_off"], p["secrets_digest"]) == (False, None, None)


@pytest.mark.parametrize(
    "prior",
    [
        None,  # no previous row
        {},  # a previous row without this server
        {(81, "x"): "d"},  # another pid and start
    ],
)
def test_no_matching_prior_row_means_no_carry(proc_root, repo, tmp_path, main_venv, prior):
    secrets = tmp_path / "own.env"
    secrets.write_text("OTHER=2\n")
    p = _census_with_prior(
        proc_root,
        repo,
        tmp_path,
        main_venv,
        {"SECRETS_PATH": str(secrets)},
        started=_PAST,
        prior=prior,
    )
    assert (p["carried"], p["telemetry_off"]) == (False, None)


def test_another_pid_with_the_same_start_is_not_carried(proc_root, repo, tmp_path, main_venv):
    secrets = tmp_path / "own.env"
    secrets.write_text("OTHER=2\n")
    p = _census_with_prior(
        proc_root, repo, tmp_path, main_venv, {"SECRETS_PATH": str(secrets)},
        started=_PAST, prior={(81, census._iso(_PAST)): proof.secrets_digest({})},
    )
    assert p["carried"] is False


def test_a_reused_pid_with_a_new_start_is_not_carried(proc_root, repo, tmp_path, main_venv):
    secrets = tmp_path / "own.env"
    secrets.write_text("OTHER=2\n")
    digest = proof.secrets_digest({})
    p = _census_with_prior(
        proc_root,
        repo,
        tmp_path,
        main_venv,
        {"SECRETS_PATH": str(secrets)},
        started=_PAST,
        prior={(80, census._iso(_PAST - 50)): digest},
    )
    assert p["carried"] is False


def test_a_missing_file_is_never_carried(proc_root, repo, tmp_path, main_venv):
    env = {"SECRETS_PATH": str(tmp_path / "gone.env")}
    p = _census_with_prior(
        proc_root,
        repo,
        tmp_path,
        main_venv,
        env,
        started=_PAST,
        prior={(80, census._iso(_PAST)): proof.secrets_digest({})},
    )
    assert (p["carried"], p["telemetry_off"]) == (False, None)


def test_a_symlink_swapped_after_start_is_never_carried(proc_root, repo, tmp_path, main_venv):
    """A retargeted link is a different file, not an edit of the proven one."""
    real = tmp_path / "real.env"
    real.write_text("OTHER=2\n")
    link = tmp_path / "link.env"
    link.symlink_to(real)
    p = _census_with_prior(
        proc_root,
        repo,
        tmp_path,
        main_venv,
        {"SECRETS_PATH": str(link)},
        started=_PAST,
        prior={(80, census._iso(_PAST)): proof.secrets_digest({})},
    )
    assert p["carried"] is False


@pytest.mark.parametrize("key", ["SECRETS_PATH", "GENESIS_REPO_ROOT"])
def test_a_file_that_moves_the_secrets_path_is_never_carried(
    proc_root, repo, tmp_path, main_venv, key
):
    """A lazy router rebuild would then read a file nobody proved."""
    secrets = tmp_path / "own.env"
    secrets.write_text(f"{key}={tmp_path}/elsewhere\n")
    vals = {key: f"{tmp_path}/elsewhere"}
    p = _census_with_prior(
        proc_root,
        repo,
        tmp_path,
        main_venv,
        {"SECRETS_PATH": str(secrets)},
        started=_PAST,
        prior={(80, census._iso(_PAST)): proof.secrets_digest(vals)},
    )
    assert p["carried"] is False


#: Written out rather than read from the module, so a key dropped there fails
#: here instead of silently dropping its own test case.
_CENSUS_KEYS = (
    "GENESIS_GRAPH_TELEMETRY_DISABLED",
    "GENESIS_DB_PATH",
    "GENESIS_REPO_ROOT",
    "HOME",
    "GENESIS_HOME",
    "SECRETS_PATH",
)


def test_the_digest_keys_are_every_key_the_census_reads_from_the_file():
    assert set(proof.CENSUS_SECRET_KEYS) == set(_CENSUS_KEYS)


@pytest.mark.parametrize("key", _CENSUS_KEYS)
def test_the_digest_covers_each_census_key(key):
    base = {k: "/a" for k in _CENSUS_KEYS}
    assert proof.secrets_digest(base) != proof.secrets_digest({**base, key: "/b"})
    assert proof.secrets_digest(base) != proof.secrets_digest(
        {k: v for k, v in base.items() if k != key}
    )
    assert proof.secrets_digest(base) == proof.secrets_digest({**base, "OTHER": "x"})


def test_a_carried_row_holds_no_value(proc_root, repo, tmp_path, main_venv):
    import json as _json

    secrets = tmp_path / "own.env"
    secrets.write_text(f"GENESIS_DB_PATH={tmp_path}/sekrit-db.db\nAPI_KEY_X=sekrit\n")
    vals = {"GENESIS_DB_PATH": f"{tmp_path}/sekrit-db.db"}
    p = _census_with_prior(
        proc_root,
        repo,
        tmp_path,
        main_venv,
        {"SECRETS_PATH": str(secrets)},
        started=_PAST,
        prior={(80, census._iso(_PAST)): proof.secrets_digest(vals)},
    )
    assert p["carried"] is True
    assert "sekrit" not in _json.dumps(p)


def test_prior_digests_reads_only_a_current_schema_row():
    row = {
        "v": census.CENSUS_SCHEMA,
        "procs": [
            {"pid": 1, "started_at": "s1", "secrets_digest": "d1"},
            {"pid": 2, "started_at": "s2", "secrets_digest": None},
            {"pid": 3, "started_at": "s3"},  # a row from before digests
            {"pid": "x", "started_at": "s4", "secrets_digest": "d4"},  # malformed
        ],
    }
    assert census.prior_digests(row) == {(1, "s1"): "d1"}
    assert census.prior_digests({**row, "v": census.CENSUS_SCHEMA - 1}) == {}
    assert census.prior_digests(None) == {}
    assert census.prior_digests({"v": census.CENSUS_SCHEMA, "procs": "nope"}) == {}


async def test_record_census_carries_from_the_latest_row_only(db, monkeypatch):
    """The latest row decides. One that cannot be used means no carry, never a
    reach past it to an older row that could."""
    monkeypatch.delenv("GENESIS_GRAPH_TELEMETRY_DISABLED", raising=False)
    seen: dict = {}
    monkeypatch.setattr(census, "build_census", lambda **kw: seen.update(kw) or {"procs": []})

    def row(ts: str, metrics) -> tuple:
        body = metrics if isinstance(metrics, str) else json.dumps(metrics)
        return (ts, "system", census.CENSUS_EVENT_TYPE, body)

    usable = {"v": census.CENSUS_SCHEMA, "procs": [
        {"pid": 5, "started_at": "s5", "secrets_digest": "d5"}]}
    insert = (
        "INSERT INTO eval_events (timestamp, dimension, event_type, metrics_json) "
        "VALUES (?, ?, ?, ?)"
    )
    await db.execute(insert, row("2026-01-01T00:00:00.000000Z", usable))
    await db.commit()
    await census.record_census(db)
    assert seen["prior"] == {(5, "s5"): "d5"}

    # A newer row of another schema now sits on top: it decides, and carries nothing.
    await db.execute(insert, row("2099-01-01T00:00:00.000000Z", {**usable, "v": 2}))
    await db.commit()
    await census.record_census(db)
    assert seen["prior"] == {}
def test_a_file_whose_home_moves_the_secrets_path_is_never_carried(
    proc_root, repo, tmp_path, main_venv
):
    """The server resolves `~` in its secrets path with the HOME a reload set.
    The digest matches here, so only re-deriving the path can refuse it."""
    home = tmp_path / "home"
    home.mkdir()
    secrets = home / "own.env"
    other = tmp_path / "other"
    secrets.write_text(f"HOME={other}\n")
    env = {"SECRETS_PATH": "~/own.env", "HOME": str(home)}
    p = _census_with_prior(
        proc_root,
        repo,
        tmp_path,
        main_venv,
        env,
        started=_PAST,
        prior={(80, census._iso(_PAST)): proof.secrets_digest({"HOME": str(other)})},
    )
    assert (p["carried"], p["telemetry_off"]) == (False, None)


def test_the_real_chain_carries_across_censuses(proc_root, repo, tmp_path, main_venv):
    """What one census writes, through the JSON a row is stored as, is what the
    next census carries from: no hand-built digest or key anywhere."""
    import time as _time

    r, _, _ = repo
    secrets = tmp_path / "own.env"
    secrets.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=0\nOTHER=1\n")
    start = secrets.stat().st_ctime + 0.5  # started after the file was written
    live = tmp_path / "live.db"
    live.touch()
    _proc(
        proc_root,
        80,
        main_venv,
        started=start,
        env={"SECRETS_PATH": str(secrets)},
        exe=str(tmp_path / "sys" / "bin" / "python3.12"),
    )

    def run(prior):
        m = census.build_census(r, proc_root=proc_root, live_db=live, prior=prior)
        return m, m["procs"][0]

    m1, p1 = run(None)
    assert (p1["carried"], p1["telemetry_off"]) == (False, False)
    assert p1["secrets_digest"]
    _time.sleep(max(0.0, start - _time.time()) + 0.6)
    secrets.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=0\nOTHER=2\n")  # after the start
    m2, p2 = run(census.prior_digests(json.loads(json.dumps(m1))))
    assert (p2["carried"], p2["telemetry_off"], p2["secrets_digest"]) == (
        True,
        False,
        p1["secrets_digest"],
    )
    _m3, p3 = run(census.prior_digests(json.loads(json.dumps(m2))))
    assert (p3["carried"], p3["telemetry_off"]) == (True, False)
    secrets.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=1\nOTHER=2\n")  # a census key
    _m4, p4 = run(census.prior_digests(json.loads(json.dumps(m2))))
    assert (p4["carried"], p4["telemetry_off"]) == (False, None)


async def test_a_corrupt_latest_row_never_stops_the_census(db, monkeypatch):
    """`get_events` decodes the latest row; one that will not decode must cost
    the carry, never the census row itself (it would stay latest forever)."""
    monkeypatch.delenv("GENESIS_GRAPH_TELEMETRY_DISABLED", raising=False)
    seen: dict = {}
    monkeypatch.setattr(census, "build_census", lambda **kw: seen.update(kw) or {"procs": []})
    await db.execute(
        "INSERT INTO eval_events (timestamp, dimension, event_type, metrics_json) "
        "VALUES (?, ?, ?, ?)",
        ("2099-01-01T00:00:00.000000Z", "system", census.CENSUS_EVENT_TYPE, "{"),
    )
    await db.commit()
    await census.record_census(db)
    assert seen["prior"] == {}
    cur = await db.execute(
        "SELECT COUNT(*) FROM eval_events WHERE event_type = ?", (census.CENSUS_EVENT_TYPE,)
    )
    assert (await cur.fetchone())[0] == 2
def test_a_file_naming_secrets_path_is_never_carried_even_when_it_names_itself(
    proc_root, repo, tmp_path, main_venv
):
    """The re-derived path is unchanged here, so only the key rule refuses it: a
    file that names where secrets live is not carried, whatever it names."""
    secrets = tmp_path / "own.env"
    secrets.write_text(f"SECRETS_PATH={secrets}\n")
    vals = {"SECRETS_PATH": str(secrets)}
    p = _census_with_prior(
        proc_root,
        repo,
        tmp_path,
        main_venv,
        {"SECRETS_PATH": str(secrets)},
        started=_PAST,
        prior={(80, census._iso(_PAST)): proof.secrets_digest(vals)},
    )
    assert p["carried"] is False
def _rewrite_during_read(monkeypatch, path, new_text):
    """The file changes while the census reads it: the case a digest must never
    be issued for, since the server still holds the old content. The rewrite
    lands inside the read, between its two snapshots."""
    real = Path.read_text

    def racing(self, *a, **k):
        if self == path:
            path.write_text(new_text)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", racing)


def test_an_edit_during_the_proven_read_is_unknown(monkeypatch, tmp_path):
    secrets = tmp_path / "own.env"
    secrets.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=1\n")
    start = secrets.stat().st_ctime + 0.5
    import time as _time

    _time.sleep(max(0.0, start - _time.time()) + 0.6)
    _rewrite_during_read(monkeypatch, secrets, "GENESIS_GRAPH_TELEMETRY_DISABLED=0\n")
    assert proof.secrets_values(secrets, start) is None


def test_an_edit_during_the_carry_read_is_not_read(monkeypatch, tmp_path):
    """A different size, so the snapshot sees it deterministically; a same-size
    rewrite inside one change-time tick is the stated residual, harmless because
    a carry only ever returns values matching the already-proven digest."""
    secrets = tmp_path / "own.env"
    secrets.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=1\nOTHER=1\n")
    _rewrite_during_read(monkeypatch, secrets, "GENESIS_GRAPH_TELEMETRY_DISABLED=0\nOTHER=11\n")
    assert proof.changed_secrets_values(secrets, _PAST) is None


def test_an_edit_between_the_check_and_the_read_is_unknown(monkeypatch, tmp_path):
    """The other half of the window: the edit lands after the change-time check
    but before the read starts, so the read itself sees one stable state."""
    secrets = tmp_path / "own.env"
    secrets.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=0\n")
    start = secrets.stat().st_ctime - 100  # the file now postdates the start
    monkeypatch.setattr(proof, "_unchanged_since", lambda _p, _s: True)  # the stale check
    assert proof.secrets_values(secrets, start) is None
