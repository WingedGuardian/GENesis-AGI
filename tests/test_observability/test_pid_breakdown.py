"""Tests for pid_breakdown — who holds the PID/task budget when it runs low.

Everything runs against a fake ``/proc`` and a fake cgroup tree under
``tmp_path``; ``proc_io._PROC`` is repointed too, because ``read_stat`` and
``read_starttime`` read through it, and ``_CGROUP_ROOT`` so the /proc scan for
processes missing from ``cgroup.procs`` resolves against the fake tree.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from genesis.observability import pid_breakdown as pb
from genesis.util import proc_io


def _stat(pid: int, comm: str, ppid: int, threads: int, starttime: int = 100) -> str:
    # Fields after "(comm) ": state(3) ppid(4) ... num_threads(20) ... starttime(22).
    after = ["S", str(ppid)] + ["0"] * 15 + [str(threads), "0", str(starttime)] + ["0"] * 5
    return f"{pid} ({comm}) " + " ".join(after) + "\n"


class Fake:
    def __init__(self, tmp_path, monkeypatch):
        self.proc = tmp_path / "proc"
        self.cg = tmp_path / "cg"
        self.proc.mkdir()
        monkeypatch.setattr(pb, "_PROC", str(self.proc))
        monkeypatch.setattr(proc_io, "_PROC", str(self.proc))
        monkeypatch.setattr(pb, "_CGROUP_ROOT", str(tmp_path))

    def process(self, pid, comm, ppid, threads, argv, *, cgroup="/cg/session.scope", start=100):
        d = self.proc / str(pid)
        d.mkdir()
        (d / "stat").write_text(_stat(pid, comm, ppid, threads, start))
        (d / "cmdline").write_bytes(b"\x00".join(a.encode() for a in argv) + b"\x00")
        (d / "cgroup").write_text(f"0::{cgroup}\n")

    def cgroup(self, rel, tasks, procs=()):
        d = self.cg / rel if rel else self.cg
        d.mkdir(parents=True, exist_ok=True)
        (d / "pids.current").write_text(f"{tasks}\n")
        (d / "cgroup.procs").write_text("".join(f"{p}\n" for p in procs))
        return d


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    return Fake(tmp_path, monkeypatch)


def _budget(fake, current=2300):
    return {
        "status": "error",
        "scope": "user-1000.slice",
        "path": str(fake.cg),
        "current": current,
        "max": 2400,
    }


def test_the_night_shape_names_the_claude_tree(fake):
    """The 2026-10-13 shape: one session scope holding a claude whose MCP children
    carry most of the threads, next to an unrelated small tree. The claude tree
    must come first and its children's threads must be counted in it."""
    fake.cgroup("", 640)
    fake.cgroup("session-5.scope", 620, procs=[10, 11, 12, 13, 14])
    fake.cgroup("user@1000.service/app.slice/other.service", 20, procs=[30])
    fake.process(10, "tmux: server", 1, 1, ["tmux"])
    fake.process(11, "bash", 10, 1, ["/bin/bash"])
    fake.process(12, "claude", 11, 40, ["claude", "--resume", "secret-token-abc"])
    fake.process(13, "node", 12, 300, ["node", "/opt/mcp/server.js", "--key=SECRET"])
    fake.process(14, "python3", 12, 278, ["/v/bin/python3", "-m", "genesis.mcp.health"])
    fake.process(30, "sleep", 1, 1, ["sleep", "100"])

    snap = pb.take_snapshot(_budget(fake))

    top = snap["trees"][0]
    assert top["root_pid"] == 12
    assert top["label"] == "claude"
    assert top["threads"] == 40 + 300 + 278
    assert top["processes"] == 3
    assert top["unit"] == "session.scope"
    assert [m["pid"] for m in top["top"]] == [13, 14, 12]
    labels = {m["label"] for m in top["top"]}
    assert labels == {"node server.js", "python3 -m genesis.mcp.health", "claude"}
    # The tmux server and the shell under it are their own tree, not the claude's.
    assert {t["root_pid"] for t in snap["trees"]} == {10, 12, 30}
    assert snap["threads_seen"] == 1 + 1 + 40 + 300 + 278 + 1
    assert snap["top_cgroups"][0]["cgroup"] == "session-5.scope"
    # No argument other than a module or script name ever reaches the record.
    text = json.dumps(snap) + pb.summarize(snap)
    assert "secret-token-abc" not in text and "SECRET" not in text


