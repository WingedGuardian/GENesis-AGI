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

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "lib"
MODULE = LIB / "live_move_repair.py"
GUARD = LIB / "update_state_guard.py"
KIT_FILES = (
    "live_move_repair.py",
    "update_state_guard.py",
    "deploy_checkout.sh",
    "deploy_marker.sh",
    "alert_queue.sh",
)

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + bash")


def _git_env(home: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GENESIS_"))}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
    )
    if home is not None:
        env["HOME"] = str(home)
    return env


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, env=_git_env()
    ).stdout.strip()


def _write(root: Path, files: dict[str, str | None]) -> None:
    for path, body in files.items():
        full = root / path
        if body is None:
            if full.is_dir() and not full.is_symlink():
                shutil.rmtree(full)
            elif full.exists() or full.is_symlink():
                full.unlink()
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)


#: The tag (the old tip, checked out on `live`) and the move's target differ in
#: every way a switch writes: modifications (A, sub/s1), additions (F, Z1-Z5, and
#: AGENTS.md, an ephemeral path), a deletion (O) and a file that becomes a
#: directory (P -> P/inner). K and the spy are the same on both sides.
TAG_FILES = {
    "A": "a\n",
    "K": "keep\n",
    "O": "o\n",
    "P": "p\n",
    "sub/s1": "s1\n",
    "sub/t": "t\n",
}
TO_FILES = {
    "A": "a2 with more bytes\n",
    "AGENTS.md": "agents\n",
    "F": "f\n",
    "O": None,
    "P": None,
    "P/inner": "inner\n",
    "sub/s1": "s1 changed\n",
    "sub/t": None,
    **{f"Z{i}": f"z{i}\n" for i in range(1, 6)},
}


class World:
    """A scratch HOME (with ~/.genesis) and a repository whose `live` sits at the
    tag ``pre``; ``to`` is the rebuilt tip the move was going to."""

    def __init__(self, tmp: Path):
        self.home = tmp / "home"
        self.gen = self.home / ".genesis"
        self.gen.mkdir(parents=True)
        self.root = tmp / "repo"
        self.root.mkdir()
        self.state = self.gen / "update_state.json"
        _git(self.root, "init", "-q", "-b", "main")
        _write(self.root, TAG_FILES)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "tag")
        self.tag = _git(self.root, "rev-parse", "HEAD")
        _write(self.root, TO_FILES)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "rebuilt tip")
        self.to = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "checkout", "-q", "-B", "live", self.tag)
        _git(self.root, "tag", "pre", self.tag)
        _git(self.root, "branch", "-q", "-D", "main")

    def row(self, phase: str = "moving", **over) -> dict:
        return {
            "phase": "merging",
            "rollback_tag": "pre",
            "original_branch": "live",
            "own_head": self.tag,
            "deploy_head": self.tag,
            "post_merge": False,
            "pid": 2_147_483_647,
            "live_move": {"to": self.to, "phase": phase},
            **over,
        }

    def write_state(self, state: dict) -> None:
        self.state.write_text(json.dumps(state, indent=4))

    def env(self, **extra: str) -> dict[str, str]:
        return {**_git_env(self.home), **extra}

    def kit(self) -> Path:
        kit = self.gen / "lib" / "live-repair" / "current"
        kit.mkdir(parents=True, exist_ok=True)
        for name in KIT_FILES:
            shutil.copy2(LIB / name, kit / name)
        return kit

    def repair(
        self, *args: str, mode: str = "--command", **env: str
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", "-I", "-S", str(MODULE), mode, str(self.state), str(self.root), *args],
            capture_output=True,
            text=True,
            env=self.env(**env),
            timeout=600,
        )

    def guard_rc(self, state: dict | None = None) -> int:
        path = self.gen / "judge.json"
        path.write_text(json.dumps(state or self.row()))
        return subprocess.run(
            ["python3", "-I", "-S", str(GUARD), str(path), str(self.root)],
            capture_output=True,
            env=self.env(),
        ).returncode

    def backups(self) -> list[Path]:
        root = self.gen / "deploy-backups"
        return sorted(root.glob("*-live-move-repair")) if root.exists() else []

    def report(self) -> dict:
        return json.loads((self.backups()[-1] / "REPAIR.json").read_text())

    def alerts(self) -> list[dict]:
        queue = self.gen / "alerts" / "queue"
        return (
            [json.loads(p.read_text()) for p in sorted(queue.glob("*.json"))]
            if queue.exists()
            else []
        )

    def snapshot(self) -> dict[str, bytes | str]:
        """Every node under the checkout (outside .git): bytes, or "dir"/"link:<target>"."""
        nodes: dict[str, bytes | str] = {}
        for top, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not (top == str(self.root) and d == ".git")]
            for name in dirs + files:
                full = Path(top) / name
                rel = str(full.relative_to(self.root))
                if full.is_symlink():
                    nodes[rel] = "link:" + os.readlink(full)
                elif full.is_dir():
                    nodes[rel] = "dir"
                elif not full.is_file():  # a FIFO or socket: opening it would block
                    st = os.lstat(full)
                    nodes[rel] = f"special:{st.st_mode:o}"
                else:
                    nodes[rel] = full.read_bytes()
        return nodes


