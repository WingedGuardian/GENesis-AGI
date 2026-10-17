"""update.sh never overwrites or deletes a state file another run left (#3139).

An earlier run that merged code it never activated, or that stopped on merge
conflicts, leaves ~/.genesis/update_state.json. These tests run the real blocks
and functions of scripts/update.sh against scratch repositories:

* ``prior-state-guard``: a plain run refuses (exit 5) over a guarding state,
  before anything is tagged or written; ``--post-merge`` is the run that finishes it;
* ``_clear_deploy_state``: an exit deletes the state only when this run wrote it
  and is not a ``--post-merge`` (N1);
* ``_drop_own_prestop_state``: a plain run's own "fetching" state goes on every
  exit (the dashboard poll no longer deletes it);
* ``_write_state``: the state says whether a ``--post-merge`` wrote it;
* ``rollback-state-clear``: a rollback that left merged code keeps the state.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UPDATE = REPO / "scripts" / "update.sh"
MARKER_LIB = REPO / "scripts" / "lib" / "deploy_marker.sh"

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="bash + git")


def _text() -> str:
    return UPDATE.read_text()


def _block(name: str) -> str:
    text = _text()
    begin = text.index(f"# BEGIN {name}")
    return text[begin : text.index(f"# END {name}", begin)]


def _function(name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", _text(), re.DOTALL | re.MULTILINE)
    assert m, f"missing {name}()"
    return m.group(0)


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
def world(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "commit", "-q", "--allow-empty", "-m", "base")
    _git(root, "tag", "pre")
    head = _git(root, "rev-parse", "HEAD")
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    return {
        "root": root,
        "home": home,
        "head": head,
        "state": home / ".genesis" / "update_state.json",
    }


def _bash(script: str, home: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GENESIS_"))}
    env["HOME"] = str(home)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


# ── prior-state-guard ────────────────────────────────────────────────────────


def _guard_run(w: dict, *, post_merge: bool = False, scripts: Path | None = None):
    script = (
        "set -Eeuo pipefail\n"
        f'GENESIS_ROOT="{w["root"]}"\nSCRIPT_DIR="{scripts or REPO / "scripts"}"\n'
        f'STATE_FILE="{w["state"]}"\nPOST_MERGE={"true" if post_merge else "false"}\n'
        + _block("prior-state-guard")
        + "echo PASSED_GUARD\n"
    )
    return _bash(script, w["home"])


@pytest.mark.parametrize(
    "content",
    [
        {"phase": "bootstrap", "rollback_tag": "pre"},
        {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "x", "post_merge": True},
        "[1, 2]",
        "{not json",
        {"rollback_tag": "pre"},
    ],
    ids=["bootstrap", "post-merge-fetching", "array", "malformed", "no-phase"],
)
def test_a_guarding_state_refuses_before_anything_and_survives(world, content):
    world["state"].write_text(content if isinstance(content, str) else json.dumps(content))
    before = world["state"].read_bytes()
    r = _guard_run(world)
    assert r.returncode == 5, r.stdout + r.stderr
    assert "PASSED_GUARD" not in r.stdout
    assert "--post-merge" in r.stdout and "Dismiss" in r.stdout
    assert world["state"].read_bytes() == before


def test_a_conflict_exit_refuses_with_the_conflict_remedy(world):
    world["state"].write_text(
        json.dumps({"phase": "merging", "rollback_tag": "pre", "deploy_head": world["head"]})
    )
    (world["home"] / ".genesis" / "update_conflicts.json").write_text(
        json.dumps({"deploy_head": world["head"]})
    )
    r = _guard_run(world)
    assert r.returncode == 5, r.stdout + r.stderr
    assert "merge conflicts" in r.stdout and "Resolve" in r.stdout


@pytest.mark.parametrize(
    "content",
    [
        None,
        {"phase": "done"},
        {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "abc", "post_merge": False},
        {"phase": "merging", "rollback_tag": "pre", "deploy_head": "HEAD-SHA"},
    ],
    ids=["absent", "done", "fetching-nothing-merged", "merging-killed-before-merge"],
)
def test_a_state_that_guards_nothing_lets_the_run_proceed(world, content):
    if content is not None:
        if content.get("deploy_head") == "HEAD-SHA":
            content = {**content, "deploy_head": world["head"]}
        world["state"].write_text(json.dumps(content))
    r = _guard_run(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASSED_GUARD" in r.stdout


def test_post_merge_is_the_run_that_finishes_it(world):
    world["state"].write_text(json.dumps({"phase": "bootstrap", "rollback_tag": "pre"}))
    r = _guard_run(world, post_merge=True)
    assert r.returncode == 0 and "PASSED_GUARD" in r.stdout


def test_a_guard_that_cannot_run_refuses_only_when_a_state_exists(world, tmp_path):
    empty = tmp_path / "no-scripts"
    empty.mkdir()
    r = _guard_run(world, scripts=empty)
    assert r.returncode == 0 and "PASSED_GUARD" in r.stdout, "no state, nothing to check"
    world["state"].write_text(json.dumps({"phase": "done"}))
    r = _guard_run(world, scripts=empty)
    assert r.returncode == 5 and "PASSED_GUARD" not in r.stdout


def test_the_guard_runs_under_the_lock_before_any_tag_or_state_write():
    text = _text()
    guard = text.index("# BEGIN prior-state-guard")
    assert guard > text.index("# END live-lock-recheck")
    assert guard > text.index('if ! flock -n "$_UPDATE_LOCK_FD"; then')
    assert guard < text.index('ROLLBACK_TAG="pre-update-')
    assert guard < text.index('\n_write_state "fetching"')
    assert guard < text.index("# BEGIN live-marker")


# ── what an exit deletes ───────────────────────────────────────────────────────


def _cleanup_harness(w: dict, steps: str, *, post_merge: bool = False, on_live: bool = False):
    script = (
        "set -Eeuo pipefail\n"
        f'STATE_FILE="{w["state"]}"\nSTARTED_AT=now\nWERE_RUNNING=()\n'
        f"POST_MERGE={'true' if post_merge else 'false'}\nON_LIVE={1 if on_live else 0}\n"
        f'ROLLBACK_TAG=pre\nDEPLOY_HEAD="{w["head"]}"\n'
        f'. "{MARKER_LIB}"\n'
        + '_STATE_WRITTEN=0\n_STATE_PHASE=""\n'
        + _function("_write_state")
        + _function("_clear_deploy_marker")
        + _function("_clear_deploy_state")
        + _function("_drop_own_prestop_state")
        + steps
    )
    return _bash(script, w["home"])


def _other_runs_state(w: dict) -> bytes:
    w["state"].write_text(json.dumps({"phase": "merging", "rollback_tag": "pre", "pid": 7}))
    return w["state"].read_bytes()


def test_an_exit_before_this_run_wrote_keeps_another_runs_state(world):
    """N1: the pre-write exits (a failed fetch, a collision, a failed backup) used
    to delete whatever state file was on disk."""
    before = _other_runs_state(world)
    r = _cleanup_harness(world, "_clear_deploy_state\n")
    assert r.returncode == 0, r.stderr
    assert world["state"].read_bytes() == before


def test_a_state_this_run_wrote_goes(world):
    r = _cleanup_harness(world, '_write_state "fetching"\n_clear_deploy_state\n')
    assert r.returncode == 0, r.stderr
    assert not world["state"].exists()


def test_a_post_merge_never_deletes_the_state_it_finishes(world):
    r = _cleanup_harness(world, '_write_state "fetching"\n_clear_deploy_state\n', post_merge=True)
    assert r.returncode == 0, r.stderr
    assert world["state"].exists()


def test_write_state_records_which_kind_of_run_wrote_it(world):
    _cleanup_harness(world, '_write_state "fetching"\n', post_merge=True)
    assert json.loads(world["state"].read_text())["post_merge"] is True
    _cleanup_harness(world, '_write_state "fetching"\n')
    state = json.loads(world["state"].read_text())
    assert state["post_merge"] is False and state["deploy_head"] == world["head"]


@pytest.mark.parametrize(
    "steps,post_merge,on_live,kept",
    [
        ('_write_state "fetching"\n', False, False, False),
        ('_write_state "fetching"\n_write_state "merging"\n', False, False, True),
        ('_write_state "fetching"\n', True, False, True),
        ('_write_state "fetching"\n', False, True, True),
        ("", False, False, True),
    ],
    ids=["own-fetching", "own-merging", "post-merge", "on-live", "never-written"],
)
def test_the_exit_trap_drops_only_its_own_prestop_state(world, steps, post_merge, on_live, kept):
    """SF3: the exits after the "fetching" write (a failed DB snapshot, a server
    that will not stop) left it behind, and the poll no longer deletes it."""
    if not steps:
        _other_runs_state(world)
    r = _cleanup_harness(
        world, steps + "_drop_own_prestop_state\n", post_merge=post_merge, on_live=on_live
    )
    assert r.returncode == 0, r.stderr
    assert world["state"].exists() is kept


def test_every_exit_trap_after_the_fetching_write_drops_it():
    """Both EXIT traps a plain run can hold between "fetching" and "merging"."""
    traps = re.findall(r"^\s*trap '([^']*)' EXIT$", _text(), re.MULTILINE)
    plain = [t for t in traps if "_live_prestop_exit" not in t and "TEMP_COPY" not in t]
    assert len(plain) == 2, traps
    assert all("_drop_own_prestop_state" in t for t in plain), plain


# ── the rollback ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("checkout_ok,kept", [("true", False), ("false", True)])
def test_a_rollback_keeps_the_state_when_merged_code_stayed(world, checkout_ok, kept):
    world["state"].write_text(json.dumps({"phase": "bootstrap", "rollback_tag": "pre"}))
    script = (
        "set -Eeuo pipefail\n"
        f'STATE_FILE="{world["state"]}"\ncheckout_ok={checkout_ok}\n'
        f'. "{MARKER_LIB}"\n' + _function("_clear_deploy_marker") + _block("rollback-state-clear")
    )
    r = _bash(script, world["home"])
    assert r.returncode == 0, r.stderr
    assert world["state"].exists() is kept
    if kept:
        assert "restart guard" in r.stdout