def test_summary_names_trees_cgroups_and_coverage(fake):
    fake.cgroup("", 5)
    fake.cgroup("a.scope", 5, procs=[12])
    fake.process(12, "claude", 1, 5, ["claude"])
    text = pb.summarize(pb.take_snapshot(_budget(fake, current=7)))
    assert "claude (pid 12, session.scope): 5 tasks in 1 procs" in text
    assert "a.scope: 5" in text
    assert "Counted 5 of 7 tasks" in text


def test_a_pid_that_vanishes_is_counted_not_hidden(fake):
    fake.cgroup("", 3)
    fake.cgroup("a.scope", 3, procs=[12, 99])  # 99 has no /proc entry
    fake.process(12, "claude", 1, 2, ["claude"])
    snap = pb.take_snapshot(_budget(fake))
    assert snap["vanished"] == 1
    assert snap["processes_seen"] == 1


def test_a_ppid_cycle_terminates(fake):
    fake.cgroup("", 2, procs=[20, 21])
    fake.process(20, "a", 21, 1, ["a"])
    fake.process(21, "b", 20, 1, ["b"])
    snap = pb.take_snapshot(_budget(fake))
    assert sum(t["processes"] for t in snap["trees"]) == 2


def test_an_unreadable_stat_and_a_zombie(fake):
    fake.cgroup("", 2, procs=[40, 41])
    fake.process(40, "x", 1, 3, ["x"])
    (fake.proc / "40" / "stat").write_text("garbage")
    fake.process(41, "defunct", 1, 0, [])  # zombie: no argv, 0 threads
    snap = pb.take_snapshot(_budget(fake))
    assert snap["vanished"] == 1
    zombie = snap["trees"][0]
    assert zombie["threads"] == 1  # a zombie still holds a pid
    assert zombie["label"] == "[defunct]"


def test_an_unreadable_cgroup_is_counted(fake):
    fake.cgroup("", 1)
    d = fake.cgroup("a.scope", 1)
    (d / "cgroup.procs").unlink()
    snap = pb.take_snapshot(_budget(fake))
    assert snap["cgroups_unreadable"] == 1


def test_deep_cgroups_stop_listing_but_their_pids_still_count(fake):
    fake.cgroup("", 9)
    fake.cgroup("a/b/c/d/e/f", 9, procs=[50])
    fake.process(50, "deep", 1, 9, ["deep"])
    snap = pb.take_snapshot(_budget(fake))
    assert max(n["depth"] for n in snap["cgroups"]) == pb._CGROUP_DEPTH
    assert snap["threads_seen"] == 9
    assert snap["top_cgroups"][0]["cgroup"] == "a/b/c/d"


def test_empty_subtree(fake):
    fake.cgroup("", 0)
    snap = pb.take_snapshot(_budget(fake))
    assert snap["trees"] == []
    assert "none readable" in pb.summarize(snap)


def test_no_path_is_no_snapshot(fake):
    assert pb.take_snapshot({"status": "error"}) is None


