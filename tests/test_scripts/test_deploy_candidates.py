"""The integration-branch engine: ``scripts/deploy_candidates.py`` and its entry.

``live`` is origin/main plus the candidate branches named in the deploy manifest,
rebuilt OFF the working tree with ``git merge-tree --write-tree`` and
``git commit-tree``. Every test runs against scratch repositories under
``tmp_path`` with ``HOME`` pointed at a scratch directory, so neither the real
deploy manifest nor the real deploy lock is ever read or written. GitHub is a
seam: in-process runs pass a fake ``gh``; the shell-entry tests put a fake ``gh``
first on PATH.

Each refusal has a control that must succeed, so an engine that refuses
everything (or nothing) fails the suite.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import private_module

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
ENTRY = _SCRIPTS / "deploy_candidates"
ENGINE = _SCRIPTS / "deploy_candidates.py"
_HOOK_NAMES = (
    "commit-msg",
    "post-commit",
    "pre-commit",
    "prepare-commit-msg",
    "pre-push",
    "pre-merge-commit",
)


def _ephemeral_re() -> str:
    """EPHEMERAL_DIRTY_RE as the lib defines it (the one definition)."""
    return subprocess.run(
        ["bash", "-c", f'. "{_SCRIPTS}/lib/deploy_marker.sh"; printf %s "$EPHEMERAL_DIRTY_RE"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture()
def dc():
    return private_module("deploy_candidates_under_test", ENGINE)


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
            if not k.startswith(("GIT_", "GENESIS_", "DEPLOY_CANDIDATES_"))
        }
        self.env.update(
            HOME=str(self.home),
            GIT_CONFIG_NOSYSTEM="1",
            GENESIS_DEPLOY_CANDIDATES_ROOT=str(tmp / "root"),
            DEPLOY_CANDIDATES_EPHEMERAL_RE=_ephemeral_re(),
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
            "scripts/hooks/sync-hooks.sh": (_SCRIPTS / "hooks" / "sync-hooks.sh").read_text(),
        }
        for name in _HOOK_NAMES:
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
        for name in _HOOK_NAMES:
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
        sha = self.commit(wt, files, msg or f"work on {branch}")
        return sha

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

    # ── running the engine in-process ───────────────────────────────────
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
                        {"number": n, **s}
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

    def run(self, dc, *argv: str, lock: bool = False, extra_env: dict | None = None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        fd = None
        try:
            if lock:
                lock_path = self.home / ".genesis" / "locks" / "update.lock"
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
                env["DEPLOY_CANDIDATES_LOCK_FD"] = str(fd)
            return dc.main(list(argv), env=env, gh=self.gh, serving=self.serving)
        finally:
            if fd is not None:
                os.close(fd)

    @property
    def manifest_path(self) -> Path:
        return self.home / ".genesis" / "deploy_manifest.json"

    def manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text())

    def write_manifest(self, candidates: list[dict], **extra) -> None:
        common = os.path.realpath(self.root / ".git")
        data = {"version": 1, "repo": common, "candidates": candidates}
        data.update(extra)
        self.manifest_path.write_text(json.dumps(data))

    def entry(self, branch: str, pr: int | None = None, **kw) -> dict:
        e = {
            "branch": branch,
            "pr": pr,
            "owner_session": "s1",
            "added_at": "2026-10-01T00:00:00Z",
            "verified_head": self.rev(f"refs/heads/{branch}"),
            "owner_approved": False,
        }
        e.update(kw)
        return e


@pytest.fixture()
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture()
def ready(dc, world, monkeypatch):
    """Every readiness condition met: the required commits are the scratch base,
    and the serving commit contains them."""
    base = world.rev("refs/remotes/origin/main")
    monkeypatch.setattr(dc, "REQUIRED_SERVING_COMMITS", (("scratch base", base),))
    world.serving_sha = base
    return world


# ── manifest ──────────────────────────────────────────────────────────────


def test_add_writes_a_manifest_that_list_reads_back(dc, ready, capsys):
    w = ready
    head = w.candidate("feat/x", {"x.txt": "x\n"})
    w.gh_states[11] = {
        "state": "OPEN",
        "headRefName": "feat/x",
        "headRefOid": head,
        "author": {"login": "someone"},
    }
    assert w.run(dc, "add", "feat/x", "--pr", "11", "--owner", "sess-a") == 0, capsys.readouterr()
    data = w.manifest()
    assert data["version"] == 1
    assert data["repo"] == os.path.realpath(w.root / ".git")
    [e] = data["candidates"]
    assert (e["branch"], e["pr"], e["owner_session"], e["verified_head"]) == (
        "feat/x",
        11,
        "sess-a",
        head,
    )
    capsys.readouterr()
    assert w.run(dc, "list") == 0
    out = capsys.readouterr().out
    assert "feat/x" in out and "#11" in out and "sess-a" in out


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"version": 2, "repo": "/x", "candidates": []}',
        '{"version": 1, "candidates": []}',
        '{"version": 1, "repo": "/x", "candidates": {}}',
        '{"version": 1, "repo": "REPO", "candidates": [{"branch": "a"}]}',
    ],
)
def test_a_malformed_manifest_refuses_every_mutating_command(dc, ready, capsys, text):
    w = ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    w.manifest_path.write_text(text.replace("REPO", os.path.realpath(w.root / ".git")))
    before = w.manifest_path.read_text()
    for argv, lock in (
        (("add", "feat/x", "--owner", "s"), False),
        (("drop", "feat/x"), True),
        (("rebuild",), True),
    ):
        assert w.run(dc, *argv, lock=lock) == 1, argv
        assert "manifest" in capsys.readouterr().err.lower(), argv
    assert w.manifest_path.read_text() == before
    assert (
        w.git(w.root, "rev-parse", "--verify", "-q", "refs/heads/live", check=False).returncode != 0
    )


@pytest.mark.parametrize("bad", ["-x", "a..b", "live", "main"])
def test_a_manifest_entry_whose_branch_is_not_a_branch_name_is_malformed(dc, ready, capsys, bad):
    w = ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    w.write_manifest([dict(w.entry("feat/x"), branch=bad)])
    assert w.run(dc, "rebuild", lock=True) == 1
    assert "malformed" in capsys.readouterr().err


def test_git_location_variables_from_a_hook_context_do_not_redirect_the_engine(dc, ready, tmp_path):
    """Inside a git hook, GIT_DIR and friends name the hook's repository; the
    engine must still act on its own checkout."""
    w = ready
    ha = w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], env=w.env, check=True)
    extra = {
        "GIT_DIR": str(other / ".git"),
        "GIT_WORK_TREE": str(other),
        "GIT_INDEX_FILE": str(other / ".git" / "index"),
    }
    assert w.run(dc, "rebuild", lock=True, extra_env=extra) == 0
    assert w.live_merges() == [("feat/a", ha)]
    assert (
        subprocess.run(
            ["git", "-C", str(other), "rev-parse", "--verify", "-q", "refs/heads/live"],
            env=w.env,
            capture_output=True,
        ).returncode
        != 0
    )


def test_a_manifest_for_another_repository_is_refused(dc, ready, capsys):
    w = ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    other = w.tmp / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], env=w.env, check=True)
    w.write_manifest([w.entry("feat/x")], repo=str(other))
    assert w.run(dc, "rebuild", lock=True) == 1
    assert "another repository" in capsys.readouterr().err


# ── rebuild ───────────────────────────────────────────────────────────────


def test_rebuild_merges_two_candidates_onto_origin_main(dc, ready, capsys):
    w = ready
    ha = w.candidate("feat/a", {"x.txt": "x\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild", lock=True) == 0, capsys.readouterr()
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "live"
    assert w.live_merges() == [("feat/a", ha), ("feat/b", hb)]
    assert (w.root / "x.txt").read_text() == "x\n" and (w.root / "y.txt").read_text() == "y\n"
    assert (
        w.git(w.root, "merge-base", "--is-ancestor", "refs/remotes/origin/main", "live").returncode
        == 0
    )
    # live tracks origin/main, so "behind" counting keeps working.
    assert w.git(w.root, "rev-parse", "--abbrev-ref", "live@{u}").stdout.strip() == "origin/main"
    # The reflog of live is kept by housekeeping.
    assert (
        w.git(w.root, "config", "--get", "gc.refs/heads/live.reflogExpire").stdout.strip()
        == "never"
    )


def test_rebuild_without_the_lock_is_refused(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 1
    assert "update.lock" in capsys.readouterr().err
    assert (
        w.git(w.root, "rev-parse", "--verify", "-q", "refs/heads/live", check=False).returncode != 0
    )


def test_a_conflicting_candidate_is_excluded_and_the_rest_go_live(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild", lock=True) == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/a .*conflict", out), out
    assert "a.txt" in out
    assert w.live_merges() == [("feat/b", hb)]
    assert (w.root / "a.txt").read_text() == "a1\nMAIN-SIDE\na3\n"


def test_a_candidate_derived_from_an_excluded_one_is_excluded_too(dc, ready, capsys):
    w = ready
    # feat/a: a first commit that merges cleanly, then one that conflicts.
    a1 = w.candidate("feat/a", {"p.txt": "p\n"})
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    # feat/c is stacked on a's FIRST commit: it carries a's unmerged code (p.txt)
    # but nothing that conflicts, so only the derived rule can exclude it.
    w.candidate("feat/c", {"z.txt": "z\n"}, base=a1)
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/c"), w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild", lock=True) == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/a .*conflict", out), out
    assert re.search(r"EXCLUDED: feat/c .*derived from .*feat/a", out), out
    assert w.live_merges() == [("feat/b", hb)]
    assert not (w.root / "p.txt").exists() and not (w.root / "z.txt").exists()


def test_a_dependency_of_an_excluded_candidate_stays_live(dc, ready, capsys):
    """feat/a is stacked ON feat/b: when a is excluded, b's commits are b's own
    code, not a's, so b stays live."""
    w = ready
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"}, base="feat/b")
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/b"), w.entry("feat/a")])
    assert w.run(dc, "rebuild", lock=True) == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/a .*conflict", out), out
    assert "EXCLUDED: feat/b" not in out
    assert w.live_merges() == [("feat/b", hb)]


