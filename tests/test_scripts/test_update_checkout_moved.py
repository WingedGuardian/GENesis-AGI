"""update.sh when ANOTHER process moves the checkout while it runs.

A concurrent session, an editor or a git hook can switch the branch, commit, or
edit tracked files during the minutes an update takes. update.sh must then never
act on that work: it refuses before the rollback tag when the checkout moved
during the start-up backup, refuses before the clears and again before the merge,
pins the deploy target from a ref only this run writes, and its rollback resets
ONLY from the commit this run itself produced. Each block is extracted from the
real script and run against real git repositories.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE = REPO_ROOT / "scripts" / "update.sh"
LIB = REPO_ROOT / "scripts" / "lib" / "deploy_checkout.sh"
MARKER_LIB = REPO_ROOT / "scripts" / "lib" / "deploy_marker.sh"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GENESIS_"))}
    env.update(_GIT_ENV)
    env["HOME"] = str(home)
    return env


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        env=_env(cwd),
    ).stdout.strip()


def _text() -> str:
    return UPDATE.read_text()


def _block(marker: str) -> str:
    match = re.search(
        rf"# BEGIN {re.escape(marker)}[^\n]*\n(.*?)# END {re.escape(marker)}", _text(), re.DOTALL
    )
    assert match, f"missing {marker} block"
    return match.group(1)


def _run(script: str, home: Path) -> subprocess.CompletedProcess:
    libs = f'. "{MARKER_LIB}"\n. "{LIB}"\n'
    return subprocess.run(
        ["bash", "-c", "set -Eeuo pipefail\n" + libs + script],
        capture_output=True,
        text=True,
        timeout=60,
        env=_env(home),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout on main with a rollback tag at its head, and a branch `other`."""
    root = tmp_path / "root"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    (root / "code.py").write_text("x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "c1")
    _git(root, "tag", "pre-update-test")
    _git(root, "branch", "other")
    return root


def _merge_like_commit(root: Path) -> str:
    """What the update's merge leaves: a new commit on main."""
    (root / "code.py").write_text("x = 2\n")
    _git(root, "commit", "-qam", "merged upstream")
    return _git(root, "rev-parse", "HEAD")


# ── the rollback guard ──────────────────────────────────────────────────────


def _rollback_guard(root: Path, own_head: str, merge_attempted: bool = False) -> str:
    """The guard, inside a function as in _do_rollback, then its verdict."""
    return (
        f'GENESIS_ROOT="{root}"\nORIGINAL_BRANCH=main\nROLLBACK_TAG=pre-update-test\n'
        f'UPDATE_OWN_HEAD="{own_head}"\nEPHEMERAL_BACKUP_ROOT=/nonexistent\n'
        f"MERGE_ATTEMPTED={1 if merge_attempted else 0}\n"
        "_ephemeral_backup_before_reset() { echo BACKUP-BEFORE-RESET; }\n"
        "guard() {\n"
        + _block("rollback-code-guard")
        + '    echo "ACTION=$code_action OK=$checkout_ok RESTART=$restart_ok"\n}\nguard\n'
    )


def _verdict(r: subprocess.CompletedProcess) -> tuple[str, str]:
    assert r.returncode == 0, r.stderr
    m = re.search(r"ACTION=(\w+) OK=(\w+)", r.stdout)
    assert m, r.stdout
    return m.group(1), m.group(2)


def _restarts(r: subprocess.CompletedProcess) -> bool:
    """Whether the guard lets services restart from the checkout."""
    m = re.search(r"RESTART=(\w+)", r.stdout)
    assert m, r.stdout
    return m.group(1) == "true"


def test_a_failure_before_the_merge_resets_nothing_and_keeps_new_edits(repo, tmp_path):
    """This run never moved HEAD, so there is nothing to undo — and an edit made
    meanwhile by someone else must survive (a reset would destroy it). That edit
    is code nobody validated, so services are NOT restarted on it either."""
    tag = _git(repo, "rev-parse", "pre-update-test")
    (repo / "code.py").write_text("someone else's edit\n")
    r = _run(_rollback_guard(repo, own_head=tag), tmp_path)
    assert _verdict(r) == ("none", "false")
    assert not _restarts(r)
    assert (repo / "code.py").read_text() == "someone else's edit\n"
    assert "BACKUP-BEFORE-RESET" not in r.stdout
    assert "services are NOT restarted" in r.stdout and "code.py" in r.stdout


def test_a_clean_failure_before_the_merge_restarts_on_the_old_code(repo, tmp_path):
    tag = _git(repo, "rev-parse", "pre-update-test")
    r = _run(_rollback_guard(repo, own_head=tag), tmp_path)
    assert _verdict(r) == ("none", "true")
    assert _restarts(r)


def test_a_failure_after_the_merge_resets_this_runs_merge(repo, tmp_path):
    merged = _merge_like_commit(repo)
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r) == ("reset", "true")
    assert _restarts(r)
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "pre-update-test")
    assert (repo / "code.py").read_text() == "x = 1\n"
    assert "BACKUP-BEFORE-RESET" in r.stdout, "ephemeral edits are saved before the reset"


