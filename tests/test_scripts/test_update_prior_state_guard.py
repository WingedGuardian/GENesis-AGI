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
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts.test_update_state_guard import diverged_live, live_row, post_merge_advice

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
    assert guard < text.index("# BEGIN live-engine")


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
        ('_write_state "fetching"\n', False, True, False),
        ("", False, False, True),
    ],
    ids=["own-fetching", "own-merging", "post-merge", "on-live", "never-written"],
)
def test_the_exit_trap_drops_only_its_own_prestop_state(world, steps, post_merge, on_live, kept):
    """SF3: the exits after the "fetching" write (a failed DB snapshot, a server
    that will not stop) left it behind, and the poll no longer deletes it. On
    `live` too (#3370): it writes the same pre-stop row as main."""
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
    plain = [t for t in traps if "TEMP_COPY" not in t]
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


def test_td20_a_suspect_live_tree_the_guard_cannot_judge_names_the_repair(world, tmp_path):
    """T-D20: the live rollback marked the tree suspect and the guard could not
    run (since #3532 every verdict it CAN give prints its own remedy, a live
    half-written one naming the repair, so this line is only for a guard that
    said nothing): the kept-state line names the repair (it says when there is
    nothing to repair), and keeps the by-hand steps."""
    world["state"].write_text(json.dumps({"phase": "bootstrap", "rollback_tag": "pre"}))
    no_guard = tmp_path / "no-guard-here"  # python3 exits 2: the guard file is missing
    script = (
        "set -Eeuo pipefail\n"
        f'STATE_FILE="{world["state"]}"\ncheckout_ok=false\n_LIVE_TREE_SUSPECT=1\n'
        f'SCRIPT_DIR="{no_guard}"\nGENESIS_ROOT="{world["root"]}"\nROLLBACK_TAG=pre\n'
        f'. "{MARKER_LIB}"\n' + _function("_clear_deploy_marker") + _block("rollback-state-clear")
    )
    r = _bash(script, world["home"])
    assert r.returncode == 0, r.stderr
    assert "scripts/deploy_candidates repair-move puts it back to pre" in r.stdout, r.stdout
    assert "by hand, put back the paths" in r.stdout and world["state"].exists()
    assert not post_merge_advice(r.stdout), r.stdout


# ── --post-merge over a half-written checkout (#3492) ───────────────────────────


def test_t4s_post_merge_refuses_a_half_written_tree_on_main(world):
    """T4s: a merge killed after writing only a file it adds (HEAD at the tag,
    the file untracked): --post-merge would bootstrap and restart on part of two
    trees, so it refuses (exit 5) with the guard's message, and keeps the state."""
    root = world["root"]
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-qm", "incoming")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "-q", "--hard", "pre")
    (root / "b.txt").write_text("b\n")
    world["state"].write_text(
        json.dumps({"phase": "merging", "rollback_tag": "pre", "deploy_head": incoming})
    )
    before = world["state"].read_bytes()
    r = _guard_run(world, post_merge=True)
    assert r.returncode == 5 and "PASSED_GUARD" not in r.stdout, r.stdout + r.stderr
    assert "b.txt" in r.stdout and not post_merge_advice(r.stdout), r.stdout
    assert world["state"].read_bytes() == before


def _live_world(tmp_path: Path) -> tuple[dict, dict]:
    d = diverged_live(tmp_path / "repo")
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    w = {"root": d["root"], "home": home, "state": home / ".genesis" / "update_state.json"}
    return d, w


def test_t4s_post_merge_refuses_a_half_written_tree_on_live(tmp_path):
    """T4s on `live`: the switch was killed after writing F, which only the
    two-dot range names."""
    d, w = _live_world(tmp_path)
    (d["root"] / "F").write_text("f\n")
    w["state"].write_text(json.dumps(live_row(d)))
    r = _guard_run(w, post_merge=True)
    assert r.returncode == 5 and "PASSED_GUARD" not in r.stdout, r.stdout + r.stderr
    assert "  ?? F" in r.stdout and not post_merge_advice(r.stdout), r.stdout


def test_post_merge_still_finishes_a_live_move_that_completed(tmp_path):
    """Control: the move finished (HEAD on `live` at its target, nothing else
    written). That guards, and --post-merge is what finishes it."""
    d, w = _live_world(tmp_path)
    _git(d["root"], "checkout", "-q", "-B", "live", d["new"])
    w["state"].write_text(json.dumps(live_row(d, "done")))
    r = _guard_run(w, post_merge=True)
    assert r.returncode == 0 and "PASSED_GUARD" in r.stdout, r.stdout + r.stderr


