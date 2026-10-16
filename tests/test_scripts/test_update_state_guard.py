"""scripts/lib/update_state_guard.py: whether update_state.json still guards (#3139).

One rule, asked by update.sh, bootstrap.sh, deploy_code_only.sh, the live engine
and the dashboard. These tests run the real module (loaded per test, so a missing
module fails a test instead of collection) and its command line against scratch
repositories.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "lib" / "update_state_guard.py"

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + bash")


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_git_env()
    ).stdout.strip()


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location("update_state_guard_under_test", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path: Path) -> dict:
    """A checkout at tag `pre` (HEAD), and `incoming`, a commit it has not merged."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "base")
    _git(root, "tag", "pre")
    base = _git(root, "rev-parse", "HEAD")
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-qm", "incoming")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "-q", "--hard", base)
    home = tmp_path / "home"
    home.mkdir()
    return {"root": root, "base": base, "incoming": incoming, "home": home}


def _state(r: dict, content) -> Path:
    path = r["home"] / "update_state.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def _verdict(guard, r: dict, content, conflicts=None) -> str:
    path = _state(r, content)
    if conflicts is not None:
        (r["home"] / "update_conflicts.json").write_text(json.dumps(conflicts))
    return guard.verdict(str(path), str(r["root"]))[0]


def test_no_file_guards_nothing(guard, repo):
    assert guard.verdict(str(repo["home"] / "update_state.json"), str(repo["root"]))[0] == "safe"


@pytest.mark.parametrize(
    "content",
    ["{not json", "[1, 2]", '"text"', "{}", '{"pid": 1}', '{"phase": 7}'],
    ids=["malformed", "array", "string", "empty-object", "no-phase", "non-string-phase"],
)
def test_a_state_that_cannot_be_judged_guards(guard, repo, content):
    assert _verdict(guard, repo, content) == "guards"


def test_done_guards_nothing(guard, repo):
    assert _verdict(guard, repo, {"phase": "done"}) == "safe"


def test_fetching_at_the_tag_guards_nothing(guard, repo):
    state = {
        "phase": "fetching",
        "rollback_tag": "pre",
        "deploy_head": repo["incoming"],
        "post_merge": False,
    }
    assert _verdict(guard, repo, state) == "safe"


@pytest.mark.parametrize(
    "state",
    [
        {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "", "post_merge": False},
        {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "x", "post_merge": True},
        {"phase": "fetching", "rollback_tag": "gone", "deploy_head": "x"},
        {"phase": "fetching", "deploy_head": "x"},
    ],
    ids=["no-deploy-head", "post-merge", "missing-tag", "no-tag"],
)
def test_a_fetching_state_that_may_cover_merged_code_guards(guard, repo, state):
    """SF1: a --post-merge writes "fetching" over code that is already merged,
    and may have made its fallback tag at that merged HEAD."""
    assert _verdict(guard, repo, state) == "guards"


def test_fetching_after_head_moved_guards(guard, repo):
    _git(repo["root"], "merge", "-q", "--ff-only", repo["incoming"])
    state = {
        "phase": "fetching",
        "rollback_tag": "pre",
        "deploy_head": repo["incoming"],
        "post_merge": False,
    }
    assert _verdict(guard, repo, state) == "guards"


def test_merging_after_the_merge_guards(guard, repo):
    _git(repo["root"], "merge", "-q", "--ff-only", repo["incoming"])
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state) == "guards"


def test_a_conflict_exit_is_conflict_pending(guard, repo):
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state, {"deploy_head": repo["incoming"]}) == "conflict_pending"


def test_a_kill_before_the_merge_is_not_a_conflict(guard, repo):
    """Review 4255463476: "merging" at the tag without a recorded conflict is a
    run killed between writing "merging" and its merge; nothing merged."""
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state) == "safe"


def test_a_conflict_file_for_another_commit_is_not_this_conflict(guard, repo):
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state, {"deploy_head": "0" * 40}) == "safe"


@pytest.mark.parametrize("path", ["a.txt", "AGENTS.md"], ids=["tracked", "ephemeral"])
def test_merging_at_the_tag_with_tracked_changes_guards(guard, repo, path):
    """The bootstrap residue check: every tracked change counts, an ephemeral
    path included (a half-written fast-forward writes those too)."""
    if path == "AGENTS.md":
        (repo["root"] / path).write_text("x\n")
        _git(repo["root"], "add", path)
        _git(repo["root"], "commit", "-qm", "agents")
        _git(repo["root"], "tag", "-f", "pre")
    (repo["root"] / path).write_text("edited\n")
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state, {"deploy_head": repo["incoming"]}) == "guards"


def test_merging_at_the_tag_with_an_added_file_guards(guard, repo):
    """A fast-forward killed after writing only an added file leaves it untracked."""
    (repo["root"] / "b.txt").write_text("b\n")
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state) == "guards"


def test_a_merge_in_progress_guards(guard, repo):
    _git(repo["root"], "checkout", "-q", "-b", "side")
    (repo["root"] / "a.txt").write_text("side\n")
    _git(repo["root"], "commit", "-qam", "side")
    _git(repo["root"], "checkout", "-q", "main")
    (repo["root"] / "a.txt").write_text("main\n")
    _git(repo["root"], "commit", "-qam", "main")
    _git(repo["root"], "tag", "-f", "pre")
    # The conflicting merge exits non-zero; the identity env lets git start it on
    # any machine (no global identity needed), and MERGE_HEAD proves it stopped.
    r = subprocess.run(
        ["git", "-C", str(repo["root"]), "merge", "-q", "side"],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    assert (repo["root"] / ".git" / "MERGE_HEAD").exists(), r.stdout + r.stderr
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state) == "guards"