def test_a_switched_branch_is_left_exactly_as_it_is(repo, tmp_path):
    """Someone switched to another branch and edited there. The old rollback
    checked main out again and reset it, carrying their edit away and destroying
    it; now nothing is touched and the rollback reports itself incomplete."""
    merged = _merge_like_commit(repo)
    _git(repo, "checkout", "-q", "other")
    (repo / "code.py").write_text("work on other\n")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r) == ("moved", "false")
    assert not _restarts(r), "never restart services on someone else's branch"
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "other"
    assert (repo / "code.py").read_text() == "work on other\n"
    assert _git(repo, "rev-parse", "main") == merged, "main was not reset either"
    assert "the checkout moved after this update merged" in r.stdout
    assert "reset $ORIGINAL_BRANCH" not in r.stdout and "reset main" not in r.stdout


def test_a_clean_switch_after_the_merge_is_switched_back_and_rolled_back(repo, tmp_path):
    """A plain branch switch with nothing uncommitted: switch back (non-forced),
    then undo this run's own merge and restart on the old code. The other branch
    keeps its commits."""
    merged = _merge_like_commit(repo)
    _git(repo, "checkout", "-q", "other")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "their work")
    theirs = _git(repo, "rev-parse", "other")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r) == ("reset", "true")
    assert _restarts(r)
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "pre-update-test")
    assert _git(repo, "rev-parse", "other") == theirs, "their branch keeps its commits"
    assert "switched back to main" in r.stdout


def test_a_commit_made_on_top_of_the_merge_is_not_reset_away(repo, tmp_path):
    merged = _merge_like_commit(repo)
    (repo / "code.py").write_text("x = 3\n")
    _git(repo, "commit", "-qam", "someone else's commit")
    theirs = _git(repo, "rev-parse", "HEAD")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r) == ("moved", "false")
    assert not _restarts(r)
    assert _git(repo, "rev-parse", "HEAD") == theirs


def test_a_detached_head_is_switched_back_and_a_missing_tag_is_moved(repo, tmp_path):
    merged = _merge_like_commit(repo)
    # Detached at a commit that is not ours, with main still at our merge: a
    # clean switch, so main is checked out again and our merge rolled back.
    _git(repo, "checkout", "-q", "--detach", "HEAD~1")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r) == ("reset", "true")
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    # No rollback target at all: never reset blind, never restart.
    merged = _merge_like_commit(repo)
    _git(repo, "tag", "-d", "pre-update-test")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r) == ("moved", "false"), "no rollback target: never reset blind"
    assert not _restarts(r)
    assert _git(repo, "rev-parse", "HEAD") == merged


def test_a_commit_before_any_merge_is_left_alone_and_not_restarted_on(repo, tmp_path):
    """This run never merged, so a commit on top is someone else's: left alone,
    never reset away, and services are NOT restarted on that unvalidated code —
    an incomplete rollback, with no advice to reset their work."""
    tag = _git(repo, "rev-parse", "pre-update-test")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "theirs")
    before = _git(repo, "rev-parse", "HEAD")
    r = _run(_rollback_guard(repo, own_head=tag), tmp_path)
    assert _verdict(r) == ("untouched", "false")
    assert not _restarts(r)
    assert _git(repo, "rev-parse", "HEAD") == before
    assert "This run never changed the code" in r.stdout
    assert "reset" not in r.stdout.lower().replace("not restarted", "")