def test_sf4_post_merge_refuses_a_rollback_killed_after_adoption(tmp_path):
    """SF4: after update.sh adopted the rebuilt tip (record gone, own_head the
    tip), its rollback was killed partway back to the tag. --post-merge would
    install over that tree, so it refuses."""
    d, w = _live_world(tmp_path)
    _git(d["root"], "checkout", "-q", "-B", "live", d["new"])
    (d["root"] / "O").write_text("o\n")
    row = live_row(d)
    row.pop("live_move")
    row["own_head"] = d["new"]
    w["state"].write_text(json.dumps(row))
    r = _guard_run(w, post_merge=True)
    assert r.returncode == 5 and "PASSED_GUARD" not in r.stdout, r.stdout + r.stderr
    assert "  ?? O" in r.stdout, r.stdout


def _stub_guard(tmp_path: Path, rc: int) -> Path:
    scripts = tmp_path / "stub-scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "lib" / "update_state_guard.py").write_text(
        f"import sys\nprint('STUB GUARD SAID THIS')\nsys.exit({rc})\n"
    )
    return scripts


@pytest.mark.parametrize("rc", [1, 2, 3, 4, 6, 10, 126, 127])
def test_post_merge_refuses_when_the_guard_could_not_run(world, tmp_path, rc):
    """NOTE 5 and #3532: 1, 2, 126 and 127 mean the guard crashed or could not
    run, 4 a half-written tree, 6 a state finishing forward would get wrong, 10
    nothing to finish. --post-merge refuses each, with what the guard said (10:
    that there is nothing to finish)."""
    world["state"].write_text(json.dumps({"phase": "bootstrap", "rollback_tag": "pre"}))
    r = _guard_run(world, post_merge=True, scripts=_stub_guard(tmp_path, rc))
    assert r.returncode == 5 and "PASSED_GUARD" not in r.stdout, r.stdout + r.stderr
    said = {10: "nothing to finish", 3: "resolve the conflicts first"}.get(
        rc, "STUB GUARD SAID THIS"
    )
    assert said in r.stdout, r.stdout


@pytest.mark.parametrize("rc", [0])
def test_post_merge_finishes_what_the_guard_judged(world, tmp_path, rc):
    """Control: merged code awaiting bootstrap (0, a resolved conflict
    included) is the one thing --post-merge exists to finish (#3532)."""
    world["state"].write_text(json.dumps({"phase": "bootstrap", "rollback_tag": "pre"}))
    r = _guard_run(world, post_merge=True, scripts=_stub_guard(tmp_path, rc))
    assert r.returncode == 0 and "PASSED_GUARD" in r.stdout, r.stdout + r.stderr


def test_n1_post_merge_refuses_a_guard_whose_check_failed(world, tmp_path):
    """N1: the real guard, with git unrunnable inside its check (python3 and
    bash are reachable, git is not): it could not judge the tree, so it exits
    1, and --post-merge refuses instead of finishing a state it never judged."""
    # "merging" at the tag: judging it needs git (a "bootstrap" row guards on
    # its phase alone and would never reach the failing call).
    world["state"].write_text(
        json.dumps({"phase": "merging", "rollback_tag": "pre", "deploy_head": world["head"]})
    )
    bin_ = tmp_path / "no-git-bin"
    bin_.mkdir()
    for tool in ("python3", "bash"):
        (bin_ / tool).symlink_to(shutil.which(tool))
    script = (
        "set -Eeuo pipefail\n"
        f'GENESIS_ROOT="{world["root"]}"\nSCRIPT_DIR="{REPO / "scripts"}"\n'
        f'STATE_FILE="{world["state"]}"\nPOST_MERGE=true\n'
        + _block("prior-state-guard")
        + "echo PASSED_GUARD\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GENESIS_"))}
    env.update(HOME=str(world["home"]), PATH=str(bin_))
    r = subprocess.run([str(bin_ / "bash"), "-c", script], capture_output=True, text=True, env=env)
    assert r.returncode == 5 and "PASSED_GUARD" not in r.stdout, r.stdout + r.stderr
    assert "could not be checked" in r.stdout, r.stdout


