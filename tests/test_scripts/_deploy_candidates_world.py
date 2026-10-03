"""The scratch world every deploy-candidates test runs against.

``live`` is origin/main plus the candidate branches named in the deploy manifest,
rebuilt OFF the working tree. Every test runs against scratch repositories under
``tmp_path`` with ``HOME`` pointed at a scratch directory, so neither the real
deploy manifest nor the real deploy lock is ever read or written. GitHub is a
seam (a fake ``gh``); the server's commit is a seam (a fake ``serving``).

The ``dc``, ``dc_world`` and ``dc_ready`` fixtures are registered in this
directory's conftest.py.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import private_module

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
ENTRY = SCRIPTS / "deploy_candidates"
ENGINE = SCRIPTS / "deploy_candidates.py"
ENGINE_FILES = sorted(p.name for p in SCRIPTS.glob("deploy_candidates*"))
HOOK_NAMES = (
    "commit-msg",
    "post-commit",
    "pre-commit",
    "prepare-commit-msg",
    "pre-push",
    "pre-merge-commit",
)
HELPER_NAMES = ("emit_bugfix_audit.py", "db_admission_check.py")
LOCKED = ("add", "drop", "rebuild")


def ephemeral_re() -> str:
    """EPHEMERAL_DIRTY_RE as the lib defines it (the one definition)."""
    return subprocess.run(
        ["bash", "-c", f'. "{SCRIPTS}/lib/deploy_marker.sh"; printf %s "$EPHEMERAL_DIRTY_RE"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


class World:
    """A bare origin, an upstream clone that advances main, and the live checkout."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        (self.home / ".genesis").mkdir(parents=True)
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = t\n\temail = t@example.invalid\n[init]\n\tdefaultBranch = main\n"
        )
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("GIT_", "GENESIS_", "DEPLOY_CANDIDATES_", "CLAUDECODE"))
        }
        self.env.update(
            HOME=str(self.home),
            GIT_CONFIG_NOSYSTEM="1",
            DEPLOY_CANDIDATES_EPHEMERAL_RE=ephemeral_re(),
        )
        self.origin = tmp / "origin.git"
        self.root = tmp / "root"
        self.up = tmp / "up"
        self.gh_states: dict[int, dict] = {}
        self.gh_fail: set[int] = set()
        self.gh_calls: list[list[str]] = []
        self.serving_sha: str | None = None
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
            env=self.env,
            check=True,
        )
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.up)],
            env=self.env,
            check=True,
            capture_output=True,
        )
        self.git(self.up, "checkout", "-q", "-b", "main")
        files = {
            "a.txt": "a1\na2\na3\n",
            "b.txt": "b\n",
            "scripts/hooks/sync-hooks.sh": (SCRIPTS / "hooks" / "sync-hooks.sh").read_text(),
            "scripts/lib/deploy_live.sh": "# PR C's file (a stand-in)\n",
        }
        for name in HOOK_NAMES + HELPER_NAMES:
            files[f"scripts/hooks/{name}"] = f"#!/bin/sh\n# {name}\nexit 0\n"
        self.commit(self.up, files, "base")
        self.git(self.up, "push", "-q", "origin", "main")
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.root)],
            env=self.env,
            check=True,
            capture_output=True,
        )
        # The installed hook copies equal their sources: readiness's hooks check passes.
        for name in HOOK_NAMES + HELPER_NAMES:
            dst = self.root / ".git" / "hooks" / name
            shutil.copy2(self.root / "scripts" / "hooks" / name, dst)
            dst.chmod(0o755)

    # ── git helpers ──────────────────────────────────────────────────────
    def git(self, repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
        )

    def rev(self, ref: str, repo: Path | None = None) -> str:
        return self.git(repo or self.root, "rev-parse", ref).stdout.strip()

    def commit(self, repo: Path, files: dict[str, str | None], msg: str) -> str:
        for name, text in files.items():
            p = repo / name
            if text is None:
                p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", msg)
        return self.rev("HEAD", repo)

    def advance_main(self, files: dict[str, str | None], msg: str = "upstream") -> str:
        self.git(self.up, "checkout", "-q", "main")
        sha = self.commit(self.up, files, msg)
        self.git(self.up, "push", "-q", "origin", "main")
        return sha

    def squash_merge(self, branch: str, msg: str = "squash") -> str:
        """Squash-merge a local candidate branch of the live checkout into origin
        main, the way GitHub does: one new commit on main, no ancestry."""
        self.git(
            self.root,
            "push",
            "-q",
            "-f",
            str(self.origin),
            f"refs/heads/{branch}:refs/heads/{branch}",
        )
        self.git(self.up, "fetch", "-q", "origin", branch)
        self.git(self.up, "checkout", "-q", "main")
        self.git(self.up, "merge", "--squash", "-q", "FETCH_HEAD")
        self.git(self.up, "commit", "-q", "-m", msg)
        self.git(self.up, "push", "-q", "origin", "main")
        return self.rev("HEAD", self.up)

    def candidate(
        self,
        branch: str,
        files: dict[str, str | None],
        base: str = "origin/main",
        msg: str | None = None,
    ) -> str:
        """A local candidate branch in the live checkout, made in a worktree."""
        wt = self.tmp / ("wt-" + branch.replace("/", "-"))
        if not wt.exists():
            self.git(self.root, "fetch", "-q", "origin")
            self.git(self.root, "worktree", "add", "-q", "-b", branch, str(wt), base)
        return self.commit(wt, files, msg or f"work on {branch}")

    def live_merges(self) -> list[tuple[str, str]]:
        """(candidate branch, merged head) per Deploy-rebuild merge, oldest first."""
        out = self.git(
            self.root,
            "log",
            "--first-parent",
            "--reverse",
            "--format=%H %P%x00%(trailers:key=Deploy-candidate,valueonly,separator=)",
            "refs/heads/live",
            "--not",
            "refs/remotes/origin/main",
            check=False,
        ).stdout
        result = []
        for line in out.splitlines():
            if "\0" not in line:
                continue
            ids, branch = line.split("\0", 1)
            parts = ids.split()
            if len(parts) == 3 and branch.strip():
                result.append((branch.strip(), parts[2]))
        return result

    # ── GitHub and the server ────────────────────────────────────────────
    def pr(self, n: int, branch: str, head: str | None = None, **kw) -> None:
        state = {
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": branch,
            "headRefOid": head or self.rev(f"refs/heads/{branch}"),
            "mergeCommit": None,
            "updatedAt": "2026-10-01T00:00:00Z",
        }
        state.update(kw)
        self.gh_states[n] = state

    def gh(self, args, cwd, env):
        self.gh_calls.append(list(args))
        if args[:2] == ["pr", "view"]:
            n = int(args[2])
            if n in self.gh_fail or n not in self.gh_states:
                return 1, "", "HTTP 502: unreachable"
            return 0, json.dumps(self.gh_states[n]), ""
        if args[:2] == ["pr", "list"]:
            return (
                0,
                json.dumps(
                    [
                        {"number": n, "headRefOid": s["headRefOid"]}
                        for n, s in self.gh_states.items()
                        if s.get("state") == "OPEN"
                    ]
                ),
                "",
            )
        return 1, "", "unexpected gh call"

    def serving(self, root):
        if self.serving_sha is None:
            return None, "genesis-server is not running"
        return self.serving_sha, ""

    # ── running the engine in-process ───────────────────────────────────
    def run(
        self,
        dc,
        *argv: str,
        lock: bool | None = None,
        extra_env: dict | None = None,
    ):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        if lock is None:
            lock = bool(argv) and argv[0] in LOCKED
        fd = None
        try:
            if lock:
                lock_path = self.home / ".genesis" / "locks" / "update.lock"
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
                env["DEPLOY_CANDIDATES_LOCK_FD"] = str(fd)
            return dc.main(list(argv), env=env, gh=self.gh, serving=self.serving, root=self.root)
        finally:
            if fd is not None:
                os.close(fd)

    def add(self, dc, branch: str, *extra: str) -> int:
        return self.run(dc, "add", branch, "--owner", "s1", *extra)

    @property
    def manifest_path(self) -> Path:
        return self.home / ".genesis" / "deploy_manifest.json"

    def manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text())

    def write_manifest(self, candidates: list[dict], **extra) -> None:
        common = os.path.realpath(self.root / ".git")
        data = {"version": 2, "repo": common, "candidates": candidates}
        data.update(extra)
        self.manifest_path.write_text(json.dumps(data))

    def entry(self, branch: str, pr: int | None = None, head: str | None = None, **kw) -> dict:
        head = head or self.rev(f"refs/heads/{branch}")
        e = {
            "branch": branch,
            "pr": pr,
            "owner_session": "s1",
            "added_at": "2026-10-01T00:00:00Z",
            "verified_head": head,
        }
        e.update(kw)
        return e

    # ── the shell entry ──────────────────────────────────────────────────
    def install_engine(self) -> None:
        """Copy the entry, the engine modules and the marker lib into the scratch
        checkout (untracked), so the entry runs the engine from a checkout whose
        root IS the scratch repository: nothing can point it at the real one."""
        (self.root / "scripts" / "lib").mkdir(parents=True, exist_ok=True)
        for name in ENGINE_FILES:
            shutil.copy2(SCRIPTS / name, self.root / "scripts" / name)
        shutil.copy2(
            SCRIPTS / "lib" / "deploy_marker.sh", self.root / "scripts" / "lib" / "deploy_marker.sh"
        )

    def plain_shell(
        self, *argv: str, extra_env: list[str] | None = None
    ) -> subprocess.CompletedProcess:
        """`env -i`: no PATH, no venv, no Claude Code environment. HOME is the only
        variable, so nothing can reach the real install. ``extra_env`` adds
        "NAME=value" entries, for a test of what an inherited variable can do."""
        env = ["HOME=" + str(self.home), "GIT_CONFIG_NOSYSTEM=1", *(extra_env or [])]
        return subprocess.run(
            [
                "env",
                "-i",
                *env,
                "/bin/bash",
                str(self.root / "scripts" / "deploy_candidates"),
                *argv,
            ],
            capture_output=True,
            text=True,
        )


@pytest.fixture()
def dc():
    return private_module("deploy_candidates_under_test", ENGINE)


@pytest.fixture()
def dc_world(tmp_path):
    return World(tmp_path)


@pytest.fixture()
def dc_ready(dc, dc_world, monkeypatch):
    """Every readiness condition met: the required merged commit is the scratch
    base (which also holds PR C's file), and the server serves that base."""
    base = dc_world.rev("refs/remotes/origin/main")
    monkeypatch.setattr(dc.gate, "REQUIRED_MERGED", (("scratch base", base),))
    dc_world.serving_sha = base
    return dc_world
