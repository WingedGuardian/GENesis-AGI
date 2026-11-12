"""scripts/lib/live_move_repair.py: Genesis heals a half-written `live` checkout (#3492 PR D1).

A `git switch` killed partway (SIGKILL, power loss) leaves HEAD where it was and
the files half moved; update_state_guard.py calls that MIXED_TREE. The repair
puts the checkout back toward the rollback tag, saving every node it changes
first, and reports anything that was not the update's. Every test runs real git
against scratch repositories (real switches, killed by a smudge filter or a
reference-transaction hook), with HOME pointed at a scratch directory. The kit
bootstrap installs (PR D2) is stubbed by copying scripts/lib's files into the
scratch HOME's ~/.genesis/lib/live-repair/current.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_scripts._live_repair_world import (
    GUARD,
    LIB,
    MODULE,
    REPO,
    TAG_FILES,
    TO_FILES,
    Killed,
    World,
    _aged,
    _ancestor_file,
    _deep_desc,
    _dir_becomes_file,
    _foreign_both_flags,
    _foreign_dir_symlink_inside_dir,
    _foreign_edit,
    _foreign_fifo_inside_dir,
    _foreign_gitlink,
    _foreign_inside_dir,
    _foreign_skip_worktree,
    _foreign_staged,
    _foreign_symlink,
    _foreign_unmerged,
    _foreign_untracked,
    _gc,
    _git,
    _git_env,
    _gitlink_desc,
    _hash,
    _killed_mid_repair,
    _lock_aside,
    _lock_aside_if_any,
    _retarget,
    _skip_worktree_desc,
    _staged_desc,
    age_locks,
    assert_healed,
    foreign_rows,
    killed_rollback,
    killed_tag_side,
    restore,
    restore_all,
    tag_snapshot,
    update_bytes,
)

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + bash")


# ── T-D1, T-D2: real killed switches, both directions ───────────────────────


# A smudge filter can only kill on a path the switch ADDS: on a path both trees
# have, git runs the (missing) clean side first and refuses before writing
# anything (MEASURED, git 2.43). Paths are written in index order after the
# deletions, so AGENTS.md is the first added, Z3 a middle one, Z5 the last
# before sub/ (sub/s1 then stays unwritten); "prepared" is the whole tree.
@pytest.mark.parametrize("victim", ["AGENTS.md", "Z3", "Z5", "prepared"])
def test_td1_a_switch_killed_on_the_tag_side_is_healed_and_saved(w, victim):
    """T-D1/T-D2: killed on its first, a middle and its last file, and at the
    reference transaction (the whole target tree written): healed to the tag,
    every changed node saved first, the stale index.lock kept in the backup."""
    killed_tag_side(w, victim)
    assert w.guard_rc() == 4, "control: the kill left a mixed tree"
    lock_left = (w.root / ".git" / "index.lock").exists()
    before = w.snapshot()
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert_healed(w, before)
    assert _git(w.root, "rev-parse", "HEAD") == w.tag
    assert (w.backups()[-1] / "git" / "index.lock").exists() is lock_left
    assert foreign_rows(w) == [], "the update's own files were reported as foreign"
    assert w.hook_calls() == [], "restore-hooks runs only when `live` moved back"
    assert w.report()["hooks"] is None


# Back toward the tag the switch adds O first, P in the middle and sub/t last;
# "prepared" is the whole tag tree written, with HEAD.lock and live's lock left.
@pytest.mark.parametrize("victim", ["O", "P", "sub/t", "prepared"])
def test_td2_a_rollback_killed_partway_finishes_toward_the_tag(w, victim):
    """T-D2 (D1f): HEAD on `live` at the target, the rollback's switch back
    killed partway: the repair finishes that rollback. The files go back to the
    tag, `live` moves from the target to the tag, and restore-hooks runs from
    the checkout with update.lock's fd, as update.sh's rollback runs it."""
    killed_rollback(w, victim)
    assert w.guard_rc(w.row("done")) == 4, "control: the kill left a mixed tree"
    before = w.snapshot()
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert_healed(w, before)
    assert _git(w.root, "rev-parse", "HEAD") == w.tag
    assert _git(w.root, "symbolic-ref", "HEAD") == "refs/heads/live"
    assert _git(w.root, "rev-parse", "live@{1}") == w.to, "`live` was not moved from the target"
    assert w.hook_calls() == [
        {
            "argv": ["restore-hooks", w.to, w.tag],
            "fd_is_update_lock": True,
            "held": True,
            "ephemeral_re": True,
            "checkout_locked": True,
        }
    ]
    assert w.report()["hooks"] == "ok"
    assert foreign_rows(w) == [], "the rollback's own files were reported as foreign"


def test_td2_a_detached_head_at_the_target_is_never_finished(w, mod):
    """The forward case is HEAD on `live` at the target, as the guard's live
    rule has it. Detached there (the guard made to say mixed regardless), the
    repair changes nothing: moving `live` would leave HEAD where it is."""
    killed_rollback(w, "P")
    _git(w.root, "update-ref", "--no-deref", "HEAD", w.to)
    before = w.snapshot()
    repair = mod.Repair(str(w.state), str(w.root), False, 10)
    repair.guard.state_verdict = lambda *a, **k: (repair.guard.MIXED_TREE, "forced")
    assert repair.run() == 1
    assert w.snapshot() == before and w.backups() == []
    assert _git(w.root, "rev-parse", "live") == w.to and w.hook_calls() == []


def test_td2_an_interrupted_write_toward_the_tag_is_the_rollbacks(w):
    """T-D4, forward: the rollback was writing the TAG's bytes, so a prefix of
    them (O, which only the tag has) is the rollback's own, never foreign."""
    killed_rollback(w, "P")
    (w.root / "O").write_text("o")
    before = w.snapshot()
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert_healed(w, before)
    assert {row["path"]: row["class"] for row in w.report()["paths"]}["O"] == "engine"
    assert foreign_rows(w) == []


# ── T-D3: foreign paths are saved, healed and reported ───────────────────────


@pytest.mark.parametrize(
    "make",
    [
        _foreign_edit,
        _foreign_untracked,
        _foreign_inside_dir,
        _foreign_staged,
        _foreign_unmerged,
        _foreign_skip_worktree,
        _foreign_gitlink,
        _foreign_both_flags,
        _foreign_symlink,
        _foreign_dir_symlink_inside_dir,
        _foreign_fifo_inside_dir,
    ],
)
def test_td3_a_foreign_path_is_saved_healed_and_reported(w, make):
    """T-D3: something the update did not write, on a path the switch writes.
    The repair saves it, heals the tree anyway, lists the path as foreign, and
    the restore it prints puts the node and its index entries back exactly,
    whatever the node's kind."""
    killed_tag_side(w, "Z3")
    # The edit is made with the killed switch's stale index.lock moved aside
    # (git refuses an index write while it stands), then put back.
    lock = w.root / ".git" / "index.lock"
    lock.rename(lock.with_name("held"))
    path = make(w)
    lock.with_name("held").rename(lock)
    node_before = w.snapshot().get(path)
    sub_before = {k: v for k, v in w.snapshot().items()
                  if k.startswith(path + "/") and not update_bytes(w, k, v)}  # fmt: skip
    index_before = _git(w.root, "ls-files", "-s", "--", path)
    flags_before = _git(w.root, "ls-files", "-v", "--", path)
    before = w.snapshot()
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert_healed(w, before)
    rows = {row["path"]: row for row in w.report()["paths"]}
    assert rows[path]["class"] == "foreign", rows[path]
    assert len(foreign_rows(w)) == 1, foreign_rows(w)
    ts = w.report()["ts"]
    assert w.report()["reapply"] == [], "the writer stored a shell step (#3868)"
    assert f"scripts/deploy_candidates repair-move --restore {ts} --check" in r.stdout, r.stdout
    assert w.alerts() and all(f"repair-move --restore {ts}" in a["body"] for a in w.alerts())
    assert "rm -rf" not in r.stdout and not any("rm -rf" in a["body"] for a in w.alerts())
    assert all(a["severity"] == "critical" for a in w.alerts())
    restore_all(w)
    after = w.snapshot()
    assert after.get(path) == node_before, "the restore did not put the node back"
    assert {k: v for k, v in after.items() if k.startswith(path + "/")} == sub_before
    assert _git(w.root, "ls-files", "-s", "--", path) == index_before
    assert _git(w.root, "ls-files", "-v", "--", path) == flags_before


def test_r1_a_foreign_staged_blob_survives_gc_and_is_reapplied(w):
    """Codex c4385619761: staged content that differs from the worktree's is
    only in the object store; once the repair drops its index entry a
    `git gc --prune=now` deletes it. The backup holds the blob's bytes and the
    restore writes the object back before the index entry."""
    killed_tag_side(w, "Z3")

    def stage():
        (w.root / "A").write_text("staged by someone\n")
        _git(w.root, "add", "A")
        (w.root / "A").write_text("then edited again\n")

    _lock_aside(w, stage)
    index_before = _git(w.root, "ls-files", "-s", "--", "A")
    oid = index_before.split()[1]
    assert w.repair().returncode == 0
    _git(w.root, "gc", "-q", "--prune=now")
    assert (
        subprocess.run(["git", "-C", str(w.root), "cat-file", "-e", oid], env=_git_env()).returncode
        != 0
    ), "control: gc pruned the staged blob"
    restore_all(w)
    assert _git(w.root, "ls-files", "-s", "--", "A") == index_before
    assert _git(w.root, "cat-file", "blob", oid) == "staged by someone"
    assert (w.root / "A").read_text() == "then edited again\n"


def test_r1_a_name_with_a_newline_reapplies_its_index_entry(tmp_path, monkeypatch):
    """Codex c4385619771 / Devin c4385592698: index records are NUL-delimited, so
    a path with a newline neither breaks the restore nor feeds git an extra
    record."""
    name = "odd\nname"
    monkeypatch.setitem(TO_FILES, name, "theirs\n")
    w = World(tmp_path)
    killed_tag_side(w, "Z3")  # odd\nname sorts after Z3: never written

    def stage():
        (w.root / name).write_text("mine\n")
        _git(w.root, "add", "--", name)

    _lock_aside(w, stage)
    index_before = _git(w.root, "ls-files", "-s", "-z")
    assert w.repair().returncode == 0
    rows = {row["path"]: row for row in w.report()["paths"]}
    assert rows[name]["class"] == "foreign"
    restore_all(w)
    assert _git(w.root, "ls-files", "-s", "-z") == index_before
    assert (w.root / name).read_text() == "mine\n"


@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_r1_a_fifo_or_socket_on_a_move_path_is_recorded(w, monkeypatch, kind):
    """Devin c4385592015: a node that holds no bytes is still recorded (type
    and mode) and reported as foreign; the restore makes a FIFO again, and
    refuses a socket by name (its process binds it again)."""
    import socket as socketlib

    killed_tag_side(w, "Z3")  # Z5 is a target-only path, unwritten
    keep = None
    if kind == "fifo":
        os.mkfifo(w.root / "Z5", 0o640)
    else:
        monkeypatch.chdir(w.root)  # a unix socket path is capped near 108 bytes
        keep = socketlib.socket(socketlib.AF_UNIX)
        keep.bind("Z5")
    try:
        r = w.repair()
    finally:
        if keep is not None:
            keep.close()
    assert r.returncode == 0, r.stdout + r.stderr
    row = {row["path"]: row for row in w.report()["paths"]}["Z5"]
    assert row["class"] == "foreign", row
    assert row["special"]["type"] == kind, row
    if kind == "fifo":
        assert row["special"]["mode"] == "0640", row
    assert len(foreign_rows(w)) == 1
    assert f"--restore {w.report()['ts']}" in r.stdout and "mkfifo" not in r.stdout, r.stdout
    put = restore(w)
    import stat as st

    if kind == "fifo":
        assert put.returncode == 0, put.stdout + put.stderr
        mode = os.lstat(w.root / "Z5").st_mode
        assert st.S_ISFIFO(mode) and st.S_IMODE(mode) == 0o640
    else:
        assert put.returncode == 1 and "other:cannot-make" in put.stdout, put.stdout
        assert not (w.root / "Z5").exists()


def test_r1_the_backup_is_durable_in_its_parents_before_the_checkout_changes(w, mod, monkeypatch):
    """Devin c4385592132: the new backup's entry in deploy-backups (and that
    directory's own entry, when the run created it) is fsynced before _apply."""
    killed_tag_side(w, "Z3")
    events: list[tuple[str, str]] = []
    real_fsync, real_apply = mod._fsync_dir, mod.Repair._apply

    def fsync(path):
        events.append(("fsync", os.path.abspath(path)))
        return real_fsync(path)

    def apply(self, *a, **kw):
        events.append(("apply", ""))
        return real_apply(self, *a, **kw)

    monkeypatch.setattr(mod, "_fsync_dir", fsync)
    monkeypatch.setattr(mod.Repair, "_apply", apply)
    assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 0
    before_apply = events[: events.index(("apply", ""))]
    backup = str(w.backups()[-1])
    synced = [p for _, p in before_apply]
    assert backup in synced
    assert str(w.gen / "deploy-backups") in synced, "the backup's entry was never synced"
    assert str(w.gen) in synced, "deploy-backups' own entry was never synced"
    # the parents are synced after the report is written into the backup
    assert synced.index(str(w.gen / "deploy-backups")) > synced.index(backup)


def test_r1_a_backup_that_fails_at_boot_leaves_the_server_to_start(w):
    """Devin c4385592320: a backup that cannot be written (a full disk, a
    permission) changed nothing in the checkout, so --boot exits 0."""
    if os.geteuid() == 0:
        pytest.skip("running as root: the permission cannot be refused")
    killed_tag_side(w, "Z3")
    root = w.gen / "deploy-backups"
    root.mkdir()
    root.chmod(0o500)
    before = w.snapshot()
    try:
        r = w.repair(mode="--boot")
    finally:
        root.chmod(0o755)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing was changed" in r.stderr, r.stderr
    assert w.snapshot() == before and w.state.exists()