def test_a_switch_before_any_merge_is_switched_back_and_restarted_on_the_old_code(repo, tmp_path):
    """The P1 case: a branch switched before the merge used to restart services on
    the other branch. A clean switch is reversed (non-forced) and services restart
    on the pre-update commit; the other branch is untouched."""
    tag = _git(repo, "rev-parse", "pre-update-test")
    _git(repo, "checkout", "-q", "other")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "feature work")
    theirs = _git(repo, "rev-parse", "other")
    r = _run(_rollback_guard(repo, own_head=tag), tmp_path)
    assert _verdict(r) == ("none", "true")
    assert _restarts(r)
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(repo, "rev-parse", "HEAD") == tag
    assert _git(repo, "rev-parse", "other") == theirs


def test_a_switch_back_never_overwrites_an_ignored_file(repo, tmp_path):
    """The original branch tracks a file the other branch de-tracked and ignores
    (the shape of this repo's de-tracked settings files). A plain checkout would
    replace the local ignored copy without asking (measured, git 2.43); the switch
    back must refuse instead, keep the file, and not restart."""
    tag = _git(repo, "rev-parse", "pre-update-test")
    _git(repo, "checkout", "-q", "other")
    _git(repo, "rm", "-q", "--cached", "code.py")
    (repo / ".gitignore").write_text("code.py\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "de-track code.py")
    (repo / "code.py").write_text("PRECIOUS local copy\n")
    r = _run(_rollback_guard(repo, own_head=tag), tmp_path)
    assert (repo / "code.py").read_text() == "PRECIOUS local copy\n"
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "other"
    assert _verdict(r) == ("untouched", "false")
    assert not _restarts(r)
    assert "Did not switch back to main" in r.stdout


def test_no_switch_back_when_the_original_branch_moved_on(repo, tmp_path):
    """Someone committed on main AND switched away: main is no longer this run's
    state, so switching back would restart on their commit. Left as found."""
    tag = _git(repo, "rev-parse", "pre-update-test")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "theirs on main")
    _git(repo, "checkout", "-q", "other")
    r = _run(_rollback_guard(repo, own_head=tag), tmp_path)
    assert _verdict(r) == ("untouched", "false")
    assert not _restarts(r)
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "other"


def test_a_switch_with_uncommitted_work_is_not_switched_back_or_restarted_on(repo, tmp_path):
    """Their uncommitted edit is in the way: no switch (it would carry or clash
    with their edit), no restart on their branch, rollback incomplete."""
    tag = _git(repo, "rev-parse", "pre-update-test")
    _git(repo, "checkout", "-q", "other")
    (repo / "code.py").write_text("their uncommitted work\n")
    r = _run(_rollback_guard(repo, own_head=tag), tmp_path)
    assert _verdict(r) == ("untouched", "false")
    assert not _restarts(r)
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "other"
    assert (repo / "code.py").read_text() == "their uncommitted work\n"


def test_an_interrupted_merge_with_merge_head_is_aborted(repo, tmp_path):
    """HEAD unchanged but a merge in progress: abort it (what the old reset
    repaired), without resetting anything else."""
    tag = _git(repo, "rev-parse", "pre-update-test")
    _git(repo, "checkout", "-q", "other")
    (repo / "code.py").write_text("x = 'other'\n")
    _git(repo, "commit", "-qam", "other side")
    _git(repo, "checkout", "-q", "main")
    (repo / "code.py").write_text("x = 'main'\n")
    _git(repo, "commit", "-qam", "main side")
    _git(repo, "tag", "-f", "pre-update-test")
    tag = _git(repo, "rev-parse", "pre-update-test")
    subprocess.run(
        ["git", "-C", str(repo), "merge", "-q", "other"], capture_output=True, env=_env(repo)
    )
    assert (repo / ".git" / "MERGE_HEAD").exists(), "fixture: a merge must be in progress"
    r = _run(_rollback_guard(repo, own_head=tag, merge_attempted=True), tmp_path)
    assert _verdict(r) == ("none", "true")
    assert _restarts(r)
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert (repo / "code.py").read_text() == "x = 'main'\n"