@pytest.mark.parametrize(
    ("argv", "label"),
    [
        (["/usr/bin/node", "--max-old-space-size=4096", "/a/b/cli.js"], "node cli.js"),
        (["python3.12", "-m", "genesis"], "python3.12 -m genesis"),
        (["bash", "-c", "curl -H 'Authorization: x' http://h"], "bash -c"),
        (["uvx", "--from", "git+https://u:tok@host/r", "tool"], "uvx"),
        (["python3", "https://evil/x.py"], "python3"),
        (["/opt/bin/serena-mcp", "--token", "abc"], "serena-mcp"),
        (["/usr/bin/redis-server unixsocket:/home/u/x.sock"], "redis-server"),
        (["python3", "-m", "x:tok@h"], "python3 -m"),
        (["node", "--inspect-port", "9229", "x.js"], "node"),
        (["python", "-W", "ignore", "x.py"], "python"),
        (["python", "ghp_abcdef123"], "python"),
        (["tok:abc@host"], "[x]"),
    ],
)
def test_labels_never_carry_an_argument_value(fake, argv, label):
    fake.process(60, "x", 1, 1, argv)
    assert pb.process_label(60) == label


def test_snapshot_for_writes_prunes_and_obeys_the_off_switch(fake, tmp_path, monkeypatch):
    home = tmp_path / "gh"
    monkeypatch.setenv("GENESIS_HOME", str(home))
    monkeypatch.delenv(pb._DISABLE_ENV, raising=False)
    fake.cgroup("", 1, procs=[12])
    fake.process(12, "claude", 1, 1, ["claude"])
    snaps = home / "logs" / "pid_snapshots"
    snaps.mkdir(parents=True)
    old = snaps / "old.json"
    old.write_text("{}")
    os.utime(old, (time.time() - 8 * 86400,) * 2)

    out = pb.snapshot_for(_budget(fake))

    assert out is not None and "claude" in out["text"]
    written = json.loads(Path(out["file"]).read_text())
    assert written["trees"][0]["root_pid"] == 12
    assert not old.exists()

    monkeypatch.setenv(pb._DISABLE_ENV, "1")
    assert pb.snapshot_for(_budget(fake)) is None


def test_the_size_cap_drops_the_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "_RETENTION_BYTES", 10)
    now = time.time()
    for i, name in enumerate(["a.json", "b.json", "c.json"]):
        p = tmp_path / name
        p.write_text("12345")
        os.utime(p, (now - 100 + i,) * 2)
    pb._prune(tmp_path, now)
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["b.json", "c.json"]


def test_snapshot_for_never_raises(fake, monkeypatch):
    monkeypatch.delenv(pb._DISABLE_ENV, raising=False)

    def boom(_budget):
        raise RuntimeError("x")

    monkeypatch.setattr(pb, "take_snapshot", boom)
    assert pb.snapshot_for(_budget(fake)) is None


def test_each_service_is_its_own_tree_not_the_managers(fake):
    """Every user service's parent is the user's systemd manager. Without a unit
    boundary they would all be counted as one 'systemd' tree (measured live: 220
    tasks in one lump)."""
    fake.cgroup("", 60, procs=[2, 3, 4])
    fake.process(2, "systemd", 1, 1, ["/lib/systemd/systemd", "--user"], cgroup="/cg/init.scope")
    fake.process(3, "python3", 2, 50, ["python", "-m", "genesis"], cgroup="/cg/app/server.service")
    fake.process(4, "redis", 2, 9, ["redis-server"], cgroup="/cg/app/graph.service")
    trees = {t["root_pid"]: t for t in pb.take_snapshot(_budget(fake))["trees"]}
    assert set(trees) == {2, 3, 4}
    assert trees[3]["threads"] == 50
    assert trees[3]["unit"] == "server.service"
    assert trees[3]["label"] == "python -m genesis"


def test_a_fork_storm_names_what_the_processes_are(fake):
    """Many one-thread processes tie in "top"; by_comm and the zombie count say
    what they are, and the summary carries it."""
    # As the kernel does: zombies are absent from cgroup.procs (no live thread)
    # while pids.current still counts them; only their /proc/<pid>/cgroup says
    # where they belong.
    fake.cgroup("", 12, procs=list(range(100, 109)))
    fake.process(100, "claude", 1, 1, ["claude"], cgroup="/cg")
    for pid in range(101, 109):
        fake.process(pid, "git", 100, 1, ["git", "status"], cgroup="/cg")
    for pid in range(109, 112):
        fake.process(pid, "defunct", 100, 0, [], cgroup="/cg")
        stat = (fake.proc / str(pid) / "stat").read_text().replace(") S ", ") Z ", 1)
        (fake.proc / str(pid) / "stat").write_text(stat)
    snap = pb.take_snapshot(_budget(fake))
    tree = snap["trees"][0]
    assert tree["by_comm"][0] == ("git", 8)
    assert tree["zombies"] == 3
    assert "mostly git x8" in pb.summarize(snap)
    assert "3 zombies" in pb.summarize(snap)