def _main_merge_killed(world: dict, edited: str) -> None:
    """A main merge row at the tag whose merge (pre...incoming) writes b.txt,
    with a tracked edit to ``edited``."""
    root = world["root"]
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "a")
    _git(root, "tag", "-f", "pre")
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-qm", "incoming")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "-q", "--hard", "pre")
    if edited == "b.txt":
        (root / "b.txt").write_text("half\n")
    else:
        (root / edited).write_text("a session's edit\n")
    world["state"].write_text(
        json.dumps({"phase": "merging", "rollback_tag": "pre", "deploy_head": incoming})
    )


def test_main_post_merge_is_refused_over_a_foreign_edit_with_nothing_merged(world):
    """A tracked edit the merge never writes, HEAD at the tag: the update merged
    nothing, so --post-merge has nothing to finish (#3532 round 2); the remedy
    is to save the edit, Dismiss, and run the update again."""
    _main_merge_killed(world, "a.txt")
    r = _guard_run(world, post_merge=True)
    assert r.returncode == 5 and "PASSED_GUARD" not in r.stdout, r.stdout + r.stderr
    assert "never changed the checkout" in r.stdout and "half-written" not in r.stdout


def test_main_post_merge_refuses_residue_on_a_path_the_merge_writes(world):
    _main_merge_killed(world, "b.txt")
    r = _guard_run(world, post_merge=True)
    assert r.returncode == 5 and "PASSED_GUARD" not in r.stdout, r.stdout + r.stderr
    assert "b.txt" in r.stdout, r.stdout


# ── #3532 round 2: --post-merge finishes ACTIVATE (0) and a conflict (3) only ──


def test_p1_post_merge_refuses_a_main_rollback_killed_partway(world):
    root = world["root"]
    (root / "a.txt").write_text("merged\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "the update's merge")
    own = _git(root, "rev-parse", "HEAD")
    _git(root, "rm", "-q", "a.txt")
    world["state"].write_text(
        json.dumps({"phase": "merging", "rollback_tag": "pre", "own_head": own, "deploy_head": own})
    )
    r = _guard_run(world, post_merge=True)
    assert r.returncode == 5 and "PASSED_GUARD" not in r.stdout, r.stdout + r.stderr


def test_p2_post_merge_refuses_a_live_row_that_never_moved(tmp_path):
    d, w = _live_world(tmp_path)
    (d["root"] / "A").write_text("a session's edit\n")
    w["state"].write_text(json.dumps(live_row(d)))
    r = _guard_run(w, post_merge=True)
    assert r.returncode == 5 and "PASSED_GUARD" not in r.stdout, r.stdout + r.stderr
    assert "never changed the checkout" in r.stdout, r.stdout


def test_post_merge_still_finishes_a_clean_merge(world):
    root = world["root"]
    (root / "a.txt").write_text("merged\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "the update's merge")
    own = _git(root, "rev-parse", "HEAD")
    world["state"].write_text(
        json.dumps({"phase": "merging", "rollback_tag": "pre", "own_head": own, "deploy_head": own})
    )
    r = _guard_run(world, post_merge=True)
    assert r.returncode == 0 and "PASSED_GUARD" in r.stdout, r.stdout + r.stderr


def test_post_merge_refuses_a_conflict_not_yet_resolved(world):
    """Audit 4: a pending conflict (exit 3) means nothing is merged yet; a
    resolved one reads 0. Finishing 3 would bootstrap the old code, record a
    success from the old version to itself and delete the conflict record."""
    world["state"].write_text(
        json.dumps({"phase": "merging", "rollback_tag": "pre", "deploy_head": world["head"]})
    )
    (world["home"] / ".genesis" / "update_conflicts.json").write_text(
        json.dumps({"deploy_head": world["head"]})
    )
    r = _guard_run(world, post_merge=True)
    assert r.returncode == 5 and "PASSED_GUARD" not in r.stdout, r.stdout + r.stderr
    assert "resolve the conflicts first" in r.stdout, r.stdout


def test_n7_a_kept_state_that_guards_nothing_says_so(world):
    """N7: a kept state the guard calls safe (exit 10) is not "a check that
    could not run"."""
    world["state"].write_text(json.dumps({"phase": "done"}))
    script = (
        "set -Eeuo pipefail\n"
        f'STATE_FILE="{world["state"]}"\ncheckout_ok=false\n'
        f'SCRIPT_DIR="{REPO / "scripts"}"\nGENESIS_ROOT="{world["root"]}"\n'
        f'. "{MARKER_LIB}"\n' + _function("_clear_deploy_marker") + _block("rollback-state-clear")
    )
    r = _bash(script, world["home"])
    assert r.returncode == 0, r.stderr
    assert "could not run" not in r.stdout and "guards nothing" in r.stdout, r.stdout