def test_an_interrupted_merge_that_left_tracked_changes_is_reported_not_reset(repo, tmp_path):
    tag = _git(repo, "rev-parse", "pre-update-test")
    (repo / "code.py").write_text("half-written\n")
    r = _run(_rollback_guard(repo, own_head=tag, merge_attempted=True), tmp_path)
    assert _verdict(r) == ("none", "false")
    assert not _restarts(r)
    assert "the merge was interrupted" in r.stdout and "code.py" in r.stdout
    assert (repo / "code.py").read_text() == "half-written\n", "never reset: may be someone's edit"
    # Control: with no merge attempted the edit is not called an interrupted
    # merge (it is still not restarted on: see the first test).
    r = _run(_rollback_guard(repo, own_head=tag), tmp_path)
    assert "the merge was interrupted" not in r.stdout


def test_a_moved_checkout_keeps_the_migrated_database():
    """Code left as it is (merged plus their change) must keep the schema that
    matches it; restoring the pre-update DB would put new code on an old schema."""
    text = _text()
    start = text.index("_do_rollback() {")
    body = text[start : text.index("\n_on_err() {", start)]  # the heredoc holds a column-0 }
    migrated = body.index('if [ "${MIGRATIONS_RAN:-0}" = "1" ]; then')
    moved = body.index('if [ "$code_action" = "moved" ]; then', migrated)
    restore = body.index('cp "$DB_FILE.pre-update" "$DB_FILE"', migrated)
    assert migrated < moved < restore
    branch = body[moved : body.index("elif", moved)]
    assert "db_ok=false" in branch and "cp " not in branch


def test_reinstall_and_restart_happen_only_on_a_verified_old_tree():
    """The guard's verdict must actually gate the two steps that would boot the
    checkout: the dependency reinstall and the service restarts."""
    text = _text()
    start = text.index("_do_rollback() {")
    body = text[start : text.index("\n_on_err() {", start)]  # the heredoc holds a column-0 }
    guard_end = body.index("# END rollback-code-guard")
    pip = body.index('"$VENV_DIR/bin/pip" install', guard_end)
    assert 'if [ "$restart_ok" = "true" ] \\\n' in body[guard_end:pip]
    loop = body.index('for svc in "${WERE_RUNNING[@]}"; do', guard_end)
    gate = body.rindex('if [ "$restart_ok" = "true" ]; then', guard_end, loop)
    assert "\n    fi\n" not in body[gate:loop]
    assert body.count('for svc in "${WERE_RUNNING[@]}"; do') == 1


def test_the_closing_banner_does_not_claim_a_rollback_that_did_not_happen():
    text = _text()
    start = text.index("_do_rollback() {")
    body = text[start : text.index("\n_on_err() {", start)]  # the heredoc holds a column-0 }
    banner = body.index('echo "  Rolled back: $OLD_TAG ($OLD_COMMIT) on $ORIGINAL_BRANCH"')
    gate = body.rindex(
        'if [ "$checkout_ok" = "true" ] && [ "$pip_ok" = "true" ] && [ "$db_ok" = "true" ]',
        0,
        banner,
    )
    assert "\n    fi\n" not in body[gate:banner]
    assert "NOT fully rolled back" in body[banner:]


def test_the_rollback_no_longer_checks_a_branch_out():
    """`checkout "$ORIGINAL_BRANCH"` was how a rollback moved someone's checkout
    off their branch; the guard requires being on it instead."""
    text = _text()
    body = text[
        text.index("_do_rollback() {") : text.index("\n}\n", text.index("_do_rollback() {"))
    ]
    assert 'checkout "$ORIGINAL_BRANCH"' not in body
    guard = _block("rollback-code-guard")
    assert guard.count('reset --hard "$ROLLBACK_TAG"') == 1
    assert guard.index("reset)") < guard.index('reset --hard "$ROLLBACK_TAG"') < guard.index("*)")


# ── the checks before the clears and before the merge ───────────────────────


def _unmoved_check(root: Path, own_head: str) -> str:
    return (
        f'GENESIS_ROOT="{root}"\nORIGINAL_BRANCH=main\nUPDATE_OWN_HEAD="{own_head}"\n'
        '_do_rollback() { echo "ROLLBACK: $1"; }\n' + _block("checkout-unmoved") + "echo PASSED\n"
    )


def test_an_unmoved_clean_checkout_passes(repo, tmp_path):
    r = _run(_unmoved_check(repo, _git(repo, "rev-parse", "HEAD")), tmp_path)
    assert r.returncode == 0, r.stderr
    assert "PASSED" in r.stdout and "ROLLBACK" not in r.stdout