def test_carries_is_the_derived_dependency_rule(dc):
    assert dc.carries({"a1", "c1"}, {"a1", "a2"}, set())  # stacked on part of it
    assert dc.carries({"a1", "a2", "c1"}, {"a1", "a2"}, set())  # stacked on its head
    assert not dc.carries({"b1"}, {"b1", "a1"}, set())  # it is stacked on this one
    assert not dc.carries({"c1"}, {"a1"}, set())  # unrelated
    # Both stacked on a THIRD candidate: what they share is that candidate's.
    assert not dc.carries({"c1", "b1"}, {"c1", "a1"}, {"c1"})


def test_siblings_stacked_on_a_shared_candidate_do_not_exclude_each_other(dc, ready, capsys):
    w = ready
    hc = w.candidate("feat/c", {"c.txt": "c\n"})
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"}, base="feat/c")
    hb = w.candidate("feat/b", {"y.txt": "y\n"}, base=hc)
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/c"), w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild", lock=True) == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/a .*conflict", out), out
    assert "EXCLUDED: feat/b" not in out and "EXCLUDED: feat/c" not in out, out
    assert w.live_merges() == [("feat/c", hc), ("feat/b", hb)]


def test_a_candidate_with_nothing_beyond_its_base_never_wedges_the_rebuild(dc, ready, capsys):
    """A candidate whose head is already in what it would merge onto (a branch at
    origin/main, or one another candidate already contains) adds no commit: git
    would collapse the duplicate parent into a ONE-parent commit that later
    rebuilds read as foreign."""
    w = ready
    w.git(w.root, "branch", "feat/empty", "origin/main")
    ha = w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/empty"), w.entry("feat/a")])
    assert w.run(dc, "rebuild", lock=True) == 0
    assert "contained: feat/empty" in capsys.readouterr().out
    assert w.live_merges() == [("feat/a", ha)]
    for c in w.git(
        w.root, "rev-list", "refs/heads/live", "--not", "refs/remotes/origin/main"
    ).stdout.split():
        assert len(w.git(w.root, "rev-list", "--parents", "-1", c).stdout.split()) in (3, 2)
    assert w.run(dc, "rebuild", lock=True) == 0
    assert "checkout: unchanged (" in capsys.readouterr().out
    assert w.run(dc, "drop", "feat/empty", lock=True) == 0
    assert w.run(dc, "drop", "feat/a", lock=True) == 0
    assert w.live_merges() == []