def test_a_reused_pid_gets_no_label(fake, monkeypatch):
    fake.cgroup("", 1, procs=[12])
    fake.process(12, "claude", 1, 1, ["claude"], start=100)
    real_take = proc_io.read_starttime
    monkeypatch.setattr(proc_io, "read_starttime", lambda pid: 999 if pid == 12 else real_take(pid))
    assert pb.take_snapshot(_budget(fake))["trees"][0]["label"] == "(exited)"


def test_a_failed_write_leaves_no_partial_file(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "gh"))
    monkeypatch.delenv(pb._DISABLE_ENV, raising=False)
    fake.cgroup("", 1, procs=[12])
    fake.process(12, "claude", 1, 1, ["claude"])

    def boom(*_a):
        raise OSError("ENOSPC")

    monkeypatch.setattr(pb.os, "replace", boom)
    assert pb.snapshot_for(_budget(fake)) is None
    assert list((tmp_path / "gh" / "logs" / "pid_snapshots").iterdir()) == []


def test_read_stat_fields():
    """proc_io.read_stat against this process's own real stat (read-only)."""
    comm, state, ppid, threads, start = proc_io.read_stat(os.getpid())
    assert ppid == os.getppid()
    assert threads >= 1
    assert start == proc_io.read_starttime(os.getpid())
    assert state in "RSDTt"


def test_mostly_only_when_a_name_repeats(fake):
    fake.cgroup("", 3, procs=[12, 13, 14])
    fake.process(12, "claude", 1, 1, ["claude"])
    fake.process(13, "a", 12, 1, ["a"])
    fake.process(14, "b", 12, 1, ["b"])
    assert "mostly" not in pb.summarize(pb.take_snapshot(_budget(fake)))


def test_a_non_utf8_process_name_is_counted(fake):
    fake.cgroup("", 1, procs=[12])
    fake.process(12, "x", 1, 4, ["x"])
    (fake.proc / "12" / "stat").write_bytes(
        _stat(12, "\udcff", 1, 4).encode("utf-8", "surrogateescape")
    )
    snap = pb.take_snapshot(_budget(fake))
    assert snap["vanished"] == 0
    assert snap["threads_seen"] == 4


@pytest.mark.parametrize(
    ("argv", "label"),
    [
        (["", "example-private-value"], "[x]"),
        (["", "", "example-private-value"], "[x]"),
        (["python3", "-m", "", "example_private_module"], "python3 -m"),
        (["python3", "-m", "", "", "example_private_module"], "python3 -m"),
        (["node", "", "example-private.js"], "node"),
        (["bash", "", "", "example-private.sh"], "bash"),
        (["python3", "-m", "genesis", ""], "python3 -m genesis"),
        (["node", "server.js", ""], "node server.js"),
        ([""], "[x]"),
    ],
)
def test_empty_argument_slots_never_promote_later_values(fake, argv, label):
    fake.process(60, "x", 1, 1, argv)
    assert pb.process_label(60) == label


@pytest.mark.parametrize("membership", ["/other.scope", "/cg-sibling", None])
def test_listed_pids_with_outside_or_unknown_current_membership_are_excluded(fake, membership):
    fake.cgroup("", 8, procs=[10, 20])
    fake.process(10, "inside", 1, 1, ["inside"], cgroup="/cg")
    fake.process(20, "moved", 1, 7, ["moved"], cgroup=membership or "/cg")
    if membership is None:
        (fake.proc / "20" / "cgroup").unlink()
    snap = pb.take_snapshot(_budget(fake))
    assert [t["root_pid"] for t in snap["trees"]] == [10]
    assert snap["threads_seen"] == snap["processes_seen"] == 1
    assert snap["excluded"] == 1
    assert snap["vanished"] == 0