@pytest.mark.parametrize("phase", ["bootstrap", "migrations", "health_check", "merged"])
def test_later_or_unknown_phases_guard(guard, repo, phase):
    assert _verdict(guard, repo, {"phase": phase, "rollback_tag": "pre"}) == "guards"


def test_an_inherited_git_dir_does_not_redirect_it(guard, repo, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    state = {
        "phase": "fetching",
        "rollback_tag": "pre",
        "deploy_head": repo["incoming"],
        "post_merge": False,
    }
    assert _verdict(guard, repo, state) == "safe"


@pytest.mark.parametrize(
    "content,conflict,rc",
    [
        ({"phase": "done"}, False, 10),
        ({"phase": "bootstrap"}, False, 0),
        ({"phase": "merging", "rollback_tag": "pre"}, True, 3),
    ],
    ids=["safe", "guards", "conflict"],
)
def test_the_command_line(repo, content, conflict, rc):
    if "deploy_head" not in content and content["phase"] == "merging":
        content = {**content, "deploy_head": repo["incoming"]}
    path = _state(repo, content)
    if conflict:
        (repo["home"] / "update_conflicts.json").write_text(
            json.dumps({"deploy_head": repo["incoming"]})
        )
    r = subprocess.run(
        [sys.executable, "-I", "-S", str(GUARD), str(path), str(repo["root"])],
        capture_output=True,
        text=True,
    )
    assert r.returncode == rc, r.stdout + r.stderr
    if rc == 0:
        assert "--post-merge" in r.stdout and "Dismiss" in r.stdout
    if rc == 3:
        assert "merge conflicts" in r.stdout and "Resolve" in r.stdout


@pytest.mark.parametrize(
    "post_merge",
    ["absent", "true", None, 0],
    ids=["no-key", "string-true", "null", "zero"],
)
def test_only_an_explicit_plain_run_fetching_state_is_safe(guard, repo, post_merge):
    """SF-B: a state from before the key existed, or a hand edit, may be a
    --post-merge's "fetching" over merged code at a fallback tag made at HEAD."""
    state = {"phase": "fetching", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    if post_merge != "absent":
        state["post_merge"] = post_merge
    assert _verdict(guard, repo, state) == "guards"
    state["post_merge"] = False
    assert _verdict(guard, repo, state) == "safe", "control: an explicit plain run"


@pytest.mark.parametrize(
    "record", ["{not json", "[1, 2]", '"text"'], ids=["malformed", "array", "string"]
)
def test_a_conflict_record_that_cannot_be_read_keeps_the_conflict(guard, repo, record):
    """N1: doubt about the conflict record keeps the resolver's anchor."""
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    path = _state(repo, state)
    (repo["home"] / "update_conflicts.json").write_text(record)
    assert guard.verdict(str(path), str(repo["root"]))[0] == "conflict_pending"


def _cli(repo, path, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-S", str(GUARD), str(path), str(repo["root"])],
        capture_output=True,
        text=True,
        env=env,
    )


def test_a_check_that_cannot_run_git_guards(repo, tmp_path):
    """SF-A: git not on PATH used to raise OSError, and Python's exit 1 for an
    uncaught exception was the old "safe" code."""
    path = _state(
        repo, {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    )
    empty = tmp_path / "no-tools"
    empty.mkdir()
    r = _cli(repo, path, env={"PATH": str(empty), "HOME": str(repo["home"])})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "could not be checked" in r.stdout and "FileNotFoundError" in r.stdout


def test_a_state_nested_too_deeply_to_parse_guards(repo):
    path = repo["home"] / "update_state.json"
    path.write_text("[" * 200_000 + "]" * 200_000)
    r = _cli(repo, path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_safe_is_a_code_no_default_failure_produces():
    """Python's uncaught exception (1), a usage error (2) and a shell's cannot-run
    codes (126, 127) must never mean safe."""
    text = GUARD.read_text()
    assert "EXIT_CODES = {GUARDS: 0, SAFE: 10, CONFLICT_PENDING: 3}" in text


def test_a_hung_git_guards_and_returns(guard, repo, tmp_path, monkeypatch):
    """A git that never returns (a wedged filesystem) must not hold the caller
    forever (the dashboard holds update.lock while it asks): the call times
    out, and a check that could not finish keeps the state."""
    path = _state(
        repo,
        {
            "phase": "fetching",
            "rollback_tag": "pre",
            "deploy_head": repo["incoming"],
            "post_merge": False,
        },
    )
    hang = tmp_path / "hang-bin"
    hang.mkdir()
    fake = hang / "git"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{hang}:{os.environ['PATH']}")
    monkeypatch.setattr(guard, "_TIMEOUT_S", 0.5, raising=False)
    started = time.monotonic()
    assert guard.main([str(path), str(repo["root"])]) == guard.EXIT_CODES[guard.GUARDS]
    assert time.monotonic() - started < 10, "the git call was not bounded"