def test_r1_a_failure_after_the_checkout_changed_stays_fatal_at_boot(w, mod, monkeypatch):
    """The other side of c4385592320: once _apply began, a failure exits 1."""
    killed_tag_side(w, "Z3")
    real = mod._git

    def fail_at_restore(repo, *args, **kw):
        if args[:1] == ("restore",):
            raise mod.RepairError("restore failed")
        return real(repo, *args, **kw)

    monkeypatch.setattr(mod, "_git", fail_at_restore)
    assert mod.main(["--boot", str(w.state), str(w.root)]) == 1


@pytest.mark.parametrize("window", ["before_clear", "before_finish"])
def test_r1_an_interrupted_report_is_not_unresolved_once_healed(w, mod, monkeypatch, window):
    """Codex c4385619777: killed after the restore but before the result was
    written (state still there, or already cleared), the next run clears the
    healed state without a new backup."""
    killed_tag_side(w, "Z3")
    target = "_after" if window == "before_clear" else "_finish"

    def die(self, *a, **kw):
        raise Killed

    monkeypatch.setattr(mod.Repair, target, die)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    assert w.report()["result"] is None
    assert w.state.exists() is (window == "before_clear")
    assert w.snapshot() == tag_snapshot()
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert not w.state.exists()
    assert len(w.backups()) == 1, "a new backup was made"


# ── T-D4: what an interrupted write leaves is the engine's ───────────────────


def test_td4_empty_prefix_and_ephemeral_paths_are_the_updates(w):
    """T-D4: an empty file at an added path, a prefix of the target's bytes (an
    interrupted write), and an ephemeral path holding other bytes: all are the
    update's, saved and restored, and none is reported."""
    killed_tag_side(w, "Z3")
    (w.root / "Z5").write_text("")
    (w.root / "sub" / "s1").write_text("s1 ch")
    (w.root / "AGENTS.md").write_text("machine written\n")
    before = w.snapshot()
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert_healed(w, before)
    rows = {row["path"]: row["class"] for row in w.report()["paths"]}
    assert rows["Z5"] == rows["sub/s1"] == rows["AGENTS.md"] == "engine", rows
    assert foreign_rows(w) == []
    assert [a["severity"] for a in w.alerts()] == ["warning"]


# ── T-D5: the forward case's compare-and-swap ───────────────────────────────


def test_td5_live_moved_meanwhile_is_unresolved_and_untouched(w, mod, monkeypatch):
    """T-D5: `live` moved after the repair read it: the compare-and-swap loses,
    the ref stays where the other writer put it, the report says unresolved,
    the state is kept, no hook is touched, and the exit is 1."""
    killed_rollback(w, "P")
    real = mod._git

    def racing(repo, *args, **kw):
        if args[:1] == ("update-ref",):
            other = _git(w.root, "commit-tree", "-m", "other writer", f"{w.tag}^{{tree}}")
            _git(w.root, "update-ref", "refs/heads/live", other)
        return real(repo, *args, **kw)

    monkeypatch.setattr(mod, "_git", racing)
    rc = mod.Repair(str(w.state), str(w.root), False, 10).run()
    assert rc == 1
    assert w.report()["result"].startswith("unresolved"), w.report()["result"]
    assert w.report()["hooks"] is None
    assert _git(w.root, "log", "-1", "--format=%s", "live") == "other writer", "the ref was moved"
    assert w.state.exists()
    assert w.hook_calls() == []


# ── T-D6: locks, within one budget ──────────────────────────────────────────


class FakeClock:
    def __init__(self, at=None):
        self.t, self.at = 0.0, at or {}

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        for when, fn in list(self.at.items()):
            if self.t >= when:
                del self.at[when]
                fn()


def _hold(path: Path) -> int:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _no_git(monkeypatch):
    real = subprocess.run

    def refuse(argv, *a, **kw):
        assert argv[0] != "git" and argv[:2] != ["bash", "-c"], f"spawned {argv}"
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", refuse)


def test_td6_a_running_update_holding_the_lock_is_left_alone(w, mod, monkeypatch):
    """At boot, update.lock held and the state's pid alive: the update starts its
    own server, so the repair reads nothing and exits 0 at once."""
    killed_tag_side(w, "Z3")
    w.write_state(w.row(pid=os.getpid()))
    fd = _hold(w.gen / "locks" / "update.lock")
    clock = FakeClock()
    monkeypatch.setattr(mod, "_now", clock.now)
    monkeypatch.setattr(mod, "_sleep", clock.sleep)
    _no_git(monkeypatch)
    try:
        assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 0
    finally:
        os.close(fd)
    assert clock.t == 0, "it waited"
    assert w.backups() == [] and w.state.exists()


def test_td6_a_zombie_holder_is_dead_and_a_lock_freed_in_time_is_repaired(w, mod, monkeypatch):
    """The state's pid is a zombie (killed, unreaped): treated as dead, so the
    repair waits; the lock frees after 30 s, inside the 60 s budget, and the
    checkout is healed."""
    killed_tag_side(w, "Z3")
    _aged(w.root / ".git" / "index.lock")
    zombie = subprocess.Popen(["true"])
    deadline = time.monotonic() + 30
    while Path(f"/proc/{zombie.pid}/stat").read_text().rsplit(") ", 1)[-1][:1] != "Z":
        assert time.monotonic() < deadline, "no zombie"
        time.sleep(0.05)
    try:
        w.write_state(w.row(pid=zombie.pid))
        assert mod._pid_alive(zombie.pid) is False
        fd = _hold(w.gen / "locks" / "update.lock")
        clock = FakeClock({30: lambda: os.close(fd)})
        monkeypatch.setattr(mod, "_now", clock.now)
        monkeypatch.setattr(mod, "_sleep", clock.sleep)
        assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 0
    finally:
        zombie.wait()
    assert 30 <= clock.t < 60
    assert not w.state.exists() and w.snapshot() == tag_snapshot()


@pytest.mark.parametrize("kind", ["mixed", "safe", "holds"])
def test_td6_a_lock_held_past_the_budget_refuses_only_a_mixed_tree(w, mod, monkeypatch, kind):
    """X1: an orphan holds update.lock past 60 s. A mixed tree must not start:
    exit 1, nothing written. Anything else starts: exit 0, #3532's HOLD (an
    edit off the move's paths) included: no tree the repair heals."""
    mixed = kind == "mixed"
    if mixed:
        killed_tag_side(w, "Z3")
    else:
        w.write_state(w.row())
    if kind == "holds":
        (w.root / "K").write_text("my edit\n")
        assert w.guard_rc() == 6
    fd = _hold(w.gen / "locks" / "update.lock")
    clock = FakeClock()
    monkeypatch.setattr(mod, "_now", clock.now)
    monkeypatch.setattr(mod, "_sleep", clock.sleep)
    try:
        rc = mod.Repair(str(w.state), str(w.root), True, 0).run()
    finally:
        os.close(fd)
    assert rc == (1 if mixed else 0)
    assert clock.t >= 60
    assert w.backups() == [] and w.state.exists()


def test_td6_the_checkout_lock_is_waited_for_then_refused(w, mod, monkeypatch):
    """X2: genesis-checkout.lock held past the budget: exit 1, nothing written."""
    killed_tag_side(w, "Z3")
    fd = _hold(w.root / ".git" / "genesis-checkout.lock")
    clock = FakeClock()
    monkeypatch.setattr(mod, "_now", clock.now)
    monkeypatch.setattr(mod, "_sleep", clock.sleep)
    try:
        assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 1
    finally:
        os.close(fd)
    assert clock.t >= 60 and w.backups() == []


def test_td6_an_index_lock_a_git_still_holds_is_waited_for(w, mod, monkeypatch):
    """X2: .git/index.lock held open by a running process: waited for, then exit
    1 untouched; once that process ends, the same lock is stale and is saved."""
    killed_tag_side(w, "Z3")
    lock = w.root / ".git" / "index.lock"
    lock.touch()
    holder = subprocess.Popen(["bash", "-c", 'exec 3<"$1"; sleep 600', "hold", str(lock)])
    try:
        deadline = time.monotonic() + 30
        while not mod._held_open(str(lock)):
            assert time.monotonic() < deadline, "the holder never opened the lock"
            time.sleep(0.05)
        clock = FakeClock()
        monkeypatch.setattr(mod, "_now", clock.now)
        monkeypatch.setattr(mod, "_sleep", clock.sleep)
        assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 1
        assert w.backups() == [] and lock.exists()
    finally:
        holder.kill()
        holder.wait()
    _aged(lock)
    assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 0
    assert (w.backups()[-1] / "git" / "index.lock").exists() and not lock.exists()


def test_td6_an_unreadable_fd_list_of_this_user_may_hold_the_lock(mod, tmp_path, monkeypatch):
    """A live git of this user whose fds cannot be read (EACCES) MAY hold
    the lock: it counts as held. A zombie's fd list is EACCES too, and a zombie
    holds nothing, so it does not count (MEASURED: a zombie's /proc/<pid>/fd is
    EACCES to its own user)."""
    if os.geteuid() == 0:
        pytest.skip("running as root: EACCES cannot be produced")
    proc = tmp_path / "proc"
    entry = proc / str(os.getppid())  # a live process of this user
    (entry / "fd").mkdir(parents=True)
    (entry / "stat").write_text("1 (git) S 1 1\n")
    (entry / "comm").write_text("git\n")
    _static_proc(monkeypatch, mod, proc)
    assert mod._held_open("/x/index.lock") is False, "control: a readable, empty fd list"
    (entry / "fd").chmod(0)
    try:
        assert mod._held_open("/x/index.lock") is True
        (entry / "stat").write_text("1 (git) Z 1 1\n")
        assert mod._held_open("/x/index.lock") is False, "a zombie counted as a holder"
    finally:
        (entry / "fd").chmod(0o755)


# ── T-D7: idempotent, and convergent after a kill ────────────────────────────


def test_td7_a_second_run_does_nothing(w):
    killed_tag_side(w, "Z3")
    assert w.repair().returncode == 0
    snap, backups = w.snapshot(), w.backups()
    r = w.repair()
    assert r.returncode == 0 and "nothing to repair" in r.stdout, r.stdout
    assert w.snapshot() == snap and w.backups() == backups


def test_td7_a_repair_killed_partway_converges(w, mod, monkeypatch):
    """Killed after its backup and before git restored anything: the state is
    kept, the next run heals, and the killed run's report reads superseded (so
    the collector never calls it unresolved)."""
    killed_tag_side(w, "Z3")
    real = mod._git

    def die_at_restore(repo, *args, **kw):
        if args[:1] == ("restore",):
            raise Killed
        return real(repo, *args, **kw)

    monkeypatch.setattr(mod, "_git", die_at_restore)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    assert w.state.exists()
    first = w.backups()[-1]
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not w.state.exists() and w.snapshot() == tag_snapshot()
    assert json.loads((first / "REPAIR.json").read_text())["result"] == "superseded"


def test_td7_a_repair_killed_before_moving_live_converges(w, mod, monkeypatch):
    """D1f: killed after the files were restored and the hooks recorded
    pending, before `live` moved. The next run supersedes that entry (HEAD is
    not the tag), finds every path at the tag, moves `live` and puts the hooks
    back."""
    killed_rollback(w, "P")
    real = mod._git

    def die_at_cas(repo, *args, **kw):
        if args[:1] == ("update-ref",):
            raise Killed
        return real(repo, *args, **kw)

    monkeypatch.setattr(mod, "_git", die_at_cas)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    first = w.backups()[-1]
    assert json.loads((first / "REPAIR.json").read_text())["hooks"] == "pending"
    assert w.state.exists() and _git(w.root, "rev-parse", "HEAD") == w.to
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not w.state.exists() and _git(w.root, "rev-parse", "live") == w.tag
    assert w.snapshot() == tag_snapshot()
    killed = json.loads((first / "REPAIR.json").read_text())
    assert killed["result"] == "superseded" and killed["hooks"] == "superseded", killed
    # Not backups()[-1]: two runs in one second sort by pid, not by age.
    [healed] = [b for b in w.backups() if b != first]
    assert json.loads((healed / "REPAIR.json").read_text())["hooks"] == "ok"
    assert [c["argv"] for c in w.hook_calls()] == [["restore-hooks", w.to, w.tag]]


def test_td7_a_repair_killed_after_moving_live_retries_the_hooks(w, mod, monkeypatch):
    """Killed right after the compare-and-swap: the next run puts the hooks
    back first (HEAD is the tag on `live`), then the guard's SAFE clears."""
    killed_rollback(w, "P")

    def die(self, *a):
        raise Killed

    monkeypatch.setattr(mod.Repair, "_restore_hooks", die)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    assert w.report()["hooks"] == "pending" and _git(w.root, "rev-parse", "HEAD") == w.tag
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not w.state.exists()
    assert w.report()["hooks"] == "ok"
    assert [c["argv"] for c in w.hook_calls()] == [["restore-hooks", w.to, w.tag]]


# ── T-D8: only the guard's SAFE clears ───────────────────────────────────────


def test_td8_a_foreign_edit_outside_the_move_keeps_the_state(w):
    """T-D8: K is not on the switch's paths, so an edit there is not the repair's
    to touch: the tree is healed, the state is kept, exit 0. Since #3532 the
    guard calls that state HOLD (exit 6), not GUARDS: someone else's edit, and
    the update moved nothing it could finish."""
    killed_tag_side(w, "Z3")
    (w.root / "K").write_text("my edit\n")
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stdout + r.stderr
    assert w.state.exists()
    assert (w.root / "K").read_text() == "my edit\n"
    assert w.report()["result"].startswith("healed, state kept"), w.report()["result"]
    assert w.guard_rc() == 6


def test_td8_a_guard_that_still_says_mixed_is_unresolved(w, mod, monkeypatch):
    killed_tag_side(w, "Z3")
    calls = []
    real = mod.Repair.__init__

    def init(self, *a, **kw):
        real(self, *a, **kw)
        verdict = self.guard.state_verdict

        def second_says_mixed(state, repo, *rest):
            calls.append(1)
            return verdict(state, repo, *rest) if len(calls) == 1 else ("mixed_tree", "still")

        self.guard.state_verdict = second_says_mixed

    monkeypatch.setattr(mod.Repair, "__init__", init)
    assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 1
    assert w.state.exists() and w.report()["result"].startswith("unresolved")