def tag_snapshot() -> dict[str, bytes | str]:
    nodes: dict[str, bytes | str] = {}
    for path, body in TAG_FILES.items():
        nodes[path] = body.encode()
        parent = os.path.dirname(path)
        while parent:
            nodes[parent] = "dir"
            parent = os.path.dirname(parent)
    return nodes


def kill_switch(root: Path, target: str, victim: str) -> None:
    """A real `git switch -C live <target>`, SIGKILLed by a smudge filter while it
    writes ``victim``: what git wrote before it is on disk, HEAD, `live` and the
    index stay put, and the stale index.lock a SIGKILL leaves is kept."""
    info = root / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text(f"{victim} filter=killer\n")
    _git(root, "config", "filter.killer.smudge", "kill -9 $PPID")
    _git(root, "config", "filter.killer.required", "true")
    r = subprocess.run(
        ["git", "-C", str(root), "switch", "--no-overwrite-ignore", "-C", "live", target],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    assert r.returncode != 0, "the switch was not killed"
    (info / "attributes").unlink()
    _git(root, "config", "--remove-section", "filter.killer")


def kill_prepared(root: Path, target: str) -> None:
    """A switch killed by its reference-transaction hook at "prepared": the whole
    target tree and index written, HEAD and `live` unmoved."""
    hook = root / ".git" / "hooks" / "reference-transaction"
    hook.write_text('#!/bin/sh\n[ "$1" = prepared ] && kill -9 $PPID\nexit 0\n')
    hook.chmod(0o755)
    r = subprocess.run(
        ["git", "-C", str(root), "switch", "-C", "live", target],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    hook.unlink()
    assert r.returncode != 0, "the switch was not killed"


def killed_tag_side(w: World, victim: str) -> None:
    """The move toward ``to`` killed while writing ``victim`` (or at "prepared")."""
    if victim == "prepared":
        kill_prepared(w.root, w.to)
    else:
        kill_switch(w.root, w.to, victim)
    assert _git(w.root, "rev-parse", "HEAD") == w.tag, "HEAD moved: the kill came too late"
    w.write_state(w.row())


def killed_rollback(w: World, victim: str) -> None:
    """The move finished (HEAD on `live` at ``to``), then the rollback's switch back
    to the tag was killed while writing ``victim`` (S18)."""
    _git(w.root, "checkout", "-q", "-B", "live", w.to)
    kill_switch(w.root, w.tag, victim)
    assert _git(w.root, "rev-parse", "HEAD") == w.to, "HEAD moved: the kill came too late"
    w.write_state(w.row("done"))


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def mod(monkeypatch, w):
    """The module loaded fresh in-process (its globals are per test), with HOME
    pointing at the scratch home."""
    for k in list(os.environ):
        if k.startswith(("GIT_", "GENESIS_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("HOME", str(w.home))
    for k, v in _git_env(w.home).items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("live_move_repair_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_healed(w: World, before: dict) -> None:
    """At the tag, byte for byte, the state gone, the guard SAFE on the original
    row, and every pre-repair node not at the tag's bytes saved byte-equal."""
    assert w.snapshot() == tag_snapshot(), "the checkout is not the tag's tree"
    assert _git(w.root, "status", "--porcelain", "-uall", "--ignored") == ""
    assert not (w.root / ".git" / "index.lock").exists()
    assert not w.state.exists(), "the healed state was not cleared"
    assert w.guard_rc() == 10
    backup = w.backups()[-1]
    tag = tag_snapshot()
    for rel, node in before.items():
        if isinstance(node, bytes) and tag.get(rel) != node:
            saved = [b for b in (backup / "tree" / rel, *(backup / "tree").rglob(rel))]
            assert any(p.is_file() and p.read_bytes() == node for p in saved), (
                f"{rel} was not saved"
            )
    assert w.report()["result"] == "healed"


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
    assert w.report()["reapply"] == [], "the update's own files were reported as foreign"


def test_td2_a_rollback_killed_partway_is_left_to_the_forward_repair(w):
    """HEAD on `live` at the target (a rollback killed partway) is the forward
    repair's case (D1f): the command says so, exits 1, and changes nothing."""
    killed_rollback(w, "P")
    assert w.guard_rc(w.row("done")) == 4, "control: the kill left a mixed tree"
    before, head = w.snapshot(), _git(w.root, "rev-parse", "live")
    r = w.repair()
    assert r.returncode == 1 and "forward case: needs the forward repair (D1f)" in r.stdout, (
        r.stdout
    )
    assert w.snapshot() == before and _git(w.root, "rev-parse", "live") == head
    assert w.state.exists() and w.backups() == []


# ── T-D3: foreign paths are saved, healed and reported ───────────────────────


def _foreign_edit(w):
    (w.root / "sub" / "s1").write_text("mine\n")
    return "sub/s1"


def _foreign_untracked(w):
    (w.root / "Z5").write_text("my own z5\n")
    return "Z5"


def _foreign_inside_dir(w):
    (w.root / "P" / "mine.txt").write_text("notes\n")
    return "P"


def _foreign_staged(w):
    (w.root / "A").write_text("staged by someone\n")
    _git(w.root, "add", "A")
    return "A"


def _foreign_unmerged(w):
    oids = [
        subprocess.run(
            ["git", "-C", str(w.root), "hash-object", "-w", "--stdin"],
            input=f"side {n}\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for n in (1, 2, 3)
    ]
    info = "".join(f"100644 {o} {n}\tsub/s1\n" for n, o in zip((1, 2, 3), oids, strict=True))
    _git(w.root, "update-index", "--force-remove", "sub/s1")
    subprocess.run(
        ["git", "-C", str(w.root), "update-index", "--index-info"],
        input=info,
        text=True,
        check=True,
        env=_git_env(),
    )
    return "sub/s1"


def _foreign_skip_worktree(w):
    _git(w.root, "update-index", "--skip-worktree", "sub/s1")
    (w.root / "sub" / "s1").write_text("hidden edit\n")
    return "sub/s1"


def _foreign_gitlink(w):
    _git(w.root, "update-index", "--add", "--cacheinfo", f"160000,{w.tag},Z5")
    (w.root / "Z5").mkdir()
    return "Z5"


def _foreign_both_flags(w):
    """`ls-files -v` tags a path with both flags `s` (MEASURED, git 2.43)."""
    _git(w.root, "update-index", "--skip-worktree", "sub/s1")
    _git(w.root, "update-index", "--assume-unchanged", "sub/s1")
    (w.root / "sub" / "s1").write_text("hidden edit\n")
    return "sub/s1"


def _foreign_symlink(w):
    os.symlink("A", w.root / "Z5")
    return "Z5"


def _foreign_dir_symlink_inside_dir(w):
    """os.walk lists a directory symlink among the dirnames, not the files."""
    os.symlink("../sub", w.root / "P" / "notes")
    return "P"


def _foreign_fifo_inside_dir(w):
    os.mkfifo(w.root / "P" / "pipe", 0o640)
    return "P"


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
    the step it prints puts the node and its index entries back exactly; the
    deploy-health reader then stops reporting it, whatever the node's kind."""
    killed_tag_side(w, "Z3")
    # The edit is made with the killed switch's stale index.lock moved aside
    # (git refuses an index write while it stands), then put back.
    lock = w.root / ".git" / "index.lock"
    lock.rename(lock.with_name("held"))
    path = make(w)
    lock.with_name("held").rename(lock)
    node_before = w.snapshot().get(path)
    sub_before = {k: v for k, v in w.snapshot().items() if k.startswith(path + "/")}
    index_before = _git(w.root, "ls-files", "-s", "--", path)
    flags_before = _git(w.root, "ls-files", "-v", "--", path)
    before = w.snapshot()
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert_healed(w, before)
    rows = {row["path"]: row for row in w.report()["paths"]}
    assert rows[path]["class"] == "foreign", rows[path]
    steps = w.report()["reapply"]
    assert len(steps) == 1, steps
    assert steps[0] in r.stdout, "the re-apply step was not reported"
    assert any(steps[0] in a["body"] for a in w.alerts()), "no alert carries the step"
    assert all(a["severity"] == "critical" for a in w.alerts())
    subprocess.run(["bash", "-c", steps[0]], check=True, env=w.env())
    after = w.snapshot()
    assert after.get(path) == node_before, "the re-apply step did not restore the node"
    assert {k: v for k, v in after.items() if k.startswith(path + "/")} == sub_before
    assert _git(w.root, "ls-files", "-s", "--", path) == index_before
    assert _git(w.root, "ls-files", "-v", "--", path) == flags_before
    found = _dh().collect_live_repairs(w.gen)
    assert found["saved"] == [], "re-applied, still reported"


def _lock_aside(w: World, fn):
    """Run ``fn`` with the killed switch's stale index.lock moved aside (git
    refuses an index write while it stands), then put it back."""
    lock = w.root / ".git" / "index.lock"
    lock.rename(lock.with_name("held"))
    try:
        return fn()
    finally:
        lock.with_name("held").rename(lock)


def test_r1_a_foreign_staged_blob_survives_gc_and_is_reapplied(w):
    """Codex c4385619761: staged content that differs from the worktree's is
    only in the object store; once the repair drops its index entry a
    `git gc --prune=now` deletes it. The backup holds the blob's bytes and the
    re-apply step writes the object back before the index entry."""
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
    step = w.report()["reapply"][0]
    subprocess.run(["bash", "-c", step], check=True, env=w.env())
    assert _git(w.root, "ls-files", "-s", "--", "A") == index_before
    assert _git(w.root, "cat-file", "blob", oid) == "staged by someone"
    assert (w.root / "A").read_text() == "then edited again\n"


def test_r1_a_name_with_a_newline_reapplies_its_index_entry(tmp_path, monkeypatch):
    """Codex c4385619771 / Devin c4385592698: index records are NUL-delimited, so
    a path with a newline neither breaks the re-apply step nor feeds git an
    extra record; and nothing is removed before the step that can fail."""
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
    [step] = w.report()["reapply"]
    r = subprocess.run(["bash", "-c", step], capture_output=True, text=True, env=w.env())
    assert r.returncode == 0, r.stderr
    assert _git(w.root, "ls-files", "-s", "-z") == index_before
    assert (w.root / name).read_text() == "mine\n"


@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_r1_a_fifo_or_socket_on_a_move_path_is_recorded(w, monkeypatch, kind):
    """Devin c4385592015: a node that holds no bytes is still recorded (type
    and mode) and reported as foreign; a FIFO's re-apply step makes it again,
    a socket's says it is gone (its process binds it again)."""
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
    [step] = w.report()["reapply"]
    assert step in r.stdout
    subprocess.run(["bash", "-c", step], check=True, env=w.env())
    import stat as st

    if kind == "fifo":
        mode = os.lstat(w.root / "Z5").st_mode
        assert st.S_ISFIFO(mode) and st.S_IMODE(mode) == 0o640
        assert _dh().collect_live_repairs(w.gen)["saved"] == []
    else:
        assert "socket" in step and not (w.root / "Z5").exists()


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
    healed state without a new backup, and the reader, which asks the guard
    rather than the report, never calls it unresolved."""
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
    later = time.time() + 2 * 3600
    assert _dh().live_repair_findings(_dh().collect_live_repairs(w.gen, now=later)) == []


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
    assert w.report()["reapply"] == []
    assert [a["severity"] for a in w.alerts()] == ["warning"]


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
    assert mod.Repair(str(w.state), str(w.root), True, 0).run() == 0
    assert (w.backups()[-1] / "git" / "index.lock").exists() and not lock.exists()


def test_td6_an_unreadable_fd_list_of_this_user_may_hold_the_lock(mod, tmp_path, monkeypatch):
    """A live process of this user whose fds cannot be read (EACCES) MAY hold
    the lock: it counts as held. A zombie's fd list is EACCES too, and a zombie
    holds nothing, so it does not count (MEASURED: a zombie's /proc/<pid>/fd is
    EACCES to its own user)."""
    if os.geteuid() == 0:
        pytest.skip("running as root: EACCES cannot be produced")
    proc = tmp_path / "proc"
    entry = proc / str(os.getppid())  # a live process of this user
    (entry / "fd").mkdir(parents=True)
    (entry / "stat").write_text("1 (git) S 1 1\n")
    monkeypatch.setattr(mod, "_PROC", str(proc))
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


class Killed(BaseException):
    """A SIGKILL stand-in: not an Exception, so nothing in the module catches it."""


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


def _guardian_pick(home: Path) -> dict | None:
    """The host guardian's container-side report pick (#3627), run against this HOME."""
    from genesis.guardian import collector as gc

    out = subprocess.run(
        [sys.executable, "-I", "-S", "-"],
        input=gc._REPAIR_PICK,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        timeout=60,
    ).stdout
    return json.loads(out.split(gc._REPAIR_MARK.encode(), 1)[1]) if out else None


def test_the_guardian_picks_what_this_writer_left_eligible(w, mod, monkeypatch):
    """#3627: the guardian's pick reads the fields THIS writer writes. A run
    killed partway (result unset) is eligible until the next run marks it
    superseded; the healed report is then the one shown, until --dismiss."""
    killed_tag_side(w, "Z3")
    real = mod._git

    def die_at_restore(repo, *args, **kw):
        if args[:1] == ("restore",):
            raise Killed
        return real(repo, *args, **kw)

    monkeypatch.setattr(mod, "_git", die_at_restore)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    killed = _guardian_pick(w.home)
    assert killed is not None and killed["result"] is None, killed
    assert w.repair(mode="--boot").returncode == 0
    healed = _guardian_pick(w.home)
    assert healed is not None and healed["result"] == "healed", healed
    d = subprocess.run(
        ["python3", "-I", "-S", str(MODULE), "--dismiss", healed["ts"], str(w.state), str(w.root)],
        capture_output=True,
        text=True,
        env=w.env(),
    )
    assert d.returncode == 0, d.stderr
    assert _guardian_pick(w.home) is None  # one superseded, one dismissed: nothing to show


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


def _retarget(w: World, change) -> None:
    """Rebuild ``to`` as the current target plus ``change(w)`` (a symlink, or a
    directory that becomes a file), then put `live` back at the tag."""
    _git(w.root, "checkout", "-q", "--detach", w.to)
    change(w)
    _git(w.root, "add", "-A")
    _git(w.root, "commit", "-qm", "retarget")
    w.to = _git(w.root, "rev-parse", "HEAD")
    _git(w.root, "checkout", "-q", "-B", "live", w.tag)


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
    for step in w.report()["reapply"]:
        subprocess.run(["bash", "-c", step], check=True, env=w.env())
    if w.report()["reapply"]:
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


# ── T-D19: the report's reader ───────────────────────────────────────────────


def _dh():
    return importlib.import_module("genesis.observability.snapshots.deploy_health")


def test_td19_the_collector_reports_until_reapplied_or_dismissed(w):
    dh = _dh()
    killed_tag_side(w, "Z3")
    (w.root / "sub" / "s1").write_text("mine\n")
    assert w.repair().returncode == 0
    found = dh.collect_live_repairs(w.gen)
    assert dh.live_repair_findings(found) == ["live_repair_saved:1"], found
    assert found["saved"] == w.report()["reapply"]
    subprocess.run(["bash", "-c", found["saved"][0]], check=True, env=w.env())
    assert dh.live_repair_findings(dh.collect_live_repairs(w.gen)) == [], (
        "re-applied, still reported"
    )
    (w.root / "sub" / "s1").write_text("s1\n")
    assert dh.live_repair_findings(dh.collect_live_repairs(w.gen)) == ["live_repair_saved:1"]
    path = w.backups()[-1] / "REPAIR.json"
    assert (
        subprocess.run(
            [
                "python3",
                "-I",
                "-S",
                str(MODULE),
                "--dismiss",
                w.report()["ts"],
                str(w.state),
                str(w.root),
            ],
            env=w.env(),
        ).returncode
        == 0
    )
    assert json.loads(path.read_text())["dismissed_at"]
    assert dh.live_repair_findings(dh.collect_live_repairs(w.gen)) == []


def test_r2_a_mode_only_edit_is_not_reapplied_until_its_mode_is_back(w):
    """Codex c4385987787: the reader compares what the writer compares, the
    executable bit (the one permission git tracks) as well as the bytes."""
    dh = _dh()
    killed_tag_side(w, "Z3")  # sub/s1 is still the tag's bytes
    (w.root / "sub" / "s1").chmod(0o755)
    assert w.repair().returncode == 0
    assert not os.access(w.root / "sub" / "s1", os.X_OK)
    [step] = w.report()["reapply"]
    assert dh.collect_live_repairs(w.gen)["saved"] == [step]
    subprocess.run(["bash", "-c", step], check=True, env=w.env())
    assert os.access(w.root / "sub" / "s1", os.X_OK)
    assert dh.collect_live_repairs(w.gen)["saved"] == []


def _killed_mid_repair(w: World, mod, monkeypatch, where: str) -> None:
    """A repair killed right after its restore (``_apply``), or at it."""
    real = mod.Repair._apply

    def die(self, *a, **kw):
        if where == "after_apply":
            real(self, *a, **kw)
        raise Killed

    monkeypatch.setattr(mod.Repair, "_apply", die)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    monkeypatch.setattr(mod.Repair, "_apply", real)
    assert w.report()["result"] is None


def test_td19_an_unfinished_repair_is_unresolved_while_the_tree_is_mixed(w, mod, monkeypatch):
    """The reader derives "unresolved" from the guard at read time: a report
    that never finished, an hour old, counts only while the shared guard still
    calls the checkout half-written; a later heal clears it."""
    dh = _dh()
    killed_tag_side(w, "Z3")
    _killed_mid_repair(w, mod, monkeypatch, "at_apply")
    later = time.time() + 2 * 3600
    assert dh.live_repair_findings(dh.collect_live_repairs(w.gen, now=time.time())) == []
    found = dh.collect_live_repairs(w.gen, now=later)
    assert dh.live_repair_findings(found) == ["live_repair_unresolved"]
    findings = dh.derive_findings(
        missing_units=[], tier2_pending=[], host_gateway={}, commits_behind=0, live_repairs=found
    )
    assert findings == ["live_repair_unresolved"]
    assert w.repair().returncode == 0
    assert dh.live_repair_findings(dh.collect_live_repairs(w.gen, now=later)) == []


def _dir_becomes_file(w):
    shutil.rmtree(w.root / "sub")
    (w.root / "sub").write_text("now a file\n")


def test_r2_a_repair_killed_after_restoring_a_tag_directory_is_not_unresolved(w, mod, monkeypatch):
    """Audit S2: killed after its restore, on a path that is a directory in the
    tag, the report keeps no result; the next run clears the healed state and
    the reader, judging the tree, reports nothing unresolved."""
    _retarget(w, _dir_becomes_file)
    killed_tag_side(w, "prepared")
    _killed_mid_repair(w, mod, monkeypatch, "after_apply")
    assert w.snapshot() == tag_snapshot() and w.state.exists()
    assert w.repair().returncode == 0 and not w.state.exists()
    later = time.time() + 2 * 3600
    assert _dh().collect_live_repairs(w.gen, now=later)["unresolved"] is False


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


def test_td20_head_at_the_target_keeps_pr_cs_message_until_d1f(w):
    _git(w.root, "checkout", "-q", "-B", "live", w.to)
    (w.root / "O").write_text("o\n")
    r = _guard_cli(w, w.row("done"))
    assert r.returncode == 4, r.stdout + r.stderr
    assert "finish with scripts/update.sh --post-merge" in r.stdout, r.stdout
    assert "repair-move" not in r.stdout, r.stdout


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


@pytest.mark.parametrize(
    "paths",
    [
        ["oops"],
        "x",
        [{"path": ["a"], "class": "foreign"}],
        [{"path": "p", "class": "foreign"}, 7],
    ],
)
def test_sf3_a_malformed_report_never_blanks_the_snapshot(tmp_path, paths):
    """SF3: a REPAIR.json of the wrong shape is skipped row by row, never raised
    (an exception there blanks the whole deploy-health snapshot)."""
    dh = _dh()
    d = tmp_path / "deploy-backups" / "20260101T000000Z-1-live-move-repair"
    d.mkdir(parents=True)
    report = {"result": "healed", "paths": paths, "reapply": ["step", 5], "repo": ["r"]}
    (d / "REPAIR.json").write_text(json.dumps(report))
    found = dh.collect_live_repairs(tmp_path)
    assert isinstance(found["saved"], list)
    assert all(isinstance(s, str) for s in found["saved"])


def test_sf3_a_non_list_reapply_counts_as_none(tmp_path):
    dh = _dh()
    d = tmp_path / "deploy-backups" / "20260101T000000Z-1-live-move-repair"
    d.mkdir(parents=True)
    report = {"result": "healed", "paths": [{"path": "p", "class": "foreign"}], "reapply": "s"}
    (d / "REPAIR.json").write_text(json.dumps(report))
    assert dh.collect_live_repairs(tmp_path)["saved"] == []


def test_sf4_repair_move_explain_works_with_no_kit(w):
    """SF4a: with no kit installed (it arrives in PR D2), --explain still prints
    the by-hand steps, from the checkout's own module (it only prints)."""
    entry = _entry_checkout(w)
    shutil.copy2(MODULE, entry.parent / "lib" / MODULE.name)
    e = subprocess.run(
        ["bash", str(entry), "repair-move", "--explain"],
        capture_output=True,
        text=True,
        env=w.env(),
    )
    assert e.returncode == 0, e.stderr
    assert "git restore --source=<tag>" in e.stdout, e.stdout


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
