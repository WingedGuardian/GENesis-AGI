"""update.sh refuses to overwrite an earlier run's restart guard (#3139, #3138).

An earlier run that merged code but never activated it leaves
~/.genesis/update_state.json, and deploy_code_only.sh and the live engine refuse
to restart while it stands. A plain run used to overwrite it with its own
"fetching" state and then delete it on its nothing-to-do exit. The
``# BEGIN/END prior-state-guard`` block now asks scripts/lib/update_state_guard.py
first. These tests run that real block with the real guard against a scratch
repository.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UPDATE = REPO / "scripts" / "update.sh"
GUARD = REPO / "scripts" / "lib" / "update_state_guard.py"


def _block() -> str:
    text = UPDATE.read_text()
    return text[text.index("# BEGIN prior-state-guard") : text.index("# END prior-state-guard")]


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "scripts" / "lib" / "update_state_guard.py").write_text(GUARD.read_text())
    _git(root, "init", "-q", "-b", "trunk")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "tag", "at-head")
    (root / "x").write_text("x")
    _git(root, "add", "x")
    _git(root, "commit", "-qm", "second")
    _git(root, "tag", "at-second")
    _git(root, "checkout", "-q", "at-head")
    return root


def _run(repo: Path, state: Path, *, post_merge: bool = False) -> subprocess.CompletedProcess:
    script = f"""
set -Eeuo pipefail
GENESIS_ROOT={str(repo)!r}; SCRIPT_DIR="$GENESIS_ROOT/scripts"
STATE_FILE={str(state)!r}; POST_MERGE={"true" if post_merge else "false"}
{_block()}
echo PASSED_GUARD
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)


def _state(tmp_path: Path, content: object | None) -> Path:
    path = tmp_path / "update_state.json"
    if content is not None:
        path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


@pytest.mark.parametrize(
    "content",
    [
        None,
        {"phase": "done"},
        {"phase": "fetching", "rollback_tag": "at-head"},
    ],
    ids=["absent", "done", "fetching-nothing-merged"],
)
def test_a_state_that_guards_nothing_lets_the_run_proceed(repo, tmp_path, content):
    state = _state(tmp_path, content)
    r = _run(repo, state)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASSED_GUARD" in r.stdout


@pytest.mark.parametrize(
    "content",
    [
        {"phase": "merging", "rollback_tag": "at-second"},
        {"phase": "bootstrap", "rollback_tag": "at-head"},
        {"phase": "fetching", "rollback_tag": "at-second"},
        {"phase": "fetching"},
        {"rollback_tag": "at-head"},
        "not json",
        "[1, 2]",
    ],
    ids=[
        "merging",
        "bootstrap",
        "fetching-moved",
        "fetching-no-tag",
        "no-phase",
        "garbage",
        "list",
    ],
)
def test_a_guarding_state_refuses_before_anything_and_survives(repo, tmp_path, content):
    state = _state(tmp_path, content)
    before = state.read_bytes()
    r = _run(repo, state)
    assert r.returncode == 1
    assert "PASSED_GUARD" not in r.stdout
    assert "--post-merge" in r.stdout
    assert state.read_bytes() == before


def test_a_conflict_exit_is_kept_and_names_its_own_remedy(repo, tmp_path):
    # update.sh's conflict exit aborts the merge (HEAD back at the tag) and keeps
    # "merging" as the resolver's anchor. Nothing merged, so --post-merge is the
    # wrong advice; the file is still kept.
    state = _state(tmp_path, {"phase": "merging", "rollback_tag": "at-head"})
    before = state.read_bytes()
    r = _run(repo, state)
    assert r.returncode == 1 and "PASSED_GUARD" not in r.stdout
    assert "merge conflicts" in r.stdout and "--post-merge" not in r.stdout
    assert state.read_bytes() == before


def test_post_merge_is_the_run_that_finishes_it(repo, tmp_path):
    state = _state(tmp_path, {"phase": "merging", "rollback_tag": "at-second"})
    r = _run(repo, state, post_merge=True)
    assert r.returncode == 0 and "PASSED_GUARD" in r.stdout


def test_a_guard_that_cannot_run_refuses(repo, tmp_path):
    (repo / "scripts" / "lib" / "update_state_guard.py").unlink()
    state = _state(tmp_path, {"phase": "done"})
    r = _run(repo, state)
    assert r.returncode == 1 and "PASSED_GUARD" not in r.stdout


def test_the_guard_runs_under_the_lock_and_before_any_state_write():
    text = UPDATE.read_text()
    guard = text.index("# BEGIN prior-state-guard")
    assert guard > text.index('if ! flock -n "$_UPDATE_LOCK_FD"; then')
    assert guard < text.index('\n_write_state "fetching"')