@pytest.mark.parametrize(
    ("after", "rc", "result"),
    [
        ("safe", 0, "healed"),
        ("guards", 0, "healed, state kept"),
        ("holds", 0, "healed, state kept"),  # #3532's HOLD: kept, the server starts
        ("mixed_tree", 1, "unresolved"),
        ("conflict_pending", 1, "unresolved"),
    ],
)
def test_td8_every_verdict_after_the_repair_has_one_action(w, mod, monkeypatch, after, rc, result):
    """The post-repair re-guard, over the guard's whole verdict set: only SAFE
    clears; GUARDS and HOLD keep the state and start (the tree is the tag's on
    the move's paths); a tree still mixed, or any other verdict, is unresolved.
    Pins that the guard's set is still the one this table covers."""
    killed_tag_side(w, "Z3")
    calls = []
    real = mod.Repair.__init__

    def init(self, *a, **kw):
        real(self, *a, **kw)
        assert set(self.guard.EXIT_CODES) == {
            "safe",
            "guards",
            "holds",
            "mixed_tree",
            "conflict_pending",
        }
        verdict = self.guard.state_verdict

        def second_says(state, repo, *rest):
            calls.append(1)
            return verdict(state, repo, *rest) if len(calls) == 1 else (after, "judged")

        self.guard.state_verdict = second_says

    monkeypatch.setattr(mod.Repair, "__init__", init)
    assert mod.Repair(str(w.state), str(w.root), True, 0).run() == rc
    assert w.state.exists() == (after != "safe")
    assert w.report()["result"].startswith(result), w.report()["result"]
    assert w.snapshot() == tag_snapshot()


def test_td8_a_state_rewritten_during_the_repair_is_kept(w, mod, monkeypatch):
    killed_tag_side(w, "Z3")
    real = mod._git

    def rewrite(repo, *args, **kw):
        if args[:2] == ("update-index", "-q"):
            w.write_state(w.row(pid=7))  # another update wrote its own row
        return real(repo, *args, **kw)

    monkeypatch.setattr(mod, "_git", rewrite)
    assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 0
    assert json.loads(w.state.read_text())["pid"] == 7


# ── T-D9, T-D16: nothing to do, out of scope, failures before a write ────────


def test_td9_no_state_spawns_nothing(w, mod, monkeypatch):
    _no_git(monkeypatch)
    assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 0


def test_td9_a_main_row_is_out_of_scope(w):
    """QD3: a half-written main checkout is not repaired: boot exits 0 with a
    journal line, the command exits 3, nothing is touched."""
    (w.root / "F").write_text("f\n")
    w.write_state(
        {"phase": "merging", "rollback_tag": "pre", "deploy_head": w.to, "pid": 2_147_483_647}
    )
    assert w.guard_rc(json.loads(w.state.read_text())) == 4, "control: main residue"
    boot = w.repair(mode="--boot")
    assert boot.returncode == 0 and "only `live` rows" in boot.stderr, boot.stderr
    cmd = w.repair()
    assert cmd.returncode == 3, cmd.stdout + cmd.stderr
    assert w.backups() == [] and (w.root / "F").exists()


def test_td16_a_kit_without_its_guard_skips_at_boot(w, tmp_path):
    """T-D16: the kit cannot load its guard: boot exits 0 with a journal line
    (the server starts as it would have before D), the command exits 1."""
    killed_tag_side(w, "Z3")
    kit = tmp_path / "kit"
    kit.mkdir()
    shutil.copy2(MODULE, kit / MODULE.name)
    argv = ["python3", "-I", "-S", str(kit / MODULE.name)]
    boot = subprocess.run(
        [*argv, "--boot", str(w.state), str(w.root)], capture_output=True, text=True, env=w.env()
    )
    assert boot.returncode == 0 and "nothing was changed" in boot.stderr, boot.stderr
    cmd = subprocess.run(
        [*argv, "--command", str(w.state), str(w.root)], capture_output=True, text=True, env=w.env()
    )
    assert cmd.returncode == 1, cmd.stderr
    assert w.backups() == []


def test_td16_a_failure_before_the_first_write_exits_0_at_boot(w):
    """A checkout neither at the tag nor at the target (the guard would not call
    it mixed, so this is reached only by a race): an error before any write."""
    killed_tag_side(w, "Z3")
    (w.root / ".git" / "HEAD").write_text("garbage\n")
    r = w.repair(mode="--boot")
    assert r.returncode == 0 and "live-move repair" in r.stderr, r.stderr
    assert w.backups() == []


# ── T-D10: the repair-move alias runs the kit ────────────────────────────────