@pytest.mark.parametrize("replacement", [None, 999])
def test_a_pid_that_exits_or_is_reused_during_membership_read_is_not_counted(
    fake, monkeypatch, replacement
):
    fake.cgroup("", 7, procs=[20])
    fake.process(20, "old", 1, 7, ["old"], cgroup="/cg")
    real_read = pb._read_cgroup
    monkeypatch.setattr(pb, "_pids_by_cgroup_prefix", lambda prefix: {20})

    def churn(pid):
        path = real_read(pid)
        stat = fake.proc / str(pid) / "stat"
        if replacement is None:
            stat.unlink(missing_ok=True)
        else:
            stat.write_text(_stat(pid, "new", 1, 99, replacement))
        return path

    monkeypatch.setattr(pb, "_read_cgroup", churn)
    snap = pb.take_snapshot(_budget(fake))
    assert snap["trees"] == []
    assert snap["threads_seen"] == snap["processes_seen"] == 0
    assert snap["vanished"] == (1 if replacement is None else 0)
    assert snap["excluded"] == (0 if replacement is None else 1)


@pytest.mark.parametrize("member", [b"/cg/byte-\xff.scope", b"/cg/trailing-space ", b"/other/\xff"])
def test_cgroup_paths_preserve_filesystem_bytes_and_whitespace(fake, member):
    fake.cgroup("", 4)
    fake.process(12, "x", 1, 4, ["x"])
    (fake.proc / "12" / "cgroup").write_bytes(b"0::" + member + b"\n")
    assert pb._read_cgroup(12) == os.fsdecode(member)
    snap = pb.take_snapshot(_budget(fake))
    expected = 0 if member.startswith(b"/other/") else 4
    assert snap["threads_seen"] == expected
    # Native JSON remains round-trippable; alert text carries escaped path bytes.
    restored = json.loads(json.dumps(snap))
    if expected:
        assert os.fsencode(restored["trees"][0]["unit"]) == member.rsplit(b"/", 1)[-1]
    else:
        assert restored["trees"] == []
    pb.summarize(snap).encode("utf-8")


def test_a_binding_leaf_is_itself_a_readable_top_cgroup(fake):
    fake.cgroup("", 3)
    snap = pb.take_snapshot(_budget(fake, current=3))
    assert [(n["cgroup"], n["tasks"]) for n in snap["top_cgroups"]] == [(".", 3)]
    assert "Top cgroups: .: 3" in pb.summarize(snap)


@pytest.mark.parametrize("cap", [1, 10_000])
def test_a_pruned_new_snapshot_retains_summary_without_a_dangling_reference(
    fake, tmp_path, monkeypatch, cap
):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "gh"))
    monkeypatch.delenv(pb._DISABLE_ENV, raising=False)
    monkeypatch.setattr(pb, "_RETENTION_BYTES", cap)
    fake.cgroup("", 1, procs=[12])
    fake.process(12, "claude", 1, 1, ["claude"])
    result = pb.snapshot_for(_budget(fake))
    assert result is not None and "claude" in result["text"]
    files = list((tmp_path / "gh" / "logs" / "pid_snapshots").glob("*.json"))
    assert sum(p.stat().st_size for p in files) <= cap
    if cap == 1:
        assert result["file"] is None and files == []
    else:
        assert Path(result["file"]).is_file()


def test_snapshot_publication_uses_private_atomic_file_permissions(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "gh"))
    monkeypatch.delenv(pb._DISABLE_ENV, raising=False)
    fake.cgroup("", 1)
    result = pb.snapshot_for(_budget(fake))
    assert result is not None
    assert Path(result["file"]).stat().st_mode & 0o777 == 0o600