@pytest.mark.parametrize("move", ["switch", "commit"])
def test_a_moved_checkout_refuses_before_the_merge(repo, tmp_path, move):
    head = _git(repo, "rev-parse", "HEAD")
    if move == "switch":
        _git(repo, "checkout", "-q", "other")
    else:
        _git(repo, "commit", "-q", "--allow-empty", "-m", "theirs")
    r = _run(_unmoved_check(repo, head), tmp_path)
    assert r.returncode == 1
    assert "ROLLBACK: the checkout moved during the update" in r.stdout
    assert "PASSED" not in r.stdout


def test_a_new_tracked_edit_refuses_before_the_merge(repo, tmp_path):
    (repo / "code.py").write_text("edited during the update\n")
    r = _run(_unmoved_check(repo, _git(repo, "rev-parse", "HEAD")), tmp_path)
    assert r.returncode == 1
    assert "ROLLBACK: tracked files changed during the update" in r.stdout
    assert "code.py" in r.stdout


def test_an_ephemeral_edit_does_not_refuse(repo, tmp_path):
    """The clears below handle the excused paths; only other tracked edits refuse."""
    (repo / "AGENTS.md").write_text("stats\n")
    _git(repo, "add", "AGENTS.md")
    _git(repo, "commit", "-qm", "track AGENTS.md")
    (repo / "AGENTS.md").write_text("regenerated stats\n")
    r = _run(_unmoved_check(repo, _git(repo, "rev-parse", "HEAD")), tmp_path)
    assert r.returncode == 0, r.stdout
    assert "PASSED" in r.stdout


def test_the_check_runs_before_the_clears_and_again_just_before_the_merge():
    text = _text()
    first = text.index("# BEGIN checkout-unmoved")
    clears = text.index("# BEGIN settings-local-premerge")
    late = text.index("# END late-collision-scan")
    merge = text.index('merge --no-overwrite-ignore "$DEPLOY_HEAD" --no-edit')
    calls = [m.start() for m in re.finditer(r"^_checkout_unmoved_or_roll_back$", text, re.M)]
    assert len(calls) == 2, calls
    assert first < calls[0] < clears
    assert late < calls[1] < merge


def _assertion_block() -> str:
    text = _text()
    start = text.index('_merged_head="$(git -C "$GENESIS_ROOT" rev-parse')
    check = text.index('if ! git -C "$GENESIS_ROOT" merge-base --is-ancestor "$DEPLOY_HEAD" HEAD')
    return text[start : text.index("\nfi\n", check) + 4]