def _entry_checkout(w: World) -> Path:
    """The entry and the lib it sources, placed in the scratch checkout (git
    never sees them: they are ignored there), with the checkout's own repair
    module replaced by a booby trap."""
    scripts = w.root / "scripts"
    (scripts / "lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "scripts" / "deploy_candidates", scripts / "deploy_candidates")
    for lib in ("deploy_marker.sh", "live_system_guard.sh"):  # the entry sources both
        shutil.copy2(LIB / lib, scripts / "lib" / lib)
    (scripts / "lib" / "live_move_repair.py").write_text("raise SystemExit(99)\n")
    (w.root / ".git" / "info").mkdir(exist_ok=True)
    (w.root / ".git" / "info" / "exclude").write_text("/scripts/lib/\n/scripts/deploy_candidates\n")
    return scripts / "deploy_candidates"


def test_td10_repair_move_runs_the_kit_never_the_checkouts_copy(w):
    killed_tag_side(w, "Z3")
    entry = _entry_checkout(w)
    missing = subprocess.run(
        ["bash", str(entry), "repair-move"], capture_output=True, text=True, env=w.env()
    )
    assert missing.returncode == 1 and "kit is not installed" in missing.stderr, missing.stderr
    assert "--explain" in missing.stderr
    w.kit()
    r = subprocess.run(
        ["bash", str(entry), "repair-move"], capture_output=True, text=True, env=w.env()
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert not w.state.exists()
    # scripts/ holds the entry and its lib, placed here outside git: not compared.
    outside = {k: v for k, v in w.snapshot().items() if k.split("/")[0] != "scripts"}
    assert outside == {k: v for k, v in tag_snapshot().items() if k.split("/")[0] != "scripts"}
    ts = w.report()["ts"]
    # The wrapper forwards --restore TS like --dismiss TS, and --check rides after it
    # (#3966 item 2): without the forward, argparse refuses --command with --restore.
    c = subprocess.run(
        ["bash", str(entry), "repair-move", "--restore", ts, "--check"],
        capture_output=True,
        text=True,
        env=w.env(),
    )
    assert c.returncode == 0, c.stdout + c.stderr
    assert f"report {ts}:" in c.stdout and "nothing was written" in c.stdout, c.stdout
    # #3974 item 4: --wait may come first; the mode is found wherever it is.
    c = subprocess.run(
        ["bash", str(entry), "repair-move", "--wait", "5", "--restore", ts, "--check"],
        capture_output=True,
        text=True,
        env=w.env(),
    )
    assert c.returncode == 0 and f"report {ts}:" in c.stdout, c.stdout + c.stderr
    # Two modes are refused before the kit runs: never the last one silently.
    for two in (["--restore", ts, "--dismiss", ts], ["--dismiss", ts, "--restore", ts, "--check"]):
        c = subprocess.run(
            ["bash", str(entry), "repair-move", *two], capture_output=True, text=True, env=w.env()
        )
        assert c.returncode == 1 and "one of --dismiss TS or --restore TS" in c.stderr, two
    assert w.report()["dismissed_at"] is None
    assert not w.state.exists()
    d = subprocess.run(
        ["bash", str(entry), "repair-move", "--dismiss", ts],
        capture_output=True,
        text=True,
        env=w.env(),
    )
    assert d.returncode == 0, d.stderr
    assert w.report()["dismissed_at"]
    e = subprocess.run(
        ["bash", str(entry), "repair-move", "--explain"],
        capture_output=True,
        text=True,
        env=w.env(),
    )
    assert e.returncode == 0 and "git restore --source=<tag>" in e.stdout, e.stdout


# ── T-D11: the rebuild's objects are hardened ────────────────────────────────


def test_td11_merge_tree_and_commit_tree_harden_their_objects(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        core = importlib.import_module("deploy_candidates_core")
        plan = importlib.import_module("deploy_candidates_plan")
    finally:
        sys.path.remove(str(REPO / "scripts"))
    w = World(tmp_path)
    _git(w.root, "branch", "cand", w.to)
    repo = core.Repo(w.root, _git_env(w.home))
    calls = []
    real = repo.git

    def record(*args, **kw):
        calls.append(args)
        return real(*args, **kw)

    monkeypatch.setattr(repo, "git", record)
    plan.build_plan(repo, w.tag, [("cand", w.to)], "test")
    for verb in ("merge-tree", "commit-tree"):
        made = [c for c in calls if verb in c]
        assert made, f"no {verb} call"
        assert all(c[:2] == ("-c", "core.fsync=committed") for c in made), made


# ── T-D17: the target's objects are gone ─────────────────────────────────────


def test_td17_an_unreadable_target_is_healed_by_the_full_tree(w):
    """§2.4: a power loss lost the target's loose objects. Every change against
    HEAD and every untracked file is saved and restored, and the repair's own
    clean-tree check clears the state."""
    killed_tag_side(w, "Z3")
    only_to = _git(w.root, "rev-list", "--objects", w.to, "--not", w.tag).split("\n")
    for line in only_to:
        oid = line.split()[0]
        (w.root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
    assert _git(w.root, "cat-file", "-e", w.to, check=False) == ""
    before = w.snapshot()
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert w.snapshot() == tag_snapshot() and not w.state.exists()
    assert w.report()["result"].startswith("healed (the target was unreadable)")
    backup = w.backups()[-1]
    for rel, node in before.items():
        if isinstance(node, bytes) and tag_snapshot().get(rel) != node:
            assert (backup / "tree" / rel).read_bytes() == node, rel


def _nested_dir_rows(w):
    """Codex c4385987795: P and P/inner are both directory rows."""
    killed_tag_side(w, "prepared")
    (w.root / "P" / "inner").unlink()
    (w.root / "P" / "inner").mkdir()
    (w.root / "P" / "inner" / "mine.txt").write_text("notes\n")


def _symlink_row_in_dir_row(w):
    """Audit S1: the target's P holds a symlink, so P/l is a row inside row P."""
    _retarget(w, lambda w: os.symlink("inner", w.root / "P" / "l"))
    killed_tag_side(w, "prepared")


@pytest.mark.parametrize("make", [_nested_dir_rows, _symlink_row_in_dir_row])
def test_r2_a_row_inside_a_saved_directory_is_saved_by_it(w, make):
    """Rows nested in a directory row are covered by its recursive copy (a
    second copy collided on every retry); each keeps a saved_as inside it. The
    index is kept byte for byte before anything changes."""
    make(w)
    index_before = (w.root / ".git" / "index").read_bytes()
    before = w.snapshot()
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert w.snapshot() == tag_snapshot() and not w.state.exists()
    backup = w.backups()[-1]
    assert (backup / "git" / "index").read_bytes() == index_before
    for row in w.report()["paths"]:
        if row["disk_changed"] and row["disk"] not in ("absent", "other"):
            assert os.path.lexists(backup / row["saved_as"]), row
    if foreign_rows(w):
        restore_all(w)
        assert {k: v for k, v in w.snapshot().items() if k.startswith("P")} == {
            k: v for k, v in before.items() if k.startswith("P")
        }


def _missing(w: World, spec: str) -> None:
    oid = _git(w.root, "rev-parse", f"{w.to}:{spec}")
    (w.root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()


@pytest.mark.parametrize("lost", ["subtree", "blob"])
def test_r2_a_missing_object_below_a_readable_root_takes_the_full_tree(w, lost):
    """Codex c4385987781: the target's root tree reads, but a subtree the diff
    needs, or a blob classification reads, is gone: the full-tree path heals."""
    killed_tag_side(w, "Z3")
    if lost == "blob":
        _lock_aside(w, lambda: (w.root / "sub" / "s1").write_text("mine\n"))
    _missing(w, "sub" if lost == "subtree" else "sub/s1")
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert w.snapshot() == tag_snapshot() and not w.state.exists()
    assert w.report()["result"].startswith("healed (the target was unreadable)")


def _assert_forward_healed_once(w, before, r) -> None:
    """One run: the tag's tree, the state gone, `live` moved back, the hooks
    put back, and every pre-repair node not at the tag's bytes saved."""
    assert r.returncode == 0, r.stdout + r.stderr
    assert w.snapshot() == tag_snapshot() and not w.state.exists()
    assert _git(w.root, "status", "--porcelain", "-uall") == ""
    assert _git(w.root, "rev-parse", "live") == w.tag
    assert w.report()["hooks"] == "ok" and len(w.backups()) == 1
    backup = w.backups()[-1]
    for rel, node in before.items():
        if isinstance(node, bytes) and tag_snapshot().get(rel) != node:
            assert (backup / "tree" / rel).read_bytes() == node, rel


@pytest.mark.parametrize("lost", ["root", "subtree"])
def test_d1f_an_unreadable_target_is_healed_toward_the_tag_in_one_run(w, lost):
    """SF-1: HEAD is the target, so the full-tree fallback must diff the TAG
    (worktree and index), never HEAD: the target's root tree, or a subtree of
    it, gone after a power loss, still heals in one run, losing nothing."""
    killed_rollback(w, "P")
    _missing(w, "" if lost == "root" else "P")
    assert w.guard_rc(w.row("done")) == 4, "control: the guard calls it half-written"
    before = w.snapshot()
    r = w.repair()
    _assert_forward_healed_once(w, before, r)
    assert w.report()["result"].startswith("healed (the target was unreadable)")


def test_d1f_a_torn_index_with_an_unreadable_target_is_rebuilt_from_the_tag(w):
    """SF-1: a torn index is rebuilt from the TAG (read-tree HEAD would read
    the target's missing tree), then the forward repair heals in one run."""
    killed_rollback(w, "P")
    _missing(w, "")
    index = w.root / ".git" / "index"
    torn = index.read_bytes()[: len(index.read_bytes()) // 2]
    index.write_bytes(torn)
    before = w.snapshot()
    r = w.repair(mode="--boot")
    _assert_forward_healed_once(w, before, r)
    assert (w.backups()[-1] / "git" / "index").read_bytes() == torn
    assert "index was torn" in w.report()["result"], w.report()["result"]


@pytest.mark.parametrize("victim", ["Z3", "prepared"])
def test_r2_a_torn_index_is_kept_and_rebuilt_from_the_tag(w, victim):
    """Audit S4: git reads a torn index without checking it (no error), then
    refuses to write it back. The repair keeps its bytes, rebuilds it from the
    tag, heals, and says so."""
    killed_tag_side(w, victim)
    index = w.root / ".git" / "index"
    torn = index.read_bytes()[: len(index.read_bytes()) // 2]
    index.write_bytes(torn)
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stdout + r.stderr
    assert w.snapshot() == tag_snapshot() and not w.state.exists()
    assert _git(w.root, "status", "--porcelain", "-uall") == ""
    assert (w.backups()[-1] / "git" / "index").read_bytes() == torn
    assert "index was torn" in w.report()["result"], w.report()["result"]


def test_r2_a_mode_only_edit_reapplies_its_mode(w):
    """Codex c4385987787, the writer half: an executable-bit-only edit is
    foreign, and the restore puts the bit back."""
    killed_tag_side(w, "Z3")  # sub/s1 is still the tag's bytes
    (w.root / "sub" / "s1").chmod(0o755)
    assert w.repair().returncode == 0
    assert not os.access(w.root / "sub" / "s1", os.X_OK)
    restore_all(w)
    assert os.access(w.root / "sub" / "s1", os.X_OK)


def test_r2_a_repair_killed_after_restoring_a_tag_directory_is_cleared(w, mod, monkeypatch):
    """Audit S2: killed after its restore, on a path that is a directory in the
    tag, the report keeps no result; the next run clears the healed state."""
    _retarget(w, _dir_becomes_file)
    killed_tag_side(w, "prepared")
    _killed_mid_repair(w, mod, monkeypatch, "after_apply")
    assert w.snapshot() == tag_snapshot() and w.state.exists()
    assert w.repair().returncode == 0 and not w.state.exists()


# ── T-D20: the remedy text ───────────────────────────────────────────────────

#: PR C's main-row text, byte for byte: main rows are out of D's scope (QD3).
MAIN_REMEDY = (
    "These paths hold part of an update's checkout. They may also include another "
    "session's edits: save any you want to keep (copy them aside) before restoring "
    "anything. Then restore tracked paths to HEAD with git restore --source=HEAD "
    "--staged --worktree -- <path>, delete the listed untracked files, and remove a "
    "stale .git/index.lock if no git process is running. "
    "HEAD is the code that ran before the update, so the checkout is then that code "
    "again: Dismiss the update on the dashboard's update card (or remove "
    "update_state.json). Do NOT run scripts/update.sh --post-merge: it refuses a "
    "half-written tree."
)


def _guard_cli(w: World, state: dict) -> subprocess.CompletedProcess:
    w.write_state(state)
    return subprocess.run(
        ["python3", "-I", "-S", str(GUARD), str(w.state), str(w.root)],
        capture_output=True,
        text=True,
        env=w.env(),
    )


def _post_merge_advice(text: str) -> list[str]:
    return [
        s
        for s in text.replace("\n", " ").split(". ")
        if "--post-merge" in s and "NOT" not in s and "refuses" not in s
    ]


def _adopted(w: World) -> dict:
    row = w.row()
    row.pop("live_move")
    row["own_head"] = w.to
    return row


@pytest.mark.parametrize("case", ["record", "adopted"])
def test_td20_a_tag_side_live_message_names_the_repair(w, case):
    (w.root / "Z1").write_text("z1\n")
    r = _guard_cli(w, w.row() if case == "record" else _adopted(w))
    assert r.returncode == 4, r.stdout + r.stderr
    assert "Put it back with scripts/deploy_candidates repair-move" in r.stdout, r.stdout
    assert "repair-move --explain" in r.stdout
    assert not _post_merge_advice(r.stdout), r.stdout
    # SF4: the by-hand steps follow the repair (its kit arrives in PR D2).
    assert r.stdout.index("repair-move") < r.stdout.index("save any you want to keep")


@pytest.mark.parametrize("case", ["record", "adopted"])
def test_td20_head_at_the_target_names_the_repair_too(w, case):
    """D1f: HEAD on `live` at the target, a rollback killed partway: the
    remedy is the repair, which finishes that rollback; never --post-merge."""
    _git(w.root, "checkout", "-q", "-B", "live", w.to)
    (w.root / "O").write_text("o\n")
    r = _guard_cli(w, w.row("done") if case == "record" else _adopted(w))
    assert r.returncode == 4, r.stdout + r.stderr
    assert "Put it back with scripts/deploy_candidates repair-move" in r.stdout, r.stdout
    assert "finishes the rollback that was killed" in r.stdout, r.stdout
    assert "repair-move --explain" in r.stdout, r.stdout
    assert not _post_merge_advice(r.stdout), r.stdout


def test_td20_an_adopted_tip_off_live_keeps_mains_wording(w):
    """Rule 7a's row detached at the adopted tip: the repair would not act
    there (it finishes a rollback only on `live`), so it is not named."""
    _git(w.root, "checkout", "-q", "--detach", w.to)
    (w.root / "O").write_text("o\n")
    r = _guard_cli(w, _adopted(w))
    assert r.returncode == 4, r.stdout + r.stderr
    assert "finishes the rollback that was killed" not in r.stdout, r.stdout


def test_td20_a_non_merging_adopted_row_on_live_keeps_mains_wording(w):
    """LOW-2: an adopted live row in a later phase (a rollback after a failed
    bootstrap, killed partway) is not the repair's (it acts on "merging" rows
    only), so the guard must not name it there."""
    _git(w.root, "checkout", "-q", "-B", "live", w.to)
    (w.root / "O").write_text("o\n")
    r = _guard_cli(w, {**_adopted(w), "phase": "bootstrap"})
    assert r.returncode == 4, r.stdout + r.stderr
    assert "finishes the rollback that was killed" not in r.stdout, r.stdout


def test_explain_names_the_forward_steps():
    """LOW-1/LOW-6: restore-hooks needs update.lock, which no by-hand route
    takes, so the step names the route that does: re-running repair-move. A
    row without a move record names its target by own_head."""
    r = subprocess.run(
        ["python3", "-I", "-S", str(MODULE), "--explain"], capture_output=True, text=True
    )
    assert r.returncode == 0
    assert "git update-ref refs/heads/live <tag> <target>" in r.stdout, r.stdout
    assert "under update.lock" not in r.stdout, r.stdout
    assert "re-run scripts/deploy_candidates repair-move" in r.stdout, r.stdout
    assert "own_head" in r.stdout, r.stdout


def test_d1f_a_kept_state_after_a_forward_heal_says_what_was_done(w):
    """LOW-5: an edit off the move's paths keeps the state (HOLD). The kept
    text must not say the update never changed the checkout: it moved `live`,
    and the repair moved it back."""
    killed_rollback(w, "P")

    def edit_k():  # K is the same at both ends: off the move's paths
        (w.root / "K").write_text("mine\n")
        _git(w.root, "add", "K")

    _lock_aside_if_any(w, edit_k)
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert w.state.exists() and _git(w.root, "rev-parse", "live") == w.tag
    result = w.report()["result"]
    assert result.startswith("healed, state kept"), result
    assert "never changed the checkout" not in result, result
    assert "moved back" in result, result


# ── T-D18: hooks pending are retried ─────────────────────────────────────────


def test_td18_hooks_that_failed_are_retried_until_put_back(w):
    killed_rollback(w, "P")
    r = w.repair(SPY_RC="1")
    assert r.returncode == 0 and not w.state.exists(), r.stdout + r.stderr
    assert w.report()["hooks"] == "pending"
    assert "git hooks are not put back yet" in r.stdout, r.stdout
    again = w.repair(mode="--boot")
    assert again.returncode == 0, again.stderr
    assert w.report()["hooks"] == "ok"
    assert [c["argv"] for c in w.hook_calls()] == [["restore-hooks", w.to, w.tag]] * 2


@pytest.mark.parametrize("moved", ["commit-on-live", "live-moved-head-detached"])
def test_td18_hooks_pending_after_live_moved_on_are_superseded(w, moved):
    """`refs/heads/live` itself left the tag (a mover now owns the hooks): the
    entry is superseded and restore-hooks is not run again."""
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    if moved == "commit-on-live":
        _git(w.root, "commit", "-q", "--allow-empty", "-m", "the next update")
    else:
        _git(w.root, "checkout", "-q", "--detach", w.tag)
        other = _git(w.root, "commit-tree", "-m", "the next update", f"{w.tag}^{{tree}}")
        _git(w.root, "update-ref", "refs/heads/live", other)
    assert w.repair(mode="--boot").returncode == 0
    assert w.report()["hooks"] == "superseded"
    assert len(w.hook_calls()) == 1


def test_td18_hooks_pending_while_head_is_merely_detached_wait(w):
    """SF-2: HEAD detached at the tag, `live` still there: nothing moved the
    hooks' owner, so the entry stays pending (no call), and once HEAD is back
    on `live` the retry puts them back."""
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    _git(w.root, "checkout", "-q", "--detach", w.tag)
    assert w.repair(mode="--boot").returncode == 0
    assert w.report()["hooks"] == "pending" and len(w.hook_calls()) == 1
    _git(w.root, "checkout", "-q", "live")
    assert w.repair(mode="--boot").returncode == 0
    assert w.report()["hooks"] == "ok" and len(w.hook_calls()) == 2


def test_td18_the_retry_runs_under_the_checkout_lock(w):
    """LOW-4: as the forward heal and update.sh's rollback do."""
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    assert w.repair(mode="--boot").returncode == 0
    assert [c["checkout_locked"] for c in w.hook_calls()] == [True, True]


def test_td18_a_dismiss_and_a_waiting_retry_both_land(w):
    """SF-3: --dismiss and the retry both take update.lock, so whichever gets
    it second writes over the first's result: the retry changes only `hooks`
    and the dismissal only `dismissed_at`, so neither is undone."""
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    ts = w.report()["ts"]
    w.write_state({"phase": "done"})  # a state, so a held lock is waited for
    fd = os.open(w.gen / "locks" / "update.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        p = subprocess.Popen(
            ["python3", "-I", "-S", str(MODULE), "--command", str(w.state), str(w.root),
             "--wait", "60"],
            env=w.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        d = subprocess.Popen(
            ["python3", "-I", "-S", str(MODULE), "--dismiss", ts, str(w.state), str(w.root),
             "--wait", "60"],
            env=w.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        time.sleep(2)
        assert w.report()["hooks"] == "pending", "control: the retry waits for the lock"
        assert not w.report()["dismissed_at"], "control: the dismissal waits for the lock"
    finally:
        os.close(fd)
    _out, err = p.communicate(timeout=120)
    assert p.returncode == 0, err
    _dout, derr = d.communicate(timeout=120)
    assert d.returncode == 0, derr
    assert w.report()["hooks"] == "ok" and len(w.hook_calls()) == 2
    assert w.report()["dismissed_at"], "the retry's write undid the dismissal"


def test_td18_a_dismiss_waits_while_restore_hooks_runs(w):
    """SF-3: the retry holds update.lock through restore-hooks and its write,
    so a --dismiss sent meanwhile waits for it and then lands on the retry's
    result: the hooks stay ok and the report is dismissed."""
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    ts = w.report()["ts"]
    p = subprocess.Popen(
        ["python3", "-I", "-S", str(MODULE), "--command", str(w.state), str(w.root)],
        env=w.env(SPY_SLEEP="4"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    time.sleep(2)
    start = time.monotonic()
    d = subprocess.run(
        ["python3", "-I", "-S", str(MODULE), "--dismiss", ts, str(w.state), str(w.root)],
        capture_output=True, text=True, env=w.env(),
    )  # fmt: skip
    waited = time.monotonic() - start
    assert d.returncode == 0, d.stderr
    _out, err = p.communicate(timeout=120)
    assert p.returncode == 0, err
    assert waited >= 1, "the dismissal did not wait for the retry's update.lock"
    assert len(w.hook_calls()) == 2, "control: the retry ran"
    assert w.report()["hooks"] == "ok"
    assert w.report()["dismissed_at"], "the dismissal was lost"


def test_td18_a_held_update_lock_with_no_state_exits_at_once(w):
    """A deploy restarting its own server holds update.lock: with no state
    file the retry waits for nothing, and runs nothing."""
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    fd = os.open(w.gen / "locks" / "update.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        start = time.monotonic()
        r = w.repair("--wait", "30")
        took = time.monotonic() - start
    finally:
        os.close(fd)
    assert r.returncode == 0 and took < 15, (took, r.stdout + r.stderr)
    assert len(w.hook_calls()) == 1 and w.report()["hooks"] == "pending"


@pytest.mark.parametrize("how", ["other-checkout", "symlinked-report"])
def test_td18_a_pending_report_the_loader_refuses_is_never_retried(w, tmp_path, how):
    """The retry reads reports through load_report, as the readers do: one
    naming another checkout, or a REPAIR.json that is a symlink, is never
    retried (restore-hooks is not run) and never rewritten."""
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    path = w.backups()[-1] / "REPAIR.json"
    report = json.loads(path.read_text())
    if how == "other-checkout":
        report["repo"] = str(tmp_path / "elsewhere")
        path.write_text(json.dumps(report))
    else:
        real = tmp_path / "real.json"
        real.write_text(json.dumps(report))
        path.unlink()
        path.symlink_to(real)
    before = path.read_bytes() if how == "other-checkout" else real.read_bytes()
    assert w.repair(mode="--boot").returncode == 0
    assert len(w.hook_calls()) == 1
    after = path.read_bytes() if how == "other-checkout" else real.read_bytes()
    assert after == before and (how == "other-checkout" or path.is_symlink())


@pytest.mark.parametrize("value", ["--help", "main", "a" * 39])
@pytest.mark.parametrize("field", ["other", "tag"])
def test_td18_a_tampered_commit_in_a_pending_entry_is_never_run(w, field, value):
    """REPAIR.json is user-writable: a commit field that is not a full commit
    id (an option, a ref name, a short id) never reaches restore-hooks' argv."""
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    path = w.backups()[-1] / "REPAIR.json"
    report = json.loads(path.read_text())
    report[field] = value
    path.write_text(json.dumps(report))
    assert w.repair(mode="--boot").returncode == 0
    assert len(w.hook_calls()) == 1
    assert json.loads(path.read_text())["hooks"] == "pending"


def test_td20_a_main_rows_text_is_pr_cs_byte_for_byte(w):
    (w.root / "F").write_text("f\n")
    r = _guard_cli(w, {"phase": "merging", "rollback_tag": "pre", "deploy_head": w.to})
    assert r.returncode == 4, r.stdout + r.stderr
    head, _, rest = r.stdout.partition(": the checkout is half-written between ")
    assert head == f"{w.state} records an update that stopped partway", head
    assert rest.rstrip("\n").endswith("\n" + MAIN_REMEDY), rest
    assert "repair-move" not in r.stdout


@pytest.mark.parametrize("marker", ["at-tag", ""])
@pytest.mark.parametrize("branch", ["live", "main"])
def test_td20_bootstraps_generic_refusal_names_the_repair_on_live_only(branch, marker):
    """LOW5: the hint rides only an at-tag refusal on live; any other refusal
    (no marker) keeps the generic text alone."""
    text = (REPO / "scripts" / "bootstrap.sh").read_text()
    start = text.index("_crash_recovery_refuse() {")
    body = text[start : text.index("\n}\n", start) + 3]
    r = subprocess.run(
        ["bash", "-c", body + f'_crash_recovery_refuse "a reason" {marker}'],
        capture_output=True,
        text=True,
        env={**_git_env(), "ORIGINAL_BRANCH": branch, "GENESIS_ROOT": "/nonexistent"},
    )
    assert r.returncode == 1 and "REFUSE: a reason." in r.stderr, r.stderr
    hinted = branch == "live" and marker == "at-tag"
    assert ("scripts/deploy_candidates repair-move" in r.stderr) is hinted, r.stderr
    assert "scripts/update.sh --post-merge" in r.stderr  # the generic line is kept


# ── Review round fixes (B1, SF2, SF3, SF4, LOW5, LOW6) ───────────────────────


def _guard_on_state(w: World) -> subprocess.CompletedProcess:
    """The guard's CLI on the state file as the repair left it (not a fresh row)."""
    return subprocess.run(
        ["python3", "-I", "-S", str(GUARD), str(w.state), str(w.root)],
        capture_output=True,
        text=True,
        env=w.env(),
    )


def test_b1_a_partial_record_kept_after_a_heal_stops_reading_mixed(w):
    """B1: a "partial" record plus a tracked edit off the move's paths. The heal
    restores the move's paths and keeps the state (HOLD); the kept file must no
    longer say "partial", or the guard reads it MIXED_TREE forever and every
    caller sends the operator back to a repair that makes a new backup each run."""
    killed_tag_side(w, "Z3")
    w.write_state(w.row("partial"))
    (w.root / "K").write_text("my edit\n")
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert w.state.exists() and w.report()["result"].startswith("healed, state kept")
    g = _guard_on_state(w)
    assert g.returncode == 6, g.stdout + g.stderr
    assert json.loads(w.state.read_text())["live_move"]["phase"] == "moving"
    backups = w.backups()
    again = w.repair()
    assert again.returncode == 0, again.stdout + again.stderr
    assert w.backups() == backups, "a second repair made a new backup"
    assert (w.root / "K").read_text() == "my edit\n"


def test_b1_a_partial_record_rewritten_meanwhile_is_not_overwritten(w, mod, monkeypatch):
    """The partial -> moving rewrite happens only while the file is still the
    bytes that were judged (the same compare as the clear)."""
    killed_tag_side(w, "Z3")
    w.write_state(w.row("partial"))
    (w.root / "K").write_text("my edit\n")
    real = mod._git

    def rewrite(repo, *args, **kw):
        if args[:2] == ("update-index", "-q"):
            w.write_state(w.row("partial", pid=7))  # another writer's row
        return real(repo, *args, **kw)

    monkeypatch.setattr(mod, "_git", rewrite)
    assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 0
    kept = json.loads(w.state.read_text())
    assert kept["pid"] == 7 and kept["live_move"]["phase"] == "partial"


def test_low6_a_kept_state_says_what_the_repair_did(w):
    """LOW6: after a heal the HOLD reason ("The update never changed the
    checkout") is no longer the whole story: the result names the restore first."""
    killed_tag_side(w, "Z3")
    (w.root / "K").write_text("my edit\n")
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    result = w.report()["result"]
    assert result.startswith(
        "healed, state kept: the move's paths were restored; what still holds: "
    ), result
    assert "the move's paths were restored; what still holds:" in r.stdout


def _symlink_world(tmp: Path) -> World:
    """A World whose tag has ``d -> ../outside`` (a tracked symlink) and whose
    target replaces it with a directory holding ``d/x``; ``../outside/x`` is a
    file outside the checkout that the symlink reaches."""
    w = World(tmp)
    _git(w.root, "checkout", "-q", "-B", "live", w.tag)
    os.symlink("../outside", w.root / "d")
    _git(w.root, "add", "d")
    _git(w.root, "commit", "-qm", "tag with a symlink")
    w.tag = _git(w.root, "rev-parse", "HEAD")
    (w.root / "d").unlink()
    (w.root / "d").mkdir()
    (w.root / "d" / "x").write_text("target x\n")
    _git(w.root, "add", "-A")
    _git(w.root, "commit", "-qm", "target with d/x")
    w.to = _git(w.root, "rev-parse", "HEAD")
    _git(w.root, "checkout", "-q", "-B", "live", w.tag)
    _git(w.root, "tag", "-f", "pre", w.tag)
    return w


def test_sf2_never_deletes_through_a_symlinked_parent(tmp_path):
    """SF2: a target-only path under a tracked directory symlink is outside the
    checkout; the repair must not lstat/unlink THROUGH the symlink."""
    w = _symlink_world(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x").write_text("not the checkout's\n")
    w.write_state(w.row("partial"))
    assert w.guard_rc(w.row("partial")) == 4, "control: a partial record reads mixed"
    r = w.repair()
    assert (outside / "x").exists(), "deleted a file outside the checkout: " + r.stdout + r.stderr
    assert (outside / "x").read_text() == "not the checkout's\n"
    assert os.readlink(w.root / "d") == "../outside"
    assert "d/x" not in {row["path"] for row in w.report()["paths"]}, w.report()["paths"]
    # The shared residue check (genesis_untracked_node, deploy_checkout.sh) counts
    # a target path reachable through the symlink as a collision, conservatively,
    # so the guard still says mixed: the repair reports that, it does not guess.
    assert r.returncode == 1 and w.report()["result"].startswith("unresolved"), r.stdout
    assert w.state.exists()


def test_sf4_repair_move_explain_works_with_no_kit(w, tmp_path):
    """SF4a / CodeRabbit c4390266663: with no kit installed (it arrives in PR
    D2), --explain still prints the by-hand steps. It reads the module as HEAD
    committed it, never the working tree's copy, which can be one of the
    half-written files: truncated here, so running it would fail."""
    co = tmp_path / "co"
    (co / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "deploy_candidates", co / "scripts" / "deploy_candidates")
    for name in ("deploy_marker.sh", "live_system_guard.sh", MODULE.name):
        shutil.copy2(LIB / name, co / "scripts" / "lib" / name)
    _git(co, "init", "-q", "-b", "live")
    _git(co, "add", "-A")
    _git(co, "commit", "-qm", "tag")
    half = co / "scripts" / "lib" / MODULE.name
    half.write_bytes(MODULE.read_bytes()[:300])  # mid-docstring: a SyntaxError
    control = subprocess.run(["python3", "-I", "-S", str(half), "--explain"], capture_output=True)
    assert control.returncode != 0, "control: the truncated copy runs"
    e = subprocess.run(
        ["bash", str(co / "scripts" / "deploy_candidates"), "repair-move", "--explain"],
        capture_output=True,
        text=True,
        env=w.env(),
    )
    assert e.returncode == 0, e.stderr
    assert "git restore --source=<tag>" in e.stdout, e.stdout


def test_explain_with_no_kit_ignores_a_callers_git_dir(w, tmp_path):
    """The no-kit --explain reads the module from this checkout's HEAD even when
    the caller exports GIT_DIR (a hook does): it never runs another repository's
    committed module."""
    co = tmp_path / "co"
    (co / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "deploy_candidates", co / "scripts" / "deploy_candidates")
    for name in ("deploy_marker.sh", "live_system_guard.sh", MODULE.name):
        shutil.copy2(LIB / name, co / "scripts" / "lib" / name)
    _git(co, "init", "-q", "-b", "live")
    _git(co, "add", "-A")
    _git(co, "commit", "-qm", "tag")
    other = tmp_path / "other"
    (other / "scripts" / "lib").mkdir(parents=True)
    trap = tmp_path / "ran-the-other-module"
    (other / "scripts" / "lib" / MODULE.name).write_text(f"open({str(trap)!r}, 'w').close()\n")
    _git(other, "init", "-q")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "other")
    env = {**w.env(), "GIT_DIR": str(other / ".git")}
    e = subprocess.run(
        ["bash", str(co / "scripts" / "deploy_candidates"), "repair-move", "--explain"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert not trap.exists(), "ran the module GIT_DIR named"
    assert e.returncode == 0, e.stderr
    assert "git restore --source=<tag>" in e.stdout, e.stdout


@pytest.mark.parametrize("lib", ["deploy_marker.sh", "live_system_guard.sh"])
@pytest.mark.parametrize("damage", ["truncated", "removed"])
def test_explain_survives_a_half_written_shell_lib(w, tmp_path, lib, damage):
    """Audit SHOULD-FIX: repair-move sources neither of the checkout's shell libs
    (the move it repairs may have truncated or removed them), so --explain still
    prints the steps."""
    co = tmp_path / "co"
    (co / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "deploy_candidates", co / "scripts" / "deploy_candidates")
    for name in ("deploy_marker.sh", "live_system_guard.sh", MODULE.name):
        shutil.copy2(LIB / name, co / "scripts" / "lib" / name)
    _git(co, "init", "-q", "-b", "live")
    _git(co, "add", "-A")
    _git(co, "commit", "-qm", "tag")
    hurt = co / "scripts" / "lib" / lib
    if damage == "truncated":
        hurt.write_bytes(hurt.read_bytes()[:200])
    else:
        hurt.unlink()
    env = {k: v for k, v in w.env().items() if k != "GENESIS_TEST_FENCE_LIVE_ROOTS"}
    e = subprocess.run(
        ["bash", str(co / "scripts" / "deploy_candidates"), "repair-move", "--explain"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert e.returncode == 0, e.stderr
    assert "git restore --source=<tag>" in e.stdout, e.stdout


def test_the_restore_never_stages_a_deletion_of_an_engine_row(w):
    """Audit LOW: an UNMERGED ancestor `sub` (git checks file/directory conflicts
    per stage) beside a stage-0 sub/s1. Restoring the ancestor's stages drops
    nothing below it, so no tag file is left staged for deletion."""
    killed_tag_side(w, "prepared")

    def plant():
        oids = [_hash(w, f"UNMERGED {n}\n") for n in (1, 2, 3)]
        recs = "".join(f"100644 {o} {n}\tsub\0" for n, o in zip((1, 2, 3), oids, strict=True))
        subprocess.run(
            ["git", "-C", str(w.root), "update-index", "-z", "--index-info"],
            input=recs.encode(),
            check=True,
            env=_git_env(),
        )

    _lock_aside_if_any(w, plant)
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    restore_all(w)
    st = _git(w.root, "status", "--porcelain")
    staged_deletions = [ln for ln in st.splitlines() if ln.startswith("D ")]
    assert staged_deletions == [], st
    assert "UU sub" in st, st


def test_sf4_a_tag_side_live_message_is_actionable_with_no_kit(w):
    """SF4b: the live MIXED_TREE message names repair-move first, and also
    carries PR C's by-hand steps, so it is actionable before any kit exists."""
    (w.root / "Z1").write_text("z1\n")
    r = _guard_cli(w, w.row())
    assert r.returncode == 4, r.stdout + r.stderr
    out = r.stdout
    assert "scripts/deploy_candidates repair-move" in out
    assert "git restore --source=" in out and "--staged --worktree" in out
    assert "delete the listed untracked files" in out
    assert "stale .git/index.lock if no git process is running" in out
    assert out.index("repair-move") < out.index("git restore --source=")
    assert not _post_merge_advice(out), out


def test_low5_the_live_hint_rides_only_the_at_tag_refusal():
    """LOW5: the repair-move hint is for a checkout at the tag. The forward
    refusal ("finish it with --post-merge") and a merge in progress do not get
    it; the guard-did-not-clear refusal (inside the at-tag branch) does."""
    text = (REPO / "scripts" / "bootstrap.sh").read_text()
    calls = [ln.strip() for ln in text.splitlines() if '_crash_recovery_refuse "' in ln]
    hinted = [c for c in calls if c.endswith(" at-tag")]
    assert len(hinted) == 1 and "did not clear this state" in hinted[0], hinted
    for c in calls:
        if "--post-merge" in c or "a merge is in progress" in c:
            assert not c.endswith(" at-tag"), c


# ── Round 4: the inventory is the restore's footprint; one stale-lock rule ───


@pytest.mark.parametrize(
    "make", [_staged_desc, _skip_worktree_desc, _deep_desc, _gitlink_desc, _ancestor_file]
)
def test_r4_an_index_entry_under_a_row_path_is_its_own_row(w, make):
    """Restoring P (a file in the tag) drops every index entry under P, so each
    is a row of its own: classified foreign, its blob saved, and after a
    `git gc --prune=now` the restore puts the entry, its flag and its node
    back (the gc took the target, so P's own files, the update's, cannot be
    told apart any more: those are refused by name, and only those). Invariant: every pre-repair index entry the repair changed
    has a row."""
    killed_tag_side(w, "prepared")
    path = _lock_aside_if_any(w, lambda: make(w))
    index_before = _git(w.root, "ls-files", "-s", "-z")
    entry = _git(w.root, "ls-files", "-s", "--", path)
    flags = _git(w.root, "ls-files", "-v", "--", path)
    node = w.snapshot().get(path)
    assert w.repair().returncode == 0
    rows = {row["path"]: row for row in w.report()["paths"]}
    after = {r.split("\t", 1)[1]: r for r in _git(w.root, "ls-files", "-s", "-z").split("\0") if r}
    for rec in (r for r in index_before.split("\0") if r):
        p = rec.split("\t", 1)[1]
        if after.get(p) != rec:
            assert p in rows, f"{p}: its index entry changed and it has no row"
    assert rows[path]["class"] == "foreign", rows[path]
    mode, oid = entry.split()[:2]
    if mode != "160000":
        assert (w.backups()[-1] / "index-blobs" / oid).is_file(), "the staged blob was not saved"
    _git(w.root, "-c", "gc.reflogExpire=now", "-c", "gc.reflogExpireUnreachable=now", "gc", "-q",
         "--prune=now")  # fmt: skip
    put = restore(w)
    refused = [ln for ln in put.stdout.splitlines() if " refused:" in ln]
    assert all("other:target-unreadable" in ln for ln in refused), put.stdout
    assert _git(w.root, "ls-files", "-s", "--", path) == entry
    assert _git(w.root, "ls-files", "-v", "--", path) == flags
    if make is _gitlink_desc:  # review N-11: a saved directory's empty subdirectory
        assert node == "dir" and w.snapshot().get(path) is None  # is not rebuilt
    else:
        assert w.snapshot().get(path) == node


def test_r4_the_safe_branch_retires_a_stale_index_lock(w):
    """Codex c4386510848: the tree is already the tag's, a stale index.lock is
    left: it is saved and removed before the state is cleared, so git works."""
    w.write_state(w.row())
    (w.root / ".git" / "index.lock").write_bytes(b"stale")
    _aged(w.root / ".git" / "index.lock")
    r = w.repair("--wait", "3")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not w.state.exists()
    assert not (w.root / ".git" / "index.lock").exists(), "the stale lock was left"
    assert (w.backups()[-1] / "git" / "index.lock").read_bytes() == b"stale"
    (w.root / "K").write_text("k2\n")
    _git(w.root, "add", "K")


def test_r4_ref_locks_a_killed_transaction_left_are_retired(w):
    """A switch killed at its reference transaction leaves HEAD.lock and the
    branch's lock: both are saved and removed, so `live` can move again."""
    killed_tag_side(w, "prepared")
    assert (w.root / ".git" / "HEAD.lock").exists(), "control: the kill left HEAD.lock"
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    backup = w.backups()[-1]
    for lock in ("HEAD.lock", "refs/heads/live.lock"):
        assert not (w.root / ".git" / lock).exists(), lock
        assert (backup / "git" / lock).exists(), lock
    _git(w.root, "update-ref", "refs/heads/live", "HEAD")
    _git(w.root, "switch", "-q", "-c", "probe")


def _static_proc(monkeypatch, mod, proc: Path) -> None:
    """Point the repair at a fake /proc. Its entries never change, so the
    re-check of an unreadable process (#4001) has nothing to wait out: its real
    wait would only age a fresh lock past LOCK_FRESH_S on the real clock while
    the fake clock runs out the budget."""
    monkeypatch.setattr(mod, "_PROC", str(proc))
    monkeypatch.setattr(mod, "_settle", lambda _s: None)


def _fake_proc(tmp_path: Path, comm: str) -> tuple[Path, Path]:
    """A /proc with one live process of this user, ``comm``, whose fd list
    cannot be read (non-dumpable)."""
    entry = tmp_path / "proc" / str(os.getppid())
    (entry / "fd").mkdir(parents=True)
    (entry / "stat").write_text(f"1 ({comm}) S 1 1\n")
    (entry / "comm").write_text(comm + "\n")
    (entry / "fd").chmod(0)
    lock = tmp_path / "index.lock"
    lock.write_bytes(b"")
    return tmp_path / "proc", lock


@pytest.mark.parametrize(
    ("comm", "age", "held"),
    [("codex", 0, True), ("codex", 600, False), ("(sd-pam)", 600, False), ("git", 600, True)],
)
def test_r4_an_unreadable_process_holds_only_if_git_or_the_lock_is_fresh(
    mod, tmp_path, monkeypatch, comm, age, held
):
    """A same-uid process whose fds cannot be read holds the lock if it is a
    git, or while the lock is younger than LOCK_FRESH_S (a non-git tool may
    write the index through a library); an old lock with only non-git
    unreadable processes around ((sd-pam), gpg-agent) is stale."""
    if os.geteuid() == 0:
        pytest.skip("running as root: EACCES cannot be produced")
    proc, lock = _fake_proc(tmp_path, comm)
    then = time.time() - age
    os.utime(lock, (then, then))
    _static_proc(monkeypatch, mod, proc)
    try:
        assert mod._held_open(str(lock)) is held
    finally:
        (proc / str(os.getppid()) / "fd").chmod(0o755)


def test_r4_ephemeral_paths_match_whole_like_the_guard(tmp_path, monkeypatch):
    """Codex c4386510850: `notes AGENTS.md` is not the top-level AGENTS.md; a
    foreign edit there is reported, and restored."""
    monkeypatch.setitem(TAG_FILES, "notes AGENTS.md", "n1\n")
    monkeypatch.setitem(TO_FILES, "notes AGENTS.md", "n2\n")
    w = World(tmp_path)
    killed_tag_side(w, "prepared")
    (w.root / "notes AGENTS.md").write_text("FOREIGN EDIT\n")
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    rows = {row["path"]: row for row in w.report()["paths"]}
    assert rows["notes AGENTS.md"]["class"] == "foreign", rows["notes AGENTS.md"]
    restore_all(w)
    assert (w.root / "notes AGENTS.md").read_text() == "FOREIGN EDIT\n"


def test_r4_a_fresh_ref_lock_is_waited_for_never_left_behind(w, mod, tmp_path, monkeypatch):
    """Outside a PID namespace a fresh lock with a non-dumpable non-git process
    around counts as held. The repair waits on EVERY lock it retires, not only
    index.lock: past the budget it exits 1 with the state kept (never cleared
    with the lock still in place); once the lock is older than LOCK_FRESH_S it
    is saved and retired and the checkout heals."""
    if os.geteuid() == 0:
        pytest.skip("running as root: EACCES cannot be produced")
    killed_tag_side(w, "prepared")
    proc, _ = _fake_proc(tmp_path, "codex")
    _static_proc(monkeypatch, mod, proc)
    clock = FakeClock()
    monkeypatch.setattr(mod, "_now", clock.now)
    monkeypatch.setattr(mod, "_sleep", clock.sleep)
    head_lock = w.root / ".git" / "HEAD.lock"
    ref_lock = w.root / ".git" / "refs" / "heads" / "live.lock"
    for lock in (head_lock, ref_lock):
        os.utime(lock)  # this test is about a fresh lock; the kill helper ages them
    try:
        assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 1
        assert w.state.exists() and head_lock.exists() and w.backups() == []
        old = time.time() - 2 * mod.LOCK_FRESH_S
        for lock in (head_lock, ref_lock):
            os.utime(lock, (old, old))
        assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 0
    finally:
        (proc / str(os.getppid()) / "fd").chmod(0o755)
    assert not w.state.exists() and not head_lock.exists()
    assert (w.backups()[-1] / "git" / "HEAD.lock").exists()


# ── #3657 / #3658: index ancestors are rows; no step reaches through a link ──


def test_r5_an_index_ancestor_of_a_row_path_is_saved_and_reapplied(w):
    """#3657 (Codex c4390238983): the index holds a foreign file `sub` while
    the disk holds sub/; restoring sub/s1 drops that entry. It is a row of its
    own, its blob is saved, and after `git gc --prune=now` the restore puts
    the staged `sub` back."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _ancestor_file(w))
    index_before = _git(w.root, "ls-files", "-s", "-z")
    oid = _git(w.root, "ls-files", "-s", "--", "sub").split()[1]
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    rows = {row["path"]: row for row in w.report()["paths"]}
    assert rows.get("sub", {}).get("class") == "foreign", sorted(rows)
    assert (w.backups()[-1] / "index-blobs" / oid).is_file(), "the staged blob was not saved"
    _gc(w)
    gone = subprocess.run(["git", "-C", str(w.root), "cat-file", "-e", oid], env=_git_env())
    assert gone.returncode != 0, "control: gc pruned the staged blob"
    restore_all(w)
    assert _git(w.root, "ls-files", "-s", "-z") == index_before
    assert _git(w.root, "cat-file", "blob", oid) == "STAGED SUB"


@pytest.mark.parametrize("ancestor", [False, True], ids=["staged-child", "ancestor-file"])
def test_r5_no_restore_reaches_through_a_restored_symlink(w, tmp_path, ancestor):
    """#3658 (Codex c4390238996): `sub` is a foreign symlink to a directory
    outside the checkout and the index holds foreign content at sub/s1 (or,
    with #3657, a foreign file `sub`). The repair never reads through the link;
    neither may the restore: the outside file survives it unchanged, the child
    gets only its index state back, and its row says it was not reachable."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s1").write_text("not the checkout's\n")
    killed_tag_side(w, "Z3")

    def plant():
        if ancestor:
            _ancestor_file(w)
        else:
            (w.root / "sub" / "s1").write_text("STAGED S1\n")
            _git(w.root, "add", "--", "sub/s1")
        shutil.rmtree(w.root / "sub")
        os.symlink(outside, w.root / "sub")

    _lock_aside(w, plant)
    index_before = _git(w.root, "ls-files", "-s", "-z")
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert (outside / "s1").read_text() == "not the checkout's\n", "the repair reached outside"
    rows = {row["path"]: row for row in w.report()["paths"]}
    assert rows["sub"]["class"] == "foreign", rows["sub"]
    if not ancestor:
        assert rows["sub/s1"]["class"] == "foreign", rows["sub/s1"]
        assert rows["sub/s1"].get("unreachable_through") == "sub", rows["sub/s1"]
    _gc(w)
    restore_all(w)
    assert (outside / "s1").exists(), "the restore deleted a file outside the checkout"
    assert (outside / "s1").read_text() == "not the checkout's\n"
    assert sorted(p.name for p in outside.iterdir()) == ["s1"], "the restore wrote outside"
    assert os.readlink(w.root / "sub") == str(outside)
    assert _git(w.root, "ls-files", "-s", "-z") == index_before


# ── #3492 B2a: no generated shell (#3868, #3869), the integrity record, modes ──


def _many_foreign(w: World, n: int) -> list[str]:
    """#3869: ``n`` target-only files the switch wrote whole (killed at
    "prepared"), each then overwritten with a person's own bytes: ``n``
    foreign rows. Long names make the old shell steps outgrow one argv string."""
    names = [f"many/{i:03d}-" + "n" * 80 for i in range(n)]

    def add(w):
        (w.root / "many").mkdir()
        for p in names:
            (w.root / p).write_text("theirs\n")

    _retarget(w, add)
    killed_tag_side(w, "prepared")

    def mine():
        for p in names:
            (w.root / p).write_text("mine\n")

    _lock_aside_if_any(w, mine)
    age_locks(w)
    return names


def test_3869_boot_with_300_foreign_rows_exits_0_and_queues_its_alert(w):
    """#3869: the tree is healed and the state cleared, so the start must
    proceed (rc 0) and the alert must queue. The old writer passed every
    stored step to bash in one argv string: E2BIG, rc 1, the alert lost."""
    _many_foreign(w, 300)
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stderr[-3000:]
    assert not w.state.exists(), "control: the repair healed and cleared"
    report = w.report()
    assert sum(1 for row in report["paths"] if row["class"] == "foreign") == 300
    assert report["reapply"] == []
    [alert] = w.alerts()
    assert alert["severity"] == "critical", alert
    assert f"repair-move --restore {report['ts']}" in alert["body"], alert["body"][:500]
    assert "rm -rf" not in alert["body"]


def test_3868_the_writer_keeps_no_shell_step(w, mod):
    """#3868: a printed `rm -rf` + `cp -a` step destroyed newer work when run
    late. The writer generates none: the report's `reapply` stays [] for older
    readers, and what it prints and alerts is the restore command."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _foreign_edit(w))
    age_locks(w)
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    report = w.report()
    assert any(row["class"] == "foreign" for row in report["paths"]), "control: a foreign row"
    assert report["reapply"] == []
    ts = report["ts"]
    texts = [r.stdout, *(a["body"] for a in w.alerts())]
    assert len(texts) == 2, texts
    for text in texts:
        assert f"scripts/deploy_candidates repair-move --restore {ts} --check" in text, text
        assert f"scripts/deploy_candidates repair-move --restore {ts}." in text, text
        assert "rm -rf" not in text and "cp -a" not in text and "update-index" not in text
    assert not hasattr(mod, "reapply_step"), "the step generator is still in the writer"


def _finished(w, mod):
    """A real repair with a foreign row, its queue emptied; (Repair, backup, manifest)."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _foreign_edit(w))
    age_locks(w)
    assert w.repair().returncode == 0
    shutil.rmtree(w.gen / "alerts")  # the same dedupe key would not queue again
    return mod.Repair(str(w.state), str(w.root), True, 0), str(w.backups()[-1]), w.report()


@pytest.mark.parametrize("rc", [0, 1])
def test_a_result_of_any_size_is_alerted_under_the_argv_bound(w, mod, rc):
    """Review SF-8: MIXED_TREE's reason lists every residue path, and the body
    goes to bash as one argv string and on as ALERT_BODY. MEASURED here: one
    string of 131,072 bytes or more is E2BIG (getconf ARG_MAX: 2,097,152 for
    argv and env together). The body is cut, says so, and the alert queues;
    the full result stays in REPAIR.json; the exit code is the caller's."""
    repair, backup, manifest = _finished(w, mod)
    huge = "unresolved: mixed_tree: " + " ".join(f"residue/{i:05d}" for i in range(20_000))
    assert len(huge.encode()) > 131_072, "control: one argv string over the limit"
    assert repair._finish(backup, manifest, huge, rc) == rc
    [alert] = w.alerts()
    body = alert["body"].encode()
    assert len(body) <= mod.ALERT_BYTES < 131_072, len(body)
    assert b"more bytes" in body and b"REPAIR.json" in body, body[-300:]
    assert w.report()["result"] == huge


@pytest.mark.parametrize("rc", [0, 1])
def test_a_failed_alert_never_changes_the_exit_code(w, mod, monkeypatch, rc):
    """The module header: exit 1 only where a start would run a half-written
    tree. An alert that cannot be queued (here: bash cannot be started) is
    logged, never raised."""
    repair, backup, manifest = _finished(w, mod)

    def cannot(*a, **k):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(mod.subprocess, "run", cannot)
    assert repair._finish(backup, manifest, "healed", rc) == rc
    assert w.report()["result"] == "healed"


@pytest.mark.parametrize(
    "backup, body",
    [
        ("/x/100%done", "y" * 20_000),  # the cut note was a %-format holding the path
        ("/x", "a\0b"),  # an argv string cannot hold a NUL
        ("/x", "lone \ud800 surrogate " * 2_000),  # not a surrogateescape byte
    ],
    ids=["percent-in-path", "nul", "lone-surrogate"],
)
def test_no_alert_text_can_raise_out_of_the_alert(w, mod, backup, body):
    """Security review W2: each of these raised past the alert's try (TypeError,
    ValueError, UnicodeEncodeError), so a healed boot exited 1 again. However
    the text looks, the alert logs and returns."""
    repair, _backup, _manifest = _finished(w, mod)
    repair._alert("critical", body, "live_move_repair:x", backup)
    if "%" in backup:
        [alert] = w.alerts()
        assert len(alert["body"].encode()) <= mod.ALERT_BYTES, len(alert["body"])
        assert "more bytes" in alert["body"] and backup in alert["body"]


def test_the_report_dir_is_never_one_it_did_not_make(w, mod):
    """Security review W1: a node already at the report's name (a symlink, or a
    directory others can write) was adopted, and _sync's chmod walk followed
    the link. The writer makes the dir itself, or finds the one it made this
    run (0700, this user's); anything else refuses before a byte is saved."""
    repair = mod.Repair(str(w.state), str(w.root), True, 0)
    os.makedirs(repair.backups, exist_ok=True)
    victim = w.gen / "victim"
    victim.mkdir()
    (victim / "f").write_text("x")
    os.chmod(victim / "f", 0o666)
    os.chmod(victim, 0o777)
    link = Path(repair.backups) / f"20991231T000000Z-1{mod.SUFFIX}"
    link.symlink_to(victim)
    loose = Path(repair.backups) / f"20991231T000000Z-2{mod.SUFFIX}"
    loose.mkdir()
    os.chmod(loose, 0o775)
    for planted in (link, loose):
        with pytest.raises(mod.RepairError):
            repair._report_dir(str(planted))
    assert stat.S_IMODE(os.lstat(victim).st_mode) == 0o777
    assert stat.S_IMODE(os.lstat(victim / "f").st_mode) == 0o666
    own = Path(repair.backups) / f"20991231T000000Z-3{mod.SUFFIX}"
    repair._report_dir(str(own))
    repair._report_dir(str(own))  # again in the same run: its own dir
    assert stat.S_IMODE(os.lstat(own).st_mode) == 0o700


def test_write_json_never_writes_through_a_planted_temp(mod, tmp_path):
    """Security review W1: the temp name is guessable (`<path>.<pid>.tmp`);
    a link planted there had the JSON written into its target and was then
    renamed over the report. The temp is created fresh, never followed."""
    victim = tmp_path / "victim"
    victim.write_text("keep")
    path = tmp_path / "REPAIR.json"
    os.symlink(victim, f"{path}.{os.getpid()}.tmp")
    mod.write_json(str(path), {"a": 1}, private=True)
    assert victim.read_text() == "keep"
    assert not path.is_symlink() and json.loads(path.read_text()) == {"a": 1}
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600


def _modes(root: Path) -> dict[str, int]:
    """Every node below ``root`` (outside .git) by relative path: its lstat st_mode."""
    out = {}
    for top, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not (top == str(root) and d == ".git")]
        for name in dirs + files:
            full = os.path.join(top, name)
            out[os.path.relpath(full, root)] = os.lstat(full).st_mode
    return out


def _foreign_exec_edit(w):
    (w.root / "sub" / "s1").write_text("mine, executable\n")
    os.chmod(w.root / "sub" / "s1", 0o775)
    return "sub/s1"


def _group_writable(w, make):
    """``make``'s foreign path, every non-executable regular file at or below
    it made 0664: the copy loses the group-write bit, the record keeps the mode."""
    path = make(w)
    full = w.root / path
    nodes = [full, *full.rglob("*")] if full.is_dir() and not full.is_symlink() else [full]
    for node in nodes:
        st = os.lstat(node)
        if stat.S_ISREG(st.st_mode) and not st.st_mode & stat.S_IXUSR:
            os.chmod(node, 0o664)
    return path


@pytest.mark.parametrize(
    "make",
    [_foreign_edit, _foreign_exec_edit, _foreign_untracked, _foreign_inside_dir, _foreign_symlink,
     _foreign_fifo_inside_dir, _foreign_dir_symlink_inside_dir],
)  # fmt: skip
def test_saved_json_records_every_saved_copy(w, make):
    """Rev 2 item 3 (owner ruling ced82b49: sha256): for every saved regular
    file under tree/, its sha256 and the mode the checkout had (st_mode &
    0o7777); a symlink, its target; a FIFO, its mode. In SAVED.json beside
    REPAIR.json (which names it), so a large directory row cannot push
    REPAIR.json over REPORT_BYTES."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _group_writable(w, make))
    age_locks(w)
    before = _modes(w.root)
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    backup = w.backups()[-1]
    report = w.report()
    assert report.get("integrity") == "SAVED.json", sorted(report)
    assert list(report)[-2:] == ["index_rebuilt", "integrity"], list(report)
    saved = json.loads((backup / "SAVED.json").read_text())
    assert set(saved) == {"files", "complete"} and saved["complete"] is True
    tree, seen = backup / "tree", {}
    for rel, mode in _modes(tree).items():
        if stat.S_ISDIR(mode):
            continue
        full, key = tree / rel, f"tree/{rel}"
        orig = stat.S_IMODE(before[rel])
        if stat.S_ISLNK(mode):
            seen[key] = {"type": "symlink", "target": os.readlink(full)}
        elif stat.S_ISFIFO(mode):
            seen[key] = {"type": "fifo", "mode": f"{orig:04o}"}
        else:
            sha = hashlib.sha256(full.read_bytes()).hexdigest()
            seen[key] = {"type": "file", "mode": f"{orig:04o}", "sha256": sha}
    assert seen and saved["files"] == seen
    if make is not _foreign_symlink:  # it saves a link, no regular file of its own
        modes = {v.get("mode") for v in seen.values()}
        assert modes & {"0664", "0775"}, f"control: the planted modes: {modes}"


@pytest.mark.parametrize("make", [_foreign_edit, _foreign_inside_dir])
def test_a_saved_file_keeps_its_extended_attributes(w, make):
    """The repair deletes the original after saving it, so the copy is the only
    record: a foreign file's user.* extended attributes are copied with it, as
    copy2 copied them before the hashing copier replaced it (#3973 review)."""
    killed_tag_side(w, "Z3")
    rel = make(w)
    src = w.root / rel
    files = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
    try:
        for f in files:
            os.setxattr(f, "user.genesis-test", f.name.encode())
    except OSError as exc:  # tmpfs before 6.6, some overlay setups
        pytest.skip(f"no user xattrs on this file system: {exc}")
    age_locks(w)
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    tree = w.backups()[-1] / "tree"
    copies = [tree / f.relative_to(w.root) for f in files]
    assert copies and all(c.is_file() for c in copies), copies
    for c in copies:
        assert os.getxattr(c, "user.genesis-test") == c.name.encode(), c


def test_a_file_over_the_hash_budget_is_recorded_without_a_hash(w, mod, monkeypatch):
    """B1's per-file budget (FILE_BYTES): a bigger saved file is recorded with
    sha256 null, which a restore then refuses (`too-large`), never guesses."""
    monkeypatch.setattr(mod, "HASH_BYTES", 3, raising=False)
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _foreign_edit(w))
    age_locks(w)
    assert mod.Repair(str(w.state), str(w.root), False, 0).run() == 0
    backup = w.backups()[-1]
    files = json.loads((backup / "SAVED.json").read_text())["files"]
    sizes = {k: (backup / k).stat().st_size for k, v in files.items() if v["type"] == "file"}
    assert {k for k, n in sizes.items() if n > 3} == {
        k for k, v in files.items() if v["type"] == "file" and v["sha256"] is None
    }
    assert any(n > 3 for n in sizes.values()) and any(n <= 3 for n in sizes.values()), sizes


def test_saved_json_stays_under_its_cap(w, mod, monkeypatch):
    """SAVED.json is bounded like REPAIR.json: past SAVED_BYTES the writer
    records no more and says so (`complete` false); a record left out is a
    copy with no hash, which a restore refuses."""
    monkeypatch.setattr(mod, "SAVED_BYTES", 400, raising=False)
    killed_tag_side(w, "prepared")
    age_locks(w)
    assert mod.Repair(str(w.state), str(w.root), False, 0).run() == 0
    raw = (w.backups()[-1] / "SAVED.json").read_bytes()
    saved = json.loads(raw)
    assert len(raw) <= 400 and saved["complete"] is False and saved["files"], raw


def test_the_budgets_match_the_restore_and_the_argv_bound():
    spec = importlib.util.spec_from_file_location("_restore", LIB / "live_move_restore.py")
    restore = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restore)
    spec = importlib.util.spec_from_file_location("_writer", MODULE)
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    assert writer.HASH_BYTES == restore.FILE_BYTES
    assert writer.ALERT_BYTES * 2 < 131_072  # argv $4 and ALERT_BODY: one string each


def test_the_report_folder_is_this_users_alone(w):
    """Rev 2 item 4 (owner ruling 73b6285d): deploy-backups 0700 (an existing
    one of this user's is set to it), the report dir 0700, REPAIR.json and
    SAVED.json 0600, and nothing below with a group or world write bit, under
    a umask that grants group write (genesis-server's UMask=0002)."""
    (w.gen / "deploy-backups").mkdir()
    os.chmod(w.gen / "deploy-backups", 0o775)
    killed_tag_side(w, "Z3")

    def plant():
        _group_writable(w, _foreign_inside_dir)
        os.mkfifo(w.root / "P" / "pipe")
        os.chmod(w.root / "P" / "pipe", 0o662)
        _foreign_staged(w)  # its blob is written to index-blobs/ under the umask

    _lock_aside(w, plant)
    age_locks(w)
    old = os.umask(0o002)
    try:
        r = w.repair()
    finally:
        os.umask(old)
    assert r.returncode == 0, r.stdout + r.stderr
    backup = w.backups()[-1]
    assert stat.S_IMODE(os.lstat(w.gen / "deploy-backups").st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(backup).st_mode) == 0o700
    for name in ("REPAIR.json", "SAVED.json"):
        assert stat.S_IMODE(os.lstat(backup / name).st_mode) == 0o600, name
    loose = {rel: f"{stat.S_IMODE(m):04o}" for rel, m in _modes(backup).items()
             if not stat.S_ISLNK(m) and m & 0o022}  # fmt: skip
    assert loose == {}, loose
    assert (backup / "tree" / "P" / "pipe").exists(), "control: the FIFO was saved"
    assert (backup / "tree" / "P" / "mine.txt").exists(), "control: the file was saved"
    assert any((backup / "index-blobs").iterdir()), "control: a blob was saved"


def test_a_linked_backup_root_still_heals_and_warns(w):
    """#3974 item 6 (owner ruling 2026-11-06: heal and warn): a deploy-backups
    that is a link (or another user's, or writable by others) is still saved
    into, since refusing would leave the checkout half-written, but the output
    and the alert say the restore check refuses reports there."""
    elsewhere = w.home / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (w.gen / "deploy-backups").symlink_to(elsewhere)
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _foreign_inside_dir(w))
    age_locks(w)
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(elsewhere.iterdir()), "control: the report was saved through the link"
    assert w.report()["result"].startswith("healed"), w.report()["result"]
    warning = "is not a folder of this user's alone"
    assert warning in r.stdout, r.stdout
    alerts = w.alerts()
    assert alerts and alerts[-1]["severity"] == "critical" and warning in alerts[-1]["body"], alerts


def test_repair_move_restore_reaches_the_kit(w):
    """The command the writer prints, `scripts/deploy_candidates repair-move
    --restore TS --check`, goes through the wrapper to the kit."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _foreign_edit(w))
    age_locks(w)
    entry = _entry_checkout(w)
    w.kit()
    assert w.repair().returncode == 0
    ts = w.report()["ts"]
    r = subprocess.run(
        ["bash", str(entry), "repair-move", "--restore", ts, "--check"],
        capture_output=True, text=True, env=w.env(),
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"report {ts}: " in r.stdout and '"sub/s1" node as-left' in r.stdout, r.stdout


@pytest.mark.parametrize("mode", ["--boot", "--command"])
def test_a_fifo_at_the_state_path_never_stalls_the_repair(w, mode):
    """#3964: the repair reads the state file under update.lock and the
    checkout lock; a FIFO there (open blocks with no writer) would hold both
    for every update and deploy. Read through the guard's bounded reader, it
    is an unreadable state: nothing to repair, at once."""
    os.mkfifo(w.state)
    r = subprocess.run(
        ["python3", "-I", "-S", str(MODULE), mode, str(w.state), str(w.root)],
        capture_output=True, text=True, env=w.env(), timeout=30,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cannot be read" in r.stdout + r.stderr, r.stdout + r.stderr
    assert stat.S_ISFIFO(os.lstat(w.state).st_mode), "the repair left the state path alone"


# ── SF-1: a moved-from commit no retry can read ──────────────────────────────

_ENGINE = ("deploy_candidates.py", "deploy_candidates_core.py", "deploy_candidates_gate.py",
           "deploy_candidates_manifest.py", "deploy_candidates_plan.py")  # fmt: skip
_TAG_HOOK = "#!/bin/sh\n# the tag's {}\nexit 0\n"
_CANDIDATE_HOOK = "#!/bin/sh\n# a candidate's pre-commit, only the target has it\nexit 0\n"


def _real_engine_world(tmp_path: Path, **extra: str) -> World:
    """The world with the REAL engine and sync-hooks.sh committed (not the
    spy), the tag's own pre-commit and pre-push, and the target's pre-commit
    changed (a hook an approved candidate brought); ``extra``: more tag files."""
    tag = {f"scripts/{n}": (REPO / "scripts" / n).read_text() for n in _ENGINE}
    tag.update(extra)
    tag["scripts/hooks/sync-hooks.sh"] = (REPO / "scripts" / "hooks" / "sync-hooks.sh").read_text()
    tag.update({f"scripts/hooks/{n}": _TAG_HOOK.format(n) for n in ("pre-commit", "pre-push")})
    tag[".gitignore"] = "__pycache__/\n"  # as the real checkout ignores the engine's bytecode
    return World(tmp_path, tag, {"scripts/hooks/pre-commit": _CANDIDATE_HOOK})


def _install(w: World, name: str, body: str) -> Path:
    dst = w.root / ".git" / "hooks" / name
    dst.write_text(body)
    dst.chmod(0o755)
    return dst


def _hook_install_failures(w: World) -> list[str]:
    """Readiness's own install check (gate.hook_install_failures), run on the
    scratch checkout by its committed engine."""
    code = (
        "import json, os, sys; sys.path.insert(0, sys.argv[1] + '/scripts'); "
        "import deploy_candidates_core as c, deploy_candidates_gate as g; "
        "print(json.dumps(g.hook_install_failures(c.Repo(c.Path(sys.argv[1]), os.environ))))"
    )
    r = subprocess.run(["python3", "-I", "-S", "-c", code, str(w.root)], capture_output=True,
                       text=True, env=w.env(), check=True)  # fmt: skip
    return json.loads(r.stdout)


@pytest.mark.parametrize("gone", ["target-trees", "target-commit"])
def test_sf1_hooks_the_moved_from_commit_cannot_give_back_are_reinstalled(
    tmp_path, gone, monkeypatch
):
    """SF-1 (owner ruling): the REAL engine's restore-hooks cannot restore from
    a moved-from commit it cannot read (its trees gone: the hook list is
    unreadable; the commit gone: it refuses, `does not resolve`), so a retry
    would fail forever. The repair backs up every installed hook that is not
    the tag's (the candidate's pre-commit, a hand-edited pre-push) into the
    report's hooks-before/, has the tag's engine install the tag's hooks, and
    records "reinstalled": readiness's install check then passes, the report
    and the alert name the backup, and no later run retries."""
    w = _real_engine_world(tmp_path)
    killed_rollback(w, "P")
    _install(w, "pre-commit", _CANDIDATE_HOOK)
    mine = _TAG_HOOK.format("pre-push") + "# mine\n"
    _install(w, "pre-push", mine)
    if gone == "target-trees":
        _missing(w, "")
        r = w.repair()
        first = w.backups()[-1]
    else:
        # Killed right after `live` moved back; then the target's commit is lost.
        spec = importlib.util.spec_from_file_location("lmr_sf1", MODULE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setenv("HOME", str(w.home))
        for k, v in w.env().items():
            if k.startswith("GIT_"):
                monkeypatch.setenv(k, v)

        def die(self, *a):
            raise Killed

        monkeypatch.setattr(mod.Repair, "_restore_hooks", die)
        with pytest.raises(Killed):
            mod.Repair(str(w.state), str(w.root), True, 0).run()
        assert w.report()["hooks"] == "pending", "control: the hooks are owed"
        first = w.backups()[-1]
        oid = w.to
        (w.root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
        r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stdout + r.stderr
    backup = first
    report = json.loads((backup / "REPAIR.json").read_text())
    assert report["hooks"] == "reinstalled", (report["hooks"], r.stdout + r.stderr)
    aside = backup / "hooks-before"
    assert (aside / "pre-commit").read_text() == _CANDIDATE_HOOK
    assert (aside / "pre-push").read_text() == mine, "the hand edit was not kept"
    assert stat.S_IMODE((aside / "pre-push").stat().st_mode) == 0o600
    assert report["hooks_before"] == ["pre-commit", "pre-push"], report.get("hooks_before")
    hooks = w.root / ".git" / "hooks"
    for name in ("pre-commit", "pre-push"):
        assert (hooks / name).read_text() == _TAG_HOOK.format(name), name
        assert os.access(hooks / name, os.X_OK), name
    assert _hook_install_failures(w) == []
    assert str(aside) in report["hooks_note"], report["hooks_note"]
    assert any(str(aside) in a["body"] for a in w.alerts()), w.alerts()
    before = (backup / "REPAIR.json").read_bytes()
    again = w.repair(mode="--boot")
    assert again.returncode == 0, again.stderr
    assert (backup / "REPAIR.json").read_bytes() == before, "a later run retried"
    assert "restore-hooks failed" not in again.stderr, again.stderr


def test_sf1_a_readable_moved_from_commit_is_never_reinstalled_over(tmp_path):
    """Control: the moved-from commit reads, and a hand-edited hook makes the
    real restore-hooks fail: the hooks stay pending (retried; the hand edit is
    readiness's to name), nothing is moved aside."""
    w = _real_engine_world(tmp_path)
    killed_rollback(w, "P")
    _install(w, "pre-commit", _CANDIDATE_HOOK)
    _install(w, "pre-push", _TAG_HOOK.format("pre-push") + "# mine\n")
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert w.report()["hooks"] == "pending"
    assert not (w.backups()[-1] / "hooks-before").exists()


def test_low2_a_retry_that_cannot_write_its_report_still_lets_the_heal_run(w, mod, monkeypatch):
    """LOW-2: one pending report's write failing (ENOSPC, a folder pruned
    meanwhile) is logged; the half-written tree in the same run still heals."""
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    pending = w.backups()[-1] / "REPAIR.json"
    assert json.loads(pending.read_text())["hooks"] == "pending"
    killed_tag_side(w, "Z3")
    age_locks(w)
    real = mod.write_json

    def full(path, *a, **kw):
        if os.path.abspath(path) == str(pending):
            raise OSError(28, "No space left on device")
        return real(path, *a, **kw)

    monkeypatch.setattr(mod, "write_json", full)
    assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 0
    assert not w.state.exists(), "the heal did not run"
    assert w.snapshot() == tag_snapshot()


def _real_engine_mod(w: World, monkeypatch):
    """The repair module loaded in-process against ``w`` (HOME, git env)."""
    spec = importlib.util.spec_from_file_location("lmr_sf1b", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("HOME", str(w.home))
    for k, v in w.env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    return mod


def _report_of(w: World) -> tuple[Path, dict]:
    [backup] = w.backups()
    return backup, json.loads((backup / "REPAIR.json").read_text())


def test_sf1_with_core_hookspath_elsewhere_nothing_is_touched(tmp_path):
    """LOW-D: git runs hooks from another directory, so a reinstall into
    $GIT_COMMON_DIR/hooks would install what git never runs: nothing is
    copied, removed or installed, and the hooks stay pending."""
    w = _real_engine_world(tmp_path)
    killed_rollback(w, "P")
    elsewhere = tmp_path / "elsewhere-hooks"
    elsewhere.mkdir()
    _git(w.root, "config", "core.hooksPath", str(elsewhere))
    _install(w, "pre-commit", _CANDIDATE_HOOK)
    _missing(w, "")
    w.repair()
    backup, report = _report_of(w)
    assert report["hooks"] == "pending" and not report.get("hooks_before"), report
    assert not (backup / "hooks-before").exists()
    assert (w.root / ".git" / "hooks" / "pre-commit").read_text() == _CANDIDATE_HOOK
    assert list(elsewhere.iterdir()) == []


def test_sf1_an_unlisted_hook_and_one_equal_to_the_tags_are_left_alone(tmp_path):
    """LOW-D: only a hook the tag's sync-hooks.sh lists AND whose bytes are not
    the tag's is moved aside: a user's own hook the list does not name (even
    with a source at the tag) and a hook already the tag's copy are neither
    backed up nor replaced."""
    w = _real_engine_world(tmp_path, **{"scripts/hooks/post-checkout": _TAG_HOOK.format("pc")})
    killed_rollback(w, "P")
    _install(w, "pre-commit", _CANDIDATE_HOOK)
    equal = _install(w, "pre-push", _TAG_HOOK.format("pre-push"))
    inode = equal.stat().st_ino
    _install(w, "post-checkout", "#!/bin/sh\n# the user's own\nexit 0\n")
    _missing(w, "")
    w.repair()
    backup, report = _report_of(w)
    assert report["hooks"] == "reinstalled", report
    assert report["hooks_before"] == ["pre-commit"]
    assert sorted(p.name for p in (backup / "hooks-before").iterdir()) == ["pre-commit"]
    hooks = w.root / ".git" / "hooks"
    assert (hooks / "post-checkout").read_text() == "#!/bin/sh\n# the user's own\nexit 0\n"
    assert (hooks / "pre-push").stat().st_ino == inode, "a hook equal to the tag's was replaced"


def test_sf1_a_reinstall_that_fails_after_the_removal_is_recorded_then_converges(
    tmp_path, monkeypatch
):
    """LOW-A / LOW-D: the names moved aside are in REPAIR.json before any is
    removed, so a reinstall that then fails leaves a report saying where they
    went ("not reinstalled yet"); the next run converges."""
    w = _real_engine_world(tmp_path)
    killed_rollback(w, "P")
    _install(w, "pre-commit", _CANDIDATE_HOOK)
    _missing(w, "")
    mod = _real_engine_mod(w, monkeypatch)
    real = mod.Repair._restore_hooks

    def no_reinstall(self, before, after):
        return False if before == after else real(self, before, after)

    monkeypatch.setattr(mod.Repair, "_restore_hooks", no_reinstall)
    mod.Repair(str(w.state), str(w.root), False, 10).run()
    backup, report = _report_of(w)
    assert report["hooks"] == "pending", report["hooks"]
    assert report["hooks_before"] == ["pre-commit"]
    aside = backup / "hooks-before"
    assert str(aside) in report["hooks_note"] and "not reinstalled yet" in report["hooks_note"]
    assert (aside / "pre-commit").read_text() == _CANDIDATE_HOOK
    assert not (w.root / ".git" / "hooks" / "pre-commit").exists(), "control: it was removed"
    monkeypatch.undo()
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stdout + r.stderr
    _, report = _report_of(w)
    assert report["hooks"] == "reinstalled" and report["hooks_before"] == ["pre-commit"]
    assert _hook_install_failures(w) == []
    assert (aside / "pre-commit").read_text() == _CANDIDATE_HOOK, "the saved copy was lost"


def test_sf1_a_symlinked_hook_is_never_moved_aside(tmp_path):
    """LOW-D: a hook that is a symlink is not a file the repair copies or
    removes (sync-hooks.sh then leaves it as user-modified: still pending)."""
    w = _real_engine_world(tmp_path)
    killed_rollback(w, "P")
    _install(w, "pre-commit", _CANDIDATE_HOOK)
    target = tmp_path / "my-pre-push"
    target.write_text("#!/bin/sh\n# mine, linked\nexit 0\n")
    target.chmod(0o755)
    link = w.root / ".git" / "hooks" / "pre-push"
    link.symlink_to(target)
    _missing(w, "")
    w.repair()
    backup, report = _report_of(w)
    assert link.is_symlink() and target.read_text() == "#!/bin/sh\n# mine, linked\nexit 0\n"
    assert not (backup / "hooks-before" / "pre-push").exists()
    assert report["hooks"] == "pending" and report["hooks_before"] == ["pre-commit"], report


@pytest.mark.parametrize("planted", ["open-folder", "linked-aside"])
def test_sf1_the_retry_writes_hooks_before_only_into_a_private_report_dir(
    tmp_path, monkeypatch, planted
):
    """LOW-C: the retry writes hooks-before/ under the heal's rule for a report
    dir: this user's, 0700, never through a link. A folder others can enter,
    or a hooks-before that is a link, refuses: nothing is copied or removed."""
    w = _real_engine_world(tmp_path)
    killed_rollback(w, "P")
    _install(w, "pre-commit", _CANDIDATE_HOOK)
    mod = _real_engine_mod(w, monkeypatch)

    def die(self, *a):
        raise Killed

    monkeypatch.setattr(mod.Repair, "_restore_hooks", die)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    monkeypatch.undo()
    first = w.backups()[-1]
    (w.root / ".git" / "objects" / w.to[:2] / w.to[2:]).unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if planted == "open-folder":
        first.chmod(0o755)
    else:
        (first / "hooks-before").symlink_to(elsewhere)
    w.repair(mode="--boot")
    report = json.loads((first / "REPAIR.json").read_text())
    assert report["hooks"] == "pending" and not report.get("hooks_before"), report
    assert list(elsewhere.iterdir()) == []
    assert (w.root / ".git" / "hooks" / "pre-commit").read_text() == _CANDIDATE_HOOK


def test_sf1_the_hooks_the_repair_would_touch_are_the_engines_list(mod):
    """NOTE-A: the repair's "is it listed" test (a name quoted in the tag's
    sync-hooks.sh) picks exactly the names the engine's own parser reads from
    the real sync-hooks.sh, out of every file under scripts/hooks."""
    text = (REPO / "scripts" / "hooks" / "sync-hooks.sh").read_text()
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "import deploy_candidates_gate as g; print(json.dumps(g.sync_hook_names(sys.stdin.read())))"
    )
    parsed = json.loads(subprocess.run(
        ["python3", "-I", "-S", "-c", code, str(REPO / "scripts")], input=text,
        capture_output=True, text=True, check=True,
    ).stdout)  # fmt: skip
    names = os.listdir(REPO / "scripts" / "hooks")
    assert {n for n in names if mod._sync_listed(text, n)} == set(parsed)
    assert set(parsed) <= set(names), "control: every listed hook has a source"
