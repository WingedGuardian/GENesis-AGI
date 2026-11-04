"""The scratch world every live-move repair test runs against (#3492 PR D1).

A scratch HOME (with ~/.genesis) and a repository whose `live` sits at the
rollback tag, killed mid-switch by real git (a smudge filter or a
reference-transaction hook). Shared by the writer suite
(test_live_move_repair.py) and the readers suite
(test_live_move_repair_readers.py); the `w` and `mod` fixtures are registered
in this directory's conftest.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
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
    for lock in ("index.lock", "HEAD.lock", "refs/heads/live.lock"):
        assert not (w.root / ".git" / lock).exists(), f"a stale {lock} was left"
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


def _lock_aside(w: World, fn):
    """Run ``fn`` with the killed switch's stale index.lock moved aside (git
    refuses an index write while it stands), then put it back."""
    lock = w.root / ".git" / "index.lock"
    lock.rename(lock.with_name("held"))
    try:
        return fn()
    finally:
        lock.with_name("held").rename(lock)


class Killed(BaseException):
    """A SIGKILL stand-in: not an Exception, so nothing in the module catches it."""


def _retarget(w: World, change) -> None:
    """Rebuild ``to`` as the current target plus ``change(w)`` (a symlink, or a
    directory that becomes a file), then put `live` back at the tag."""
    _git(w.root, "checkout", "-q", "--detach", w.to)
    change(w)
    _git(w.root, "add", "-A")
    _git(w.root, "commit", "-qm", "retarget")
    w.to = _git(w.root, "rev-parse", "HEAD")
    _git(w.root, "checkout", "-q", "-B", "live", w.tag)


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


def _dir_becomes_file(w):
    shutil.rmtree(w.root / "sub")
    (w.root / "sub").write_text("now a file\n")


def _hash(w: World, body: str) -> str:
    return subprocess.run(
        ["git", "-C", str(w.root), "hash-object", "-w", "--stdin"],
        input=body,
        capture_output=True,
        text=True,
        check=True,
        env=_git_env(),
    ).stdout.strip()


def _ancestor_file(w):
    """#3657: the index holds a file `sub` where the disk (and the tag) hold
    the directory sub/: restoring sub/s1 drops that ancestor entry."""
    oid = _hash(w, "STAGED SUB\n")
    _git(w.root, "update-index", "--force-remove", "sub/s1", "sub/t")
    _git(w.root, "update-index", "--add", "--cacheinfo", f"100644,{oid},sub")
    return "sub"


def _lock_aside_if_any(w: World, fn):
    lock = w.root / ".git" / "index.lock"
    return _lock_aside(w, fn) if lock.exists() else fn()


def _gc(w: World) -> None:
    _git(w.root, "-c", "gc.reflogExpire=now", "-c", "gc.reflogExpireUnreachable=now", "gc", "-q",
         "--prune=now")  # fmt: skip


def _run_steps(w: World) -> None:
    for step in w.report()["reapply"]:
        r = subprocess.run(["bash", "-c", step], capture_output=True, text=True, env=w.env())
        assert r.returncode == 0, step + "\n" + r.stderr


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


def _staged_desc(w):
    """Codex c4386510837: a staged P/notes whose worktree bytes differ."""
    (w.root / "P" / "notes").write_text("STAGED\n")
    _git(w.root, "add", "--", "P/notes")
    (w.root / "P" / "notes").write_text("WORKTREE\n")
    return "P/notes"


def _skip_worktree_desc(w):
    """Skip-worktree P/flagged whose disk file is absent (silent before)."""
    (w.root / "P" / "flagged").write_text("FLAGGED\n")
    _git(w.root, "add", "--", "P/flagged")
    _git(w.root, "update-index", "--skip-worktree", "P/flagged")
    (w.root / "P" / "flagged").unlink()
    return "P/flagged"


def _deep_desc(w):
    (w.root / "P" / "x" / "y").mkdir(parents=True)
    (w.root / "P" / "x" / "y" / "deep").write_text("DEEP\n")
    _git(w.root, "add", "--", "P/x/y/deep")
    return "P/x/y/deep"


def _gitlink_desc(w):
    """A gitlink P/sub (an empty directory on disk): silent before."""
    (w.root / "P" / "sub").mkdir()
    _git(w.root, "update-index", "--add", "--cacheinfo", f"160000,{w.tag},P/sub")
    return "P/sub"