def test_a_conflict_with_a_later_excluded_candidate_is_recomputed(dc, ready, capsys):
    """feat/c conflicts only with feat/b; once b is excluded (derived from the
    excluded feat/a), c merges cleanly and must go live."""
    w = ready
    a1 = w.candidate("feat/a", {"p.txt": "p\n"})
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    w.candidate("feat/b", {"q.txt": "from b\n"}, base=a1)
    hc = w.candidate("feat/c", {"q.txt": "from c\n"})
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b"), w.entry("feat/c")])
    assert w.run(dc, "rebuild", lock=True) == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/b .*derived from .*feat/a", out), out
    assert w.live_merges() == [("feat/c", hc)]


def test_live_checked_out_in_another_worktree_refuses_the_rebuild(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    w.git(w.root, "branch", "live", "origin/main")
    other = w.tmp / "live-elsewhere"
    w.git(w.root, "worktree", "add", "-q", str(other), "live")
    before = w.rev("refs/heads/live")
    assert w.run(dc, "rebuild", lock=True) == 1
    assert str(other) in capsys.readouterr().err
    assert w.rev("refs/heads/live") == before
    assert w.git(other, "status", "--porcelain").stdout == ""


def test_a_drop_that_cannot_rebuild_changes_nothing(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild", lock=True) == 0
    (w.root / "b.txt").write_text("edited in place\n")
    before = w.manifest_path.read_text()
    assert w.run(dc, "drop", "feat/a", lock=True) == 1
    assert "b.txt" in capsys.readouterr().err
    assert w.manifest_path.read_text() == before


def test_a_retirement_is_not_saved_when_the_checkout_cannot_move(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.candidate("feat/b", {"local.cfg": "from b\n"})
    w.write_manifest([w.entry("feat/a", pr=1), w.entry("feat/b")])
    w.gh_states[1] = {"state": "MERGED", "mergedAt": "2026-10-01T00:00:00Z", "headRefOid": "x"}
    (w.root / ".git" / "info" / "exclude").write_text("local.cfg\n")
    (w.root / "local.cfg").write_text("install-local\n")
    before = w.manifest_path.read_text()
    assert w.run(dc, "rebuild", lock=True) == 1
    assert w.manifest_path.read_text() == before


def test_drop_without_a_manifest_writes_nothing(dc, world, capsys):
    w = world
    assert w.run(dc, "drop", "feat/a", lock=True) == 1
    assert "not a candidate" in capsys.readouterr().err
    assert sorted(p.name for p in (w.home / ".genesis").iterdir()) == ["locks"]


def test_a_ref_named_like_the_placeholder_does_not_satisfy_readiness(
    dc, world, capsys, monkeypatch
):
    w = world
    base = w.rev("refs/remotes/origin/main")
    pr_c = [r for r in dc.REQUIRED_SERVING_COMMITS if r[1] == dc.PR_C_PLACEHOLDER][0]
    monkeypatch.setattr(dc, "REQUIRED_SERVING_COMMITS", (pr_c,))
    w.serving_sha = base
    w.git(w.root, "branch", dc.PR_C_PLACEHOLDER, "origin/main")
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.run(dc, "add", "feat/x", "--owner", "s") == 1
    assert "PR C" in capsys.readouterr().err


def test_a_lock_fd_this_run_does_not_hold_is_refused(dc, ready, capsys):
    """The fd names update.lock but another process holds it (shared): the
    engine must not proceed on a lock it does not hold."""
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    lock_path = w.home / ".genesis" / "locks" / "update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    mine = os.open(lock_path, os.O_WRONLY)
    try:
        fcntl.flock(holder, fcntl.LOCK_SH)
        assert w.run(dc, "rebuild", extra_env={"DEPLOY_CANDIDATES_LOCK_FD": str(mine)}) == 1
        assert "held by another process" in capsys.readouterr().err
    finally:
        os.close(mine)
        os.close(holder)


def test_status_names_a_candidate_whose_change_is_already_on_main(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    w.advance_main({"x.txt": "x\n"}, "the same change, squash-merged")
    w.git(w.root, "fetch", "-q", "origin")
    assert w.run(dc, "status") == 0
    assert "adds nothing beyond origin/main" in capsys.readouterr().out


def test_drop_names_a_candidate_stacked_on_the_dropped_one(dc, ready, capsys):
    w = ready
    a1 = w.candidate("feat/a", {"p.txt": "p\n"})
    w.candidate("feat/c", {"z.txt": "z\n"}, base=a1)
    w.write_manifest([w.entry("feat/a"), w.entry("feat/c")])
    assert w.run(dc, "drop", "feat/a", "--no-rebuild") == 0
    assert "WARNING: feat/c carries feat/a's unmerged commits" in capsys.readouterr().out


def test_a_linked_worktree_reports_the_main_checkout_and_refuses_changes(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    wt = w.tmp / "wt-feat-a"
    env = {"GENESIS_DEPLOY_CANDIDATES_ROOT": str(wt)}
    assert w.run(dc, "status", extra_env=env) == 0
    assert f"reporting the main checkout, {w.root}" in capsys.readouterr().out
    for argv, lock in (
        (("rebuild",), True),
        (("drop", "feat/a"), True),
        (("add", "feat/a", "--owner", "s"), False),
        (("adopt", "adopt/x", "--owner", "s"), False),
    ):
        assert w.run(dc, *argv, lock=lock, extra_env=env) == 1, argv
        assert "linked worktree" in capsys.readouterr().err, argv
    assert [e["branch"] for e in w.manifest()["candidates"]] == ["feat/a"]


def test_a_merged_pr_retires_its_candidate(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a", pr=1), w.entry("feat/b", pr=2)])
    w.gh_states[1] = {"state": "MERGED", "mergedAt": "2026-10-01T00:00:00Z", "headRefOid": "x"}
    w.gh_states[2] = {"state": "OPEN", "mergedAt": None, "headRefOid": hb}
    assert w.run(dc, "rebuild", lock=True) == 0
    out = capsys.readouterr().out
    assert "retired: feat/a" in out
    assert [e["branch"] for e in w.manifest()["candidates"]] == ["feat/b"]
    assert w.live_merges() == [("feat/b", hb)]


def test_an_unknown_pr_state_retires_nothing(dc, ready, capsys):
    w = ready
    ha = w.candidate("feat/a", {"x.txt": "x\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a", pr=1), w.entry("feat/b", pr=2)])
    w.gh_states[1] = {"state": "MERGED", "mergedAt": "2026-10-01T00:00:00Z", "headRefOid": ha}
    w.gh_fail.add(2)
    assert w.run(dc, "rebuild", lock=True) == 0
    out = capsys.readouterr().out
    assert "unknown" in out and "retired nothing" in out
    assert [e["branch"] for e in w.manifest()["candidates"]] == ["feat/a", "feat/b"]
    assert w.live_merges() == [("feat/a", ha), ("feat/b", hb)]


def test_a_foreign_commit_on_live_refuses_the_rebuild(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild", lock=True) == 0
    capsys.readouterr()
    foreign = w.commit(w.root, {"hand.txt": "by hand\n"}, "a commit made on live by hand")
    tip = w.rev("refs/heads/live")
    assert w.run(dc, "rebuild", lock=True) == 1
    err = capsys.readouterr().err
    assert foreign[:12] in err
    assert w.rev("refs/heads/live") == tip
    # Control: a rebuild after the foreign commit leaves (by a reset the owner
    # makes) goes through.
    w.git(w.root, "reset", "-q", "--hard", "HEAD~1")
    assert w.run(dc, "rebuild", lock=True) == 0


def test_the_rebuild_trailer_is_a_real_trailer_and_pre_push_refuses_it(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild", lock=True) == 0
    tip = w.rev("refs/heads/live")
    trailer = w.git(
        w.root, "log", "-1", "--format=%(trailers:key=Deploy-rebuild,valueonly)", tip
    ).stdout.strip()
    assert trailer, "the Deploy-rebuild trailer must be readable as a git trailer"
    # The merged pre-push hook, invoked directly with git's stdin ref lines,
    # refuses publishing that commit; a branch without it is the control.
    zero = "0" * 40
    hook = _SCRIPTS / "hooks" / "pre-push"
    res = subprocess.run(
        ["bash", str(hook), "fork", "url"],
        input=f"refs/heads/live {tip} refs/heads/live {zero}\n",
        cwd=w.root,
        env=w.env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1 and "Deploy-rebuild" in res.stdout, res.stdout + res.stderr
    head_a = w.rev("refs/heads/feat/a")
    ctl = subprocess.run(
        ["bash", str(hook), "fork", "url"],
        input=f"refs/heads/feat/a {head_a} refs/heads/feat/a {zero}\n",
        cwd=w.root,
        env=w.env,
        capture_output=True,
        text=True,
    )
    assert ctl.returncode == 0, ctl.stdout + ctl.stderr


def test_an_unchanged_set_moves_nothing_and_a_changed_one_checks_out_once(
    dc, ready, capsys, monkeypatch
):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    calls: list[list[str]] = []
    real_run = dc.subprocess.run

    def recording_run(cmd, *a, **kw):
        if cmd and cmd[0] == "git":
            calls.append(list(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(dc.subprocess, "run", recording_run)

    def checkouts():
        return [c for c in calls if any(x in ("switch", "checkout") for x in c[3:5])]

    assert w.run(dc, "rebuild", lock=True) == 0
    assert len(checkouts()) == 1, checkouts()
    tip = w.rev("refs/heads/live")
    reflog_before = w.git(w.root, "reflog", "show", "--format=%H", "refs/heads/live").stdout
    calls.clear()
    capsys.readouterr()
    assert w.run(dc, "rebuild", lock=True) == 0
    assert checkouts() == []
    assert w.rev("refs/heads/live") == tip
    # Not even the ref moved: same origin/main, same candidate heads.
    assert w.git(w.root, "reflog", "show", "--format=%H", "refs/heads/live").stdout == reflog_before
    out = capsys.readouterr().out
    assert "checkout: unchanged (" in out
    # A new candidate changes the tree: exactly one checkout.
    w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    calls.clear()
    assert w.run(dc, "rebuild", lock=True) == 0
    assert len(checkouts()) == 1, checkouts()


def test_the_same_tree_under_new_commits_moves_the_ref_without_a_checkout(
    dc, ready, capsys, monkeypatch
):
    """main absorbs the candidate's exact change (as a squash would): the tree is
    unchanged, so live's ref moves onto the new main without touching the files."""
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild", lock=True) == 0
    w.advance_main({"x.txt": "x\n"}, "squash of feat/a")
    calls: list[list[str]] = []
    real_run = dc.subprocess.run
    monkeypatch.setattr(
        dc.subprocess,
        "run",
        lambda cmd, *a, **kw: (calls.append(list(cmd)), real_run(cmd, *a, **kw))[1],
    )
    assert w.run(dc, "rebuild", lock=True) == 0
    assert not [c for c in calls if any(x in ("switch", "checkout") for x in c[3:5])]
    assert (
        w.git(
            w.root, "merge-base", "--is-ancestor", "refs/remotes/origin/main", "refs/heads/live"
        ).returncode
        == 0
    )
    assert w.git(w.root, "status", "--porcelain").stdout == ""


def test_local_main_is_fast_forwarded(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    new_main = w.advance_main({"m.txt": "m\n"})
    assert w.rev("refs/heads/main") != new_main
    assert w.run(dc, "rebuild", lock=True) == 0
    assert w.rev("refs/heads/main") == new_main
    assert "main: fast-forwarded" in capsys.readouterr().out


def test_local_main_checked_out_in_another_worktree_is_not_moved(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild", lock=True) == 0  # the checkout is on live now; main is free
    old_main = w.rev("refs/heads/main")
    w.git(w.root, "worktree", "add", "-q", str(w.tmp / "main-wt"), "main")
    new_main = w.advance_main({"m.txt": "m\n"})
    capsys.readouterr()
    assert w.run(dc, "rebuild", lock=True) == 0
    out = capsys.readouterr().out
    assert w.rev("refs/heads/main") == old_main != new_main
    assert "not fast-forwarded" in out
    # live itself still advanced onto the new main.
    assert w.git(w.root, "merge-base", "--is-ancestor", new_main, "refs/heads/live").returncode == 0


def test_a_dirty_checkout_refuses_and_names_the_files(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    (w.root / "b.txt").write_text("edited in place\n")
    (w.root / "AGENTS.md").write_text("machine-written\n")  # untracked: not dirty
    assert w.run(dc, "rebuild", lock=True) == 1
    err = capsys.readouterr().err
    assert "b.txt" in err and "adopt" in err
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    # Control: an excused (ephemeral) tracked edit alone does not refuse.
    w.git(w.root, "checkout", "-q", "--", "b.txt")
    w.git(w.root, "rm", "-q", "--cached", "AGENTS.md", check=False)
    (w.root / "AGENTS.md").unlink()
    w.advance_main({"AGENTS.md": "tracked\n"})
    w.git(w.root, "pull", "-q", "--ff-only")
    (w.root / "AGENTS.md").write_text("rewritten by the indexer\n")
    assert w.run(dc, "rebuild", lock=True) == 0, capsys.readouterr()


def test_an_ignored_file_in_the_way_refuses_the_checkout_and_survives(dc, ready, capsys):
    """git overwrites an IGNORED file in the way of a checkout without asking
    (a local settings or secrets file); the move must refuse instead."""
    w = ready
    w.candidate("feat/a", {"local.cfg": "from the candidate\n"})
    w.write_manifest([w.entry("feat/a")])
    (w.root / ".git" / "info" / "exclude").write_text("local.cfg\n")
    (w.root / "local.cfg").write_text("install-local secret\n")
    assert w.run(dc, "rebuild", lock=True) == 1
    assert "local.cfg" in capsys.readouterr().err
    assert (w.root / "local.cfg").read_text() == "install-local secret\n"
    assert w.git(w.root, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"


def test_the_dirty_list_matches_the_deploy_scripts_pipeline(dc, ready):
    """The engine filters the same porcelain the deploy script reads, with the
    same excuse regex, so the two never disagree about a dirty tree."""
    w = ready
    (w.root / "b.txt").write_text("edited\n")
    (w.root / "a.txt").unlink()
    (w.root / "new.txt").write_text("untracked\n")
    shell = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{_SCRIPTS}/lib/deploy_marker.sh"; _status="$(git -C "$1" status --porcelain --no-renames)"; '
            'printf "%s\\n" "$_status" | grep -v "^??" | grep -vE "$EPHEMERAL_DIRTY_RE" | grep -v "^$" || true',
            "_",
            str(w.root),
        ],
        env=w.env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    engine = dc.Engine(w.root, w.env, gh=w.gh, serving=w.serving).dirty_lines()
    assert engine == shell and len(engine) == 2


# ── admission ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "files, why",
    [
        ({"src/genesis/db/migrations/20261001000000_x.py": "x\n"}, "migration"),
        ({"src/genesis/db/data_migrations/d20261001000000_x.py": "x\n"}, "migration"),
        ({"src/genesis/db/schema/_tables.py": "x\n"}, "schema"),
        ({"src/genesis/db/schema/_migrations.py": "x\n"}, "schema"),
        ({"src/genesis/guardian/thing.py": "x\n"}, "guardian"),
        ({"config/genesis-guardian.service": "x\n"}, "guardian"),
        ({"scripts/guardian-gateway.sh": "x\n"}, "guardian"),
        ({"scripts/lib/cc_version.sh": "x\n"}, "Claude Code pin"),
        ({"scripts/cc_align_host.sh": "x\n"}, "reaches the host"),
        ({"scripts/systemd/genesis-cc-align.timer.template": "x\n"}, "reaches the host"),
    ],
)
def test_admission_refuses_what_must_not_go_live(dc, ready, capsys, files, why):
    w = ready
    w.candidate("feat/x", files)
    assert w.run(dc, "add", "feat/x", "--owner", "s") == 1
    assert why in capsys.readouterr().err
    assert not w.manifest_path.exists()


def test_admission_allows_shared_runtime_paths_the_guardian_also_reads(dc, ready, capsys):
    """update.sh redeploys the host guardian when src/genesis/db, util,
    observability, env.py or pyproject.toml change; those are ordinary runtime
    code here and must not be refused (that would refuse most PRs)."""
    w = ready
    w.candidate(
        "feat/x",
        {
            "src/genesis/db/crud/thing.py": "x\n",
            "src/genesis/util/u.py": "u\n",
            "pyproject.toml": "[x]\n",
        },
    )
    assert w.run(dc, "add", "feat/x", "--owner", "s") == 0, capsys.readouterr()


def test_admission_refuses_a_branch_cut_from_live(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild", lock=True) == 0
    w.candidate("feat/bad", {"q.txt": "q\n"}, base="live")
    capsys.readouterr()
    assert w.run(dc, "add", "feat/bad", "--owner", "s") == 1
    assert "Deploy-rebuild" in capsys.readouterr().err


def test_a_dispatched_session_needs_owner_approval(dc, ready, capsys):
    w = ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.run(dc, "add", "feat/x", "--owner", "s", extra_env={"GENESIS_CC_SESSION": "1"}) == 1
    assert "--owner-approved" in capsys.readouterr().err
    assert (
        w.run(
            dc,
            "add",
            "feat/x",
            "--owner",
            "s",
            "--owner-approved",
            extra_env={"GENESIS_CC_SESSION": "1"},
        )
        == 0
    )
    assert w.manifest()["candidates"][0]["owner_approved"] is True


def test_a_devin_authored_pr_needs_owner_approval(dc, ready, capsys):
    w = ready
    head = w.candidate("devin/x", {"x.txt": "x\n"})
    w.gh_states[5] = {
        "state": "OPEN",
        "headRefName": "devin/x",
        "headRefOid": head,
        "author": {"login": "app/devin-ai-integration"},
    }
    assert w.run(dc, "add", "devin/x", "--pr", "5", "--owner", "s") == 1
    assert "--owner-approved" in capsys.readouterr().err
    assert w.run(dc, "add", "devin/x", "--pr", "5", "--owner", "s", "--owner-approved") == 0


def test_add_with_a_pr_whose_head_branch_differs_is_refused(dc, ready, capsys):
    w = ready
    head = w.candidate("feat/x", {"x.txt": "x\n"})
    w.gh_states[5] = {
        "state": "OPEN",
        "headRefName": "feat/other",
        "headRefOid": head,
        "author": {"login": "a"},
    }
    assert w.run(dc, "add", "feat/x", "--pr", "5", "--owner", "s") == 1
    assert "feat/other" in capsys.readouterr().err


def test_the_guardian_rule_accounts_for_every_path_update_sh_redeploys(dc):
    """Every entry of update.sh's GUARDIAN_PATHS is either refused as the
    guardian's own, or named as shared runtime code: adding a path there without
    deciding which fails here."""
    text = (_SCRIPTS / "update.sh").read_text()
    m = re.search(r'^\s*GUARDIAN_PATHS="([^"]+)"', text, re.MULTILINE)
    assert m
    for path in m.group(1).split():
        own = path in dc.GUARDIAN_OWN_FILES or any(
            path.startswith(p) or (path + "/").startswith(p) for p in dc.GUARDIAN_OWN_PREFIXES
        )
        shared = path in dc.GUARDIAN_SHARED_RUNTIME
        assert own != shared, path


# ── readiness ─────────────────────────────────────────────────────────────


def test_readiness_refuses_today_naming_the_unmerged_wiper_change(dc, world, monkeypatch, capsys):
    """B1 ships inert: `add` refuses until PR C (the wipers learn `live`) is on
    the serving commit. Everything else is made ready here, so the refusal can
    only come from that constant."""
    w = world
    base = w.rev("refs/remotes/origin/main")
    pr_c = [r for r in dc.REQUIRED_SERVING_COMMITS if r[1] == dc.PR_C_PLACEHOLDER]
    assert pr_c, "the PR C requirement must be present until PR C fills it in"
    monkeypatch.setattr(dc, "REQUIRED_SERVING_COMMITS", (("scratch base", base), pr_c[0]))
    w.serving_sha = base
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.run(dc, "add", "feat/x", "--owner", "s") == 1
    err = capsys.readouterr().err
    assert "PR C" in err and "scratch base" not in err
    assert not w.manifest_path.exists()
    assert not (w.home / ".genesis" / "deploy_manifest.json.lock").exists()


def test_the_pr_c_requirement_does_not_resolve_in_this_repository(dc):
    """The shipped constant cannot name a real commit, so the engine is inert on
    every install until PR C replaces it with that PR's merge commit."""
    res = subprocess.run(
        ["git", "-C", str(_REPO), "rev-parse", "--verify", "-q", dc.PR_C_PLACEHOLDER + "^{commit}"],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert any(ref == dc.PR_C_PLACEHOLDER for _, ref in dc.REQUIRED_SERVING_COMMITS)


@pytest.mark.parametrize("what", ["serving-unknown", "serving-old", "hook-stale", "hook-missing"])
def test_readiness_refuses_each_unmet_condition(dc, ready, capsys, monkeypatch, what):
    w = ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    if what == "serving-unknown":
        w.serving_sha = None
        needle = "serving commit is unknown"
    elif what == "serving-old":
        # The required commit exists but the server booted from an older one.
        newer = w.advance_main({"m.txt": "m\n"})
        w.git(w.root, "fetch", "-q", "origin")
        monkeypatch.setattr(dc, "REQUIRED_SERVING_COMMITS", (("newer main", newer),))
        needle = "newer main"
    elif what == "hook-stale":
        (w.root / ".git" / "hooks" / "pre-push").write_text("#!/bin/sh\nexit 0\n# old\n")
        needle = "pre-push"
    else:
        (w.root / ".git" / "hooks" / "pre-merge-commit").unlink()
        needle = "pre-merge-commit"
    assert w.run(dc, "add", "feat/x", "--owner", "s") == 1
    assert needle in capsys.readouterr().err
    assert not w.manifest_path.exists()


def test_the_hook_list_is_read_from_sync_hooks(dc):
    assert dc.hooks_to_check(_REPO) == list(_HOOK_NAMES)


# ── drop / status ─────────────────────────────────────────────────────────


def test_drop_removes_the_candidate_and_rebuilds_without_it(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild", lock=True) == 0
    assert w.run(dc, "drop", "feat/a", lock=True) == 0
    assert [e["branch"] for e in w.manifest()["candidates"]] == ["feat/b"]
    assert w.live_merges() == [("feat/b", hb)]
    assert not (w.root / "x.txt").exists()
    capsys.readouterr()
    assert w.run(dc, "drop", "feat/zzz", lock=True) == 1
    assert "not a candidate" in capsys.readouterr().err


def test_status_reports_live_excluded_stale_and_unverified(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a", pr=1), w.entry("feat/b", pr=2)])
    w.gh_states[1] = {"state": "OPEN", "updatedAt": "2026-01-01T00:00:00Z", "mergedAt": None}
    w.gh_states[2] = {"state": "OPEN", "updatedAt": "2099-01-01T00:00:00Z", "mergedAt": None}
    assert w.run(dc, "rebuild", lock=True) == 0
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.git(w.root, "fetch", "-q", "origin")
    w.candidate("feat/b", {"y.txt": "y2\n"}, msg="rework")
    capsys.readouterr()
    assert w.run(dc, "status") == 0
    out = capsys.readouterr().out
    block_a = out.split("feat/a", 1)[1].split("feat/b", 1)[0]
    block_b = out.split("feat/b", 1)[1]
    assert "live at" in block_a and "EXCLUDED" in block_a and "stale" in block_a
    assert "live at" in block_b and "unverified since rework" in block_b and "stale" not in block_b


# ── adopt ─────────────────────────────────────────────────────────────────


def test_adopt_snapshots_the_dirty_edits_and_classifies_each_file(dc, ready, capsys):
    w = ready
    # The checkout sits behind main, with edits of three kinds.
    w.advance_main({"a.txt": "a1\nmerged\na3\n"})
    w.git(w.root, "fetch", "-q", "origin")
    pr_head = w.candidate("feat/p", {"b.txt": "from the PR\n"})
    w.gh_states[9] = {"state": "OPEN", "headRefOid": pr_head, "headRefName": "feat/p"}
    w.git(w.root, "update-ref", "refs/pull/9/head", pr_head)
    (w.root / "a.txt").write_text("a1\nmerged\na3\n")  # equals origin/main
    (w.root / "b.txt").write_text("from the PR\n")  # equals PR #9's head
    (w.root / "hand.txt").write_text("untracked\n")  # untracked: not adopted
    w.git(w.root, "add", "-N", "hand.txt")  # intent-to-add: tracked now
    (w.root / "AGENTS.md").write_text("x\n")
    dirty_tree = _work_tree_id(w)
    assert w.run(dc, "adopt", "adopt/live-edits", "--owner", "sess-z") == 0, capsys.readouterr()
    out = capsys.readouterr().out
    snap = w.rev("refs/heads/adopt/live-edits")
    assert w.rev(f"{snap}^{{tree}}") == dirty_tree
    assert w.rev(f"{snap}^") == w.rev("HEAD")
    assert re.search(r"a\.txt\s+equals origin/main", out), out
    assert re.search(r"b\.txt\s+equals PR #9", out), out
    assert re.search(r"hand\.txt\s+neither", out), out
    # The checkout itself is untouched.
    assert (w.root / "b.txt").read_text() == "from the PR\n"
    capsys.readouterr()
    assert w.run(dc, "adopt", "adopt/live-edits", "--owner", "sess-z") == 1
    assert "exists" in capsys.readouterr().err


def _work_tree_id(w: World) -> str:
    """The tree of HEAD plus every tracked working-tree edit, via a scratch index."""
    idx = w.tmp / "probe.index"
    env = dict(w.env, GIT_INDEX_FILE=str(idx))
    subprocess.run(["git", "-C", str(w.root), "read-tree", "HEAD"], env=env, check=True)
    subprocess.run(["git", "-C", str(w.root), "add", "-u"], env=env, check=True)
    subprocess.run(["git", "-C", str(w.root), "add", "hand.txt"], env=env, check=True)
    return subprocess.run(
        ["git", "-C", str(w.root), "write-tree"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_adopt_reads_paths_literally_never_as_patterns(dc, ready, capsys):
    """A tracked file named `w*` is a path, not a glob: the snapshot must not
    sweep in the untracked `wz.txt` the pattern would match."""
    w = ready
    w.commit(w.root, {"w*": "one\n"}, "a file with a glob character in its name")
    (w.root / "w*").write_text("two\n")
    (w.root / "wz.txt").write_text("untracked\n")
    assert w.run(dc, "adopt", "adopt/glob", "--owner", "s") == 0, capsys.readouterr()
    names = w.git(w.root, "ls-tree", "--name-only", "refs/heads/adopt/glob").stdout.split("\n")
    assert "w*" in names and "wz.txt" not in names


def test_a_dependency_that_gained_commits_reads_as_carrying_the_excluded_code(dc, ready, capsys):
    """feat/a was cut from feat/b's first commit, then b gained one. Git cannot
    tell this from b having been cut from a's first commit, so b reads as
    carrying a's code and goes out with it: the conservative side, reported by
    name. Pinned so that changing it is a decision, not a drift."""
    w = ready
    b1 = w.candidate("feat/b", {"y.txt": "y\n"})
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"}, base=b1)
    w.candidate("feat/b", {"y2.txt": "y2\n"})
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.write_manifest([w.entry("feat/b"), w.entry("feat/a")])
    assert w.run(dc, "rebuild", lock=True) == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/b .*derived from .*feat/a", out), out


def test_adopt_with_nothing_dirty_refuses(dc, ready, capsys):
    w = ready
    assert w.run(dc, "adopt", "adopt/x", "--owner", "s") == 1
    assert "nothing to adopt" in capsys.readouterr().err


# ── the shell entry ───────────────────────────────────────────────────────


def _plain_shell(w: World, *argv: str) -> subprocess.CompletedProcess:
    """`env -i`: no PATH, no venv, no Claude Code environment. HOME and the root
    seam are the only variables, so nothing can reach the real install."""
    env = [
        "HOME=" + str(w.home),
        "GENESIS_DEPLOY_CANDIDATES_ROOT=" + str(w.root),
        "GIT_CONFIG_NOSYSTEM=1",
    ]
    return subprocess.run(
        ["env", "-i", *env, "/bin/bash", str(ENTRY), *argv], capture_output=True, text=True
    )


def test_drop_list_and_status_work_from_a_plain_shell(dc, ready, capsys):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild", lock=True) == 0
    res = _plain_shell(w, "list")
    assert res.returncode == 0 and "feat/a" in res.stdout, res.stdout + res.stderr
    res = _plain_shell(w, "status")
    assert res.returncode == 0 and "feat/b" in res.stdout, res.stdout + res.stderr
    res = _plain_shell(w, "drop", "feat/a")
    assert res.returncode == 0, res.stdout + res.stderr
    assert w.live_merges() == [("feat/b", hb)]
    # The entry released the deploy marker and the lock.
    assert not (w.home / ".genesis" / "update_in_progress.pid").exists()


def test_the_entry_queues_on_the_update_lock(dc, ready):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    lock_path = w.home / ".genesis" / "locks" / "update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)  # a validation's shared hold
        res = _plain_shell(w, "rebuild", "--wait", "1")
        assert res.returncode == 200, res.stdout + res.stderr
        assert (
            w.git(w.root, "rev-parse", "--verify", "-q", "refs/heads/live", check=False).returncode
            != 0
        )
    finally:
        os.close(fd)
    res = _plain_shell(w, "rebuild", "--wait", "1")
    assert res.returncode == 0, res.stdout + res.stderr


def test_the_entry_refuses_a_live_foreign_deploy_marker(dc, ready):
    w = ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    holder = subprocess.Popen(["sleep", "30"])
    try:
        (w.home / ".genesis" / "update_in_progress.pid").write_text(f"{holder.pid}\n")
        res = _plain_shell(w, "rebuild", "--wait", "1")
        assert res.returncode == 1 and "deploy marker" in res.stderr, res.stdout + res.stderr
    finally:
        holder.kill()
        holder.wait()


def test_the_engine_sources_the_ephemeral_regex_never_defines_it():
    for path in (ENTRY, ENGINE):
        text = path.read_text()
        assert "EPHEMERAL_DIRTY_RE=" not in text.replace('"$EPHEMERAL_DIRTY_RE"', ""), path.name
    assert re.search(r'^\. "\$_SELF_DIR/lib/deploy_marker\.sh"$', ENTRY.read_text(), re.MULTILINE)


def test_the_manifest_the_engine_writes_arms_the_hooks_for_this_repository_only(
    dc, ready, tmp_path
):
    """The real pre-commit hook refuses a commit on `live` in the repository the
    engine's manifest names, and leaves another clone's `live` alone."""
    w = ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.run(dc, "add", "feat/x", "--owner", "s") == 0
    hook = _SCRIPTS / "hooks" / "pre-commit"

    def commit_on_live(repo: Path) -> subprocess.CompletedProcess:
        w.git(repo, "checkout", "-q", "-B", "live")
        (repo / "z.txt").write_text("z\n")
        w.git(repo, "add", "z.txt")
        return subprocess.run(
            ["bash", str(hook)], cwd=repo, env=w.env, capture_output=True, text=True
        )

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", str(w.origin), str(other)],
        env=w.env,
        check=True,
        capture_output=True,
    )
    assert commit_on_live(w.root).returncode == 1
    assert commit_on_live(other).returncode == 0
