"""scripts/lib/stale_index_lock.py: a dead git's index.lock is retired, a live one never (#3792).

Runs against a real temporary checkout and the real /proc: "held" is a child
process keeping the lock open, because ``_held_open`` deliberately ignores the
process that asks.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "stale_index_lock", ROOT / "scripts" / "lib" / "stale_index_lock.py"
)
sil = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sil)

OLD = 3 * 3600


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "checkout"
    (r / ".git").mkdir(parents=True)
    return r


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "genesis-home"
    h.mkdir()
    return h


def _lock(repo: Path, *, age: float) -> Path:
    lock = repo / ".git" / "index.lock"
    lock.write_bytes(b"")
    t = time.time() - age
    os.utime(lock, (t, t))
    return lock


def _wait_open(proc: subprocess.Popen, path: Path) -> None:
    """Wait until ``proc`` has ``path`` open. Its fd table changes while python
    starts, so an fd listed a moment ago may be gone when it is read."""
    fd_dir = f"/proc/{proc.pid}/fd"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(f"{fd_dir}/{fd}") == str(path):
                        return
                except OSError:
                    continue
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("holder never opened the lock")


def test_no_lock_is_nothing_to_do(repo, home):
    assert sil.heal(str(repo), str(home)) == (0, "stale-index-lock: none")


def test_a_young_lock_is_left_for_its_writer(repo, home):
    lock = _lock(repo, age=5)
    rc, line = sil.heal(str(repo), str(home))
    assert rc == 0 and "left for its writer" in line
    assert lock.exists()


def test_an_old_lock_nobody_holds_is_retired_and_saved(repo, home):
    lock = _lock(repo, age=OLD)
    rc, line = sil.heal(str(repo), str(home))
    assert rc == sil.RETIRED == 3 and "retired" in line
    assert not lock.exists()
    (backup,) = (home / "deploy-backups").iterdir()
    assert backup.name.endswith(sil.SUFFIX)
    assert (backup / "index.lock").exists()
    record = json.loads((backup / "LOCK.json").read_text())
    assert record["path"] == os.path.realpath(lock)
    assert record["age_s"] >= OLD - 5
    assert isinstance(record["processes"], list)


def test_an_old_lock_a_process_holds_is_never_touched(repo, home):
    lock = _lock(repo, age=OLD)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; f = open(sys.argv[1]); time.sleep(60)", str(lock)]
    )
    try:
        _wait_open(holder, lock)
        rc, line = sil.heal(str(repo), str(home))
    finally:
        holder.kill()
        holder.wait()
    assert rc == 0 and "held by a running process" in line
    assert lock.exists()


def _fake_git(
    tmp_path: Path, cwd: Path, *args: str, env: dict | None = None, name: str = "git"
) -> subprocess.Popen:
    """A process whose comm is ``name`` (a symlink to python), parked in ``cwd``."""
    exe = tmp_path / "fakebin" / name
    exe.parent.mkdir(exist_ok=True)
    if not exe.exists():
        exe.symlink_to(sys.executable)
    proc = subprocess.Popen(
        [str(exe), "-c", "import time; time.sleep(60)", *args],
        cwd=cwd,
        env={**os.environ, **(env or {})},
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if Path(f"/proc/{proc.pid}/comm").read_text().strip() == name[:15]:
                return proc
        except OSError:
            pass
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("fake git never started")


def test_a_lock_a_git_in_the_checkout_owns_without_an_fd_is_left(tmp_path, repo, home):
    """A commit closes the lock fd and keeps the lock while its editor runs."""
    lock = _lock(repo, age=OLD)
    sub = repo / "src"
    sub.mkdir()
    holder = _fake_git(tmp_path, sub)
    try:
        rc, line = sil.heal(str(repo), str(home))
    finally:
        holder.kill()
        holder.wait()
    assert rc == 0 and f"pid {holder.pid}" in line and "is working in" in line
    assert lock.exists()


def test_a_git_pointed_at_the_checkout_with_dash_c_counts(tmp_path, repo, home):
    lock = _lock(repo, age=OLD)
    holder = _fake_git(tmp_path, tmp_path, "-C", str(repo))
    try:
        rc, line = sil.heal(str(repo), str(home))
    finally:
        holder.kill()
        holder.wait()
    assert rc == 0 and "is working in" in line
    assert lock.exists()


def test_a_git_in_a_nested_linked_worktree_does_not_count(tmp_path, repo, home):
    """A linked worktree under the checkout has its own index and its own lock."""
    lock = _lock(repo, age=OLD)
    nested = repo / ".claude" / "worktrees" / "x"
    nested.mkdir(parents=True)
    (nested / ".git").write_text("gitdir: elsewhere\n")
    holder = _fake_git(tmp_path, nested)
    try:
        rc, _line = sil.heal(str(repo), str(home))
    finally:
        holder.kill()
        holder.wait()
    assert rc == sil.RETIRED
    assert not lock.exists()


def test_a_lock_replaced_while_it_was_checked_is_left(repo, home, monkeypatch):
    """A new writer took the lock between the age check and the removal."""
    lock = _lock(repo, age=OLD)

    def swap():
        lock.unlink()
        lock.write_bytes(b"new writer")
        return []

    monkeypatch.setattr(sil, "_processes", swap)
    rc, line = sil.heal(str(repo), str(home))
    assert rc == 0 and "changed while it was checked" in line
    assert lock.read_bytes() == b"new writer"
    assert not any((home / "deploy-backups").iterdir())


def test_a_linked_worktree_is_skipped(tmp_path, home):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /elsewhere\n")
    rc, line = sil.heal(str(wt), str(home))
    assert rc == 0 and "skipped" in line


def test_a_lock_that_cannot_be_saved_is_kept(repo, home):
    lock = _lock(repo, age=OLD)
    (home / "deploy-backups").write_text("not a directory")
    rc, line = sil.heal(str(repo), str(home))
    assert rc == 1 and "could not" in line
    assert lock.exists()


def test_old_backups_are_pruned_and_others_kept(repo, home):
    backups = home / "deploy-backups"
    old = backups / ("20260101T000000Z-1" + sil.SUFFIX)
    recent = backups / ("20261101T000000Z-1" + sil.SUFFIX)
    other = backups / "20260101T000000Z-1-live-move-repair"
    for d in (old, recent, other):
        d.mkdir(parents=True)
    t = time.time() - (sil.KEEP_DAYS + 1) * 86400
    for d in (old, other):
        os.utime(d, (t, t))
    sil.heal(str(repo), str(home))
    assert not old.exists()
    assert recent.exists() and other.exists()


def test_the_cli_exit_code_and_line(repo, home):
    _lock(repo, age=OLD)
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(ROOT / "scripts/lib/stale_index_lock.py"), str(repo),
         "--home", str(home)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert proc.returncode == 3  # never 2: python itself exits 2 when the script is missing
    assert proc.stdout.startswith("stale-index-lock: retired")


@pytest.mark.parametrize("script", ["scripts/update.sh", "scripts/deploy_code_only.sh"])
def test_every_checkout_lock_in_the_deploy_scripts_is_followed_by_the_heal(script):
    """The heal runs right after genesis-checkout.lock is taken, before any index write."""
    text = (ROOT / script).read_text()
    takes = [
        m.end() for m in re.finditer(r'if ! genesis_checkout_lock "\$GENESIS_ROOT"; then', text)
    ]
    assert takes
    for at in takes:
        after = text[at : at + 900]
        assert "stale_index_lock" in after, f"{script}: a checkout lock without the heal after it"


@pytest.mark.parametrize(
    "form",
    ["two-arg-git-dir", "two-arg-work-tree", "chained-c", "env-git-dir"],
)
def test_every_way_a_git_names_the_checkout_counts(tmp_path, repo, home, form):
    lock = _lock(repo, age=OLD)
    args, env, cwd = (), None, tmp_path
    if form == "two-arg-git-dir":
        args = ("--git-dir", str(repo / ".git"))
    elif form == "two-arg-work-tree":
        args = ("--work-tree", str(repo))
    elif form == "chained-c":
        args = ("-C", str(repo.parent), "-C", repo.name)
    else:
        env = {"GIT_DIR": str(repo / ".git")}
    holder = _fake_git(tmp_path, cwd, *args, env=env)
    try:
        rc, line = sil.heal(str(repo), str(home))
    finally:
        holder.kill()
        holder.wait()
    assert rc == 0 and "is working in" in line, form
    assert lock.exists()


def test_a_holder_seen_through_a_symlinked_checkout_path_counts(tmp_path, repo, home):
    """/proc reports physical paths; the lock is compared that way too."""
    lock = _lock(repo, age=OLD)
    link = tmp_path / "link"
    link.symlink_to(repo)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; f = open(sys.argv[1]); time.sleep(60)", str(lock)]
    )
    try:
        _wait_open(holder, lock)
        rc, line = sil.heal(str(link), str(home))
    finally:
        holder.kill()
        holder.wait()
    assert rc == 0 and "held by a running process" in line
    assert lock.exists()


def test_a_failed_save_leaves_no_partial_backup(repo, home, monkeypatch):
    lock = _lock(repo, age=OLD)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(sil.shutil, "copy2", boom)
    rc, line = sil.heal(str(repo), str(home))
    assert rc == 1 and "could not save" in line
    assert lock.exists()
    assert not any((home / "deploy-backups").iterdir())


def test_a_dashed_git_entry_point_in_the_checkout_counts(tmp_path, repo, home):
    """``git-commit -a`` waiting on its editor is named git-commit, not git."""
    lock = _lock(repo, age=OLD)
    holder = _fake_git(tmp_path, repo, name="git-commit")
    try:
        rc, line = sil.heal(str(repo), str(home))
    finally:
        holder.kill()
        holder.wait()
    assert rc == 0 and "is working in" in line
    assert lock.exists()


def test_a_git_daemon_in_the_checkout_does_not_count(tmp_path, repo, home):
    """A daemon never writes the index; its comm is cut to 15 characters."""
    lock = _lock(repo, age=OLD)
    holder = _fake_git(tmp_path, repo, name="git-credential-cache--daemon")
    try:
        rc, _line = sil.heal(str(repo), str(home))
    finally:
        holder.kill()
        holder.wait()
    assert rc == sil.RETIRED
    assert not lock.exists()


@pytest.mark.parametrize(
    ("argv", "daemon"),
    [
        (["git", "daemon", "--base-path=/x"], True),
        (["git", "fsmonitor--daemon", "run"], True),
        # Only `git <daemon>` matches: a daemon behind options counts (fail closed).
        (["git", "-C", "/x", "-c", "a=b", "--no-pager", "fsmonitor--daemon", "run"], False),
        (["git", "-C", "/daemon", "commit"], False),
        (["git", "--work-tree", "/srv/gitdaemon", "commit"], False),
        (["git", "--git-dir", "/x", "fsmonitor--daemon", "run"], False),
        (["git", "--attr-source", "daemon", "commit"], False),
        (["git", "commit", "-m", "daemon"], False),
        # A dashed builtin's arguments are its own, never a subcommand.
        (["git-commit", "-a", "-m", "daemon", "-e"], False),
        (["/usr/lib/git-core/git-commit", "-m", "fsmonitor--daemon"], False),
        (["git-merge", "-m", "daemon", "x"], False),
        (["/usr/lib/git-core/git-daemon", "--base-path=/x"], True),
        (["/usr/local/bin/git-lfs-daemon"], False),
        (["/usr/lib/git-core/git-credential-cache--daemon", "/s"], True),
        (["git", "commit", "-a"], False),
        (["git-commit", "-a"], False),
        (["git"], False),
    ],
)
def test_daemon_names(argv, daemon):
    assert sil._is_daemon(argv) is daemon


def _separate(tmp_path: Path, *, linked: bool = False) -> tuple[Path, Path]:
    """A checkout whose .git is a file naming its git directory."""
    gd = tmp_path / "gd"
    gd.mkdir()
    if linked:
        (gd / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gd}\n")
    return wt, gd


def test_a_separate_git_dir_checkout_retires_the_lock_in_its_git_dir(tmp_path, home):
    wt, gd = _separate(tmp_path)
    lock = gd / "index.lock"
    lock.write_bytes(b"")
    t = time.time() - OLD
    os.utime(lock, (t, t))
    rc, line = sil.heal(str(wt), str(home))
    assert rc == sil.RETIRED and str(lock) in line
    assert not lock.exists()


def test_a_git_in_a_separate_git_dir_checkout_counts(tmp_path, home):
    wt, gd = _separate(tmp_path)
    lock = gd / "index.lock"
    lock.write_bytes(b"")
    t = time.time() - OLD
    os.utime(lock, (t, t))
    holder = _fake_git(tmp_path, wt)
    try:
        rc, line = sil.heal(str(wt), str(home))
    finally:
        holder.kill()
        holder.wait()
    assert rc == 0 and "is working in" in line
    assert lock.exists()


def test_a_git_dir_holding_commondir_is_a_linked_worktree(tmp_path, home):
    wt, gd = _separate(tmp_path, linked=True)
    lock = gd / "index.lock"
    lock.write_bytes(b"")
    rc, line = sil.heal(str(wt), str(home))
    assert rc == 0 and "skipped" in line
    assert lock.exists()


def test_a_copy_that_cannot_be_synced_keeps_the_lock(repo, home, monkeypatch):
    lock = _lock(repo, age=OLD)
    real = os.fsync

    def fsync(fd):
        if os.readlink(f"/proc/self/fd/{fd}").endswith("index.lock"):
            raise OSError("EIO")
        return real(fd)

    monkeypatch.setattr(sil.os, "fsync", fsync)
    rc, line = sil.heal(str(repo), str(home))
    assert rc == 1 and "could not save" in line
    assert lock.exists()
    assert not any((home / "deploy-backups").iterdir())


def test_a_zombie_git_does_not_block_the_heal(tmp_path, repo, home):
    """A killed git its parent never reaped is a zombie: it holds nothing."""
    lock = _lock(repo, age=OLD)
    zombie = _fake_git(tmp_path, repo)
    zombie.kill()
    deadline = time.time() + 10
    while time.time() < deadline:
        stat = Path(f"/proc/{zombie.pid}/stat").read_text()
        if stat.rsplit(") ", 1)[-1][:1] == "Z":
            break
        time.sleep(0.05)
    else:
        raise AssertionError("never became a zombie")
    try:
        rc, _line = sil.heal(str(repo), str(home))
    finally:
        zombie.wait()
    assert rc == sil.RETIRED
    assert not lock.exists()


def test_a_relative_git_dir_after_a_relative_dash_c_counts(tmp_path, monkeypatch):
    """/proc shows the cwd after git applied -C, so a relative --git-dir after a
    relative -C may be meant from either; both are places."""
    proc = tmp_path / "proc"
    (proc / "77").mkdir(parents=True)
    work = tmp_path / "work"
    (work / "a").mkdir(parents=True)
    os.symlink(work / "a", proc / "77" / "cwd")
    (proc / "77" / "cmdline").write_bytes(
        b"\0".join([b"git", b"-C", b"a", b"--git-dir=../r/.git", b"add", b"x"])
    )
    (proc / "77" / "environ").write_bytes(b"")
    monkeypatch.setattr(sil, "_PROC", str(proc))
    places = sil._git_places("77")
    assert os.path.normpath(str(work / "r" / ".git")) in [os.path.normpath(x) for x in places]