def test_only_this_runs_merge_is_adopted_as_its_own_head(tmp_path):
    """A true 3-way merge (parents: validated head, pinned head) is this run's; a
    commit someone made on top of it in the moment since is not, and fails the
    update so the rollback leaves it alone."""
    up = tmp_path / "up"
    _git(tmp_path, "init", "-q", "-b", "main", str(up))
    (up / "f").write_text("1\n")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "c1")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(up), str(clone))
    (up / "f").write_text("2\n")
    _git(up, "commit", "-qam", "upstream")
    (clone / "g").write_text("local\n")
    _git(clone, "add", "g")
    _git(clone, "commit", "-qm", "local")
    validated = _git(clone, "rev-parse", "HEAD")
    _git(clone, "fetch", "-q", "origin")
    pin = _git(clone, "rev-parse", "origin/main")
    _git(clone, "merge", "-q", "--no-edit", pin)
    base = (
        f'GENESIS_ROOT="{clone}"\nUPDATE_REMOTE=origin\nDEPLOY_BRANCH=main\nORIGINAL_BRANCH=main\n'
        f'DEPLOY_HEAD="{pin}"\nVALIDATED_HEAD="{validated}"\nUPDATE_OWN_HEAD="{validated}"\n'
        '_do_rollback() { echo "ROLLBACK: $1"; }\n'
    )
    script = base + _assertion_block() + 'echo "OWN=$UPDATE_OWN_HEAD"\n'
    r = _run(script, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"OWN={_git(clone, 'rev-parse', 'HEAD')}" in r.stdout
    # Someone commits on top of the merge before it is recorded: not adopted.
    _git(clone, "commit", "-q", "--allow-empty", "-m", "theirs")
    r = _run(script, tmp_path)
    assert r.returncode == 1
    assert "ROLLBACK: merge did not bring in" in r.stdout
    assert "OWN=" not in r.stdout


def test_the_merge_result_becomes_the_runs_own_head_before_anything_can_fail():
    text = _text()
    merge = text.index('merge --no-overwrite-ignore "$DEPLOY_HEAD" --no-edit')
    own = text.index('UPDATE_OWN_HEAD="$_merged_head"', merge)
    assertion = text.index('merge-base --is-ancestor "$DEPLOY_HEAD" HEAD', merge)
    assert merge < own < assertion


# ── the start of the run ────────────────────────────────────────────────────


def test_the_branch_is_the_validated_one_not_a_fresh_read():
    text = _text()
    assert 'ORIGINAL_BRANCH="${_branch:-$DEPLOY_BRANCH}"' in text
    assert not re.search(r"^ORIGINAL_BRANCH=\$\(git", text, re.M)
    assert text.index("genesis_deploy_branch_ok") < text.index('ORIGINAL_BRANCH="${_branch')


def test_a_checkout_moved_during_the_startup_backup_refuses_before_the_tag():
    """The backup runs for minutes between validation and the rollback tag; a
    move in that window refuses while nothing has stopped."""
    text = _text()
    backup = text.index("--- Pre-update backup ---")
    check = text.index(
        'if ! genesis_checkout_unmoved "$GENESIS_ROOT" "$VALIDATED_HEAD" "$ORIGINAL_BRANCH"; then'
    )
    tag = text.index('ROLLBACK_TAG="pre-update-')
    stop = text.index("--- Stopping services for update ---")
    assert backup < check < tag < stop
    stanza = text[check : text.index("\nfi\n", check)]
    assert "exit 1" in stanza and "_do_rollback" not in stanza


# ── the pin reads a ref only this run writes ────────────────────────────────


def _fetch_block() -> str:
    text = _text()
    start = text.index('DEPLOY_HEAD=""\n')
    return text[start : text.index("\nfi\n", start) + 4]


@pytest.fixture
def upstream_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    up = tmp_path / "upstream"
    _git(tmp_path, "init", "-q", "-b", "main", str(up))
    (up / "f").write_text("1\n")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "c1")
    _git(up, "branch", "feature")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(up), str(clone))
    (up / "f").write_text("2\n")
    _git(up, "commit", "-qam", "c2 on main")
    return up, clone


def test_another_fetch_moving_the_tracking_ref_cannot_change_the_pin(upstream_and_clone, tmp_path):
    """Right after our fetch, another session moves the shared tracking ref (as
    its own fetch of a stale mirror would). The pin must still be what WE fetched,
    and the private ref must be gone afterwards."""
    up, clone = upstream_and_clone
    stale = _git(up, "rev-parse", "feature")
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    # The fetch runs under `timeout`, which execs a BINARY, so the interference
    # has to come from a git on PATH, not a shell function.
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    (shim_dir / "git").write_text(
        "#!/bin/sh\n"
        f'"{real_git}" "$@"\n'
        "rc=$?\n"
        'if [ "$3" = fetch ]; then\n'
        f'    "{real_git}" -C "{clone}" update-ref refs/remotes/origin/main {stale}\n'
        "fi\n"
        'exit "$rc"\n'
    )
    (shim_dir / "git").chmod(0o755)
    script = (
        f'export PATH="{shim_dir}:$PATH"\n'
        f'GENESIS_ROOT="{clone}"\nUPDATE_REMOTE=origin\nPOST_MERGE=false\n'
        "ROLLBACK_TAG=t\n_clear_deploy_state() { :; }\n"
        "genesis_range_collisions() { return 0; }\n" + _fetch_block() + 'echo "PIN=$DEPLOY_HEAD"\n'
    )
    r = _run(script, tmp_path)
    assert r.returncode == 0, r.stderr
    assert _git(clone, "rev-parse", "refs/remotes/origin/main") == stale, (
        "control: the tracking ref really was moved under the run"
    )
    assert re.search(r"PIN=(\w+)", r.stdout).group(1) == _git(up, "rev-parse", "main")
    assert _git(clone, "for-each-ref", "refs/genesis/") == "", "the private ref is deleted"
