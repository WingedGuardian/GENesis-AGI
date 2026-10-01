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


def _rollback_guard(
    root: Path,
    own_head: str,
    merge_attempted: bool = False,
    backup_root: str = "",
    real_backup: bool = False,
    extra: str = "",
    db_file: str = "",
) -> str:
    """The guard, inside a function as in _do_rollback, then its verdict. The real
    backup helpers are loaded from update.sh (POST_MERGE=true skips the pre-stop
    loop); only the ephemeral pass is stubbed, so the test can see it ran."""
    return (
        f'GENESIS_ROOT="{root}"\nORIGINAL_BRANCH=main\nROLLBACK_TAG=pre-update-test\n'
        f'UPDATE_OWN_HEAD="{own_head}"\nPOST_MERGE=true\n'
        f"MERGE_ATTEMPTED={1 if merge_attempted else 0}\n"
        + _block("ephemeral-prestop-backup")
        + (f'EPHEMERAL_BACKUP_ROOT="{backup_root}"\n' if backup_root else "")
        + 'echo "BACKUP_ROOT=$EPHEMERAL_BACKUP_ROOT"\n'
        + (
            ""
            if real_backup
            else "_ephemeral_backup_before_reset() { echo BACKUP-BEFORE-RESET; }\n"
        )
        + extra
        + "guard() {\n"
        + (f'MIGRATIONS_RAN=1\nDB_SNAPSHOT_TAKEN=1\nDB_FILE="{db_file}"\n' if db_file else "")
        + _block("rollback-code-guard")
        + (
            "    local server_down=true\n"
            + _block("rollback-db-decision")
            + '    echo "DB_OK=$db_ok"\n'
            if db_file
            else ""
        )
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
    assert "rollback-save" not in r.stdout, "a clean tree saves nothing"
    assert _git(repo, "for-each-ref", "refs/genesis/rollback-save/") == ""


def _saved_sha(r: subprocess.CompletedProcess) -> str:
    m = re.search(r"stash apply --index ([0-9a-f]{40})", r.stdout)
    assert m, r.stdout
    return m.group(1)


def _second_file(repo: Path) -> None:
    """Track notes.txt in the pre-update state: a path the merge does NOT change."""
    _retag_with(repo, "notes.txt", "base notes\n")


def test_an_edit_outside_the_range_is_kept_in_place(repo, tmp_path):
    """#2679: another session edits a file this update never touched, and stages a
    new file, after the merge; then a later step fails. The reset (`--keep`) undoes
    the merge and leaves both where they are; the index state is recorded too."""
    _second_file(repo)
    merged = _merge_like_commit(repo)
    (repo / "notes.txt").write_text("their edit after the merge\n")
    (repo / "new_module.py").write_text("brand new\n")
    _git(repo, "add", "new_module.py")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r)[0] == "reset", r.stdout
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "pre-update-test")
    assert (repo / "code.py").read_text() == "x = 1\n", "the merge was undone"
    assert (repo / "notes.txt").read_text() == "their edit after the merge\n"
    assert (repo / "new_module.py").read_text() == "brand new\n"
    sha = _saved_sha(r)
    refs = _git(repo, "for-each-ref", "--format=%(objectname)", "refs/genesis/rollback-save/")
    assert refs == sha, "kept alive by a named ref"
    assert _git(repo, "show", f"{sha}^2:new_module.py") == "brand new", "the staged state"
    # Someone else's edit is code nobody validated: no restart on it (#2623's rule).
    assert not _restarts(r)
    assert "notes.txt" in r.stdout


def test_an_edit_to_a_file_the_update_changed_refuses_the_reset(repo, tmp_path):
    """The reset would have to overwrite the edit: git refuses, moving nothing, and
    the merged code, the edit and the database stay for a person to sort out."""
    merged = _merge_like_commit(repo)
    (repo / "code.py").write_text("their edit after the merge\n")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r) == ("reset", "false")
    assert not _restarts(r)
    assert _git(repo, "rev-parse", "HEAD") == merged
    assert (repo / "code.py").read_text() == "their edit after the merge\n"
    assert "git refused" in r.stdout and "NOT rolled back" in r.stdout


def test_an_edit_made_just_before_the_reset_is_judged_by_git(repo, tmp_path):
    """The snapshot-to-reset race (#2722 round 1): an edit landing AFTER the
    snapshot is still seen, because git checks each path as the reset writes it.
    The edit is injected after the snapshot helper, right before the reset."""
    merged = _merge_like_commit(repo)
    # The last check before the refresh and the reset makes the edit, then passes.
    late = (
        "_rollback_reset_collision_free() {\n"
        f'    printf "late edit\\n" > "{repo}/code.py"\n'
        "    return 0\n}\n"
    )
    r = _run(_rollback_guard(repo, own_head=merged, extra=late), tmp_path)
    assert "Recorded" not in r.stdout, "control: the snapshot ran on a clean tree"
    assert _verdict(r) == ("reset", "false"), r.stdout
    assert (repo / "code.py").read_text() == "late edit\n"
    assert _git(repo, "rev-parse", "HEAD") == merged


def test_an_unwritable_backup_dir_does_not_block_a_clean_rollback(repo, tmp_path):
    """#2722 round 1 (P2): nothing to save, so storage for a snapshot is never
    needed — a bad backup directory must not stop the reset."""
    merged = _merge_like_commit(repo)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the backup directory must go\n")
    r = _run(_rollback_guard(repo, own_head=merged, backup_root=str(blocker / "run")), tmp_path)
    assert _verdict(r) == ("reset", "true"), r.stdout
    assert _restarts(r)


@pytest.mark.parametrize("why", ["unwritable", "intent-to-add"])
def test_a_failed_snapshot_does_not_block_the_reset(repo, tmp_path, why):
    """The snapshot is a record, not the protection: when git cannot take it, or
    there is nowhere to write its record, the reset still runs and still keeps
    the edit in place."""
    _second_file(repo)
    merged = _merge_like_commit(repo)
    backup_root = ""
    if why == "unwritable":
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x\n")
        backup_root = str(blocker / "run")
        (repo / "notes.txt").write_text("their edit\n")
        kept, content = repo / "notes.txt", "their edit\n"
    else:
        (repo / "ita.py").write_text("planned\n")
        _git(repo, "add", "-N", "ita.py")
        kept, content = repo / "ita.py", "planned\n"
    r = _run(_rollback_guard(repo, own_head=merged, backup_root=backup_root), tmp_path)
    assert _verdict(r)[0] == "reset", r.stdout
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "pre-update-test")
    assert kept.read_text() == content
    if why == "intent-to-add":
        assert "NOTE: git could not snapshot" in r.stdout
    else:
        # The snapshot itself needs no directory: only its text record is lost.
        assert "Recorded" in r.stdout
        assert not (tmp_path / "not-a-dir" / "run").exists()


def _flag_and_change(repo: Path, path: str, flag: str, change: str) -> None:
    _git(repo, "update-index", f"--{flag}", path)
    p = repo / path
    if change == "content":
        p.write_text("hidden local edit\n")
    elif change == "chmod":
        p.chmod(0o755)
    else:  # a symlink whose target text equals the committed bytes
        text = p.read_text()
        p.unlink()
        p.symlink_to(text)


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
@pytest.mark.parametrize("change", ["content", "chmod", "symlink"])
def test_a_hidden_change_to_a_file_the_update_changed_refuses(repo, tmp_path, flag, change):
    """#2722 round 1: a mode or type change behind assume-unchanged hashes like
    the committed blob, and skip-worktree hides an edit from diff and stash. Under
    --keep git itself refuses over every one of them (measured)."""
    merged = _merge_like_commit(repo)
    _flag_and_change(repo, "code.py", flag, change)
    before = os.lstat(repo / "code.py")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r) == ("reset", "false"), r.stdout
    assert _git(repo, "rev-parse", "HEAD") == merged
    after = os.lstat(repo / "code.py")
    assert (after.st_mode, after.st_ino) == (before.st_mode, before.st_ino)


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
@pytest.mark.parametrize("change", ["content", "chmod", "symlink"])
def test_a_hidden_change_outside_the_range_survives_the_reset(repo, tmp_path, flag, change):
    _second_file(repo)
    merged = _merge_like_commit(repo)
    _flag_and_change(repo, "notes.txt", flag, change)
    before = os.lstat(repo / "notes.txt")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r)[0] == "reset", r.stdout
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "pre-update-test")
    after = os.lstat(repo / "notes.txt")
    assert (after.st_mode, after.st_ino) == (before.st_mode, before.st_ino)


def test_a_file_rewritten_with_identical_bytes_does_not_refuse(repo, tmp_path):
    """--keep does not refresh the index: without the refresh first, a file an
    indexer rewrote byte-for-byte reads as modified and the rollback refuses. The
    rewrite lands after the last check (earlier `git diff` calls refresh the index
    as a side effect, which would hide the gap)."""
    merged = _merge_like_commit(repo)
    p = repo / "code.py"
    late = (
        "_rollback_reset_collision_free() {\n"
        f'    cp -p "{p}" "{p}.tmp" && mv "{p}.tmp" "{p}"\n'
        f'    touch -d "+30 seconds" "{p}"\n'
        "    return 0\n}\n"
    )
    r = _run(_rollback_guard(repo, own_head=merged, extra=late), tmp_path)
    assert _verdict(r) == ("reset", "true"), r.stdout
    assert p.read_text() == "x = 1\n"


def test_an_untracked_file_where_the_reset_writes_refuses_the_reset(repo, tmp_path):
    """The merge removed a file the rollback tag tracks, and someone put an
    untracked file there: the reset would overwrite it with the tag's version."""
    _git(repo, "rm", "-q", "code.py")
    _git(repo, "commit", "-qm", "merged upstream: code.py removed")
    merged = _git(repo, "rev-parse", "HEAD")
    (repo / "code.py").write_text("someone's new untracked file\n")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r) == ("reset", "false")
    assert not _restarts(r)
    assert (repo / "code.py").read_text() == "someone's new untracked file\n"
    assert _git(repo, "rev-parse", "HEAD") == merged


def test_an_ignored_file_where_the_reset_writes_refuses_the_reset(repo, tmp_path):
    """--keep overwrites an IGNORED file at a path it writes (measured): the
    collision scan refuses first."""
    _git(repo, "rm", "-q", "code.py")
    (repo / ".gitignore").write_text("code.py\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "merged upstream: code.py removed and ignored")
    merged = _git(repo, "rev-parse", "HEAD")
    (repo / "code.py").write_text("local ignored settings\n")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r) == ("reset", "false")
    assert (repo / "code.py").read_text() == "local ignored settings\n"
    assert "untracked or ignored file where the reset writes" in r.stdout
    assert _git(repo, "rev-parse", "HEAD") == merged


def _retag_with(repo: Path, name: str, content: str) -> None:
    """Add a tracked file to the pre-update state (moving the rollback tag)."""
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", f"track {name}")
    _git(repo, "tag", "-f", "pre-update-test")


@pytest.mark.parametrize("ignored", [False, True])
@pytest.mark.parametrize("in_range", [False, True])
def test_a_file_replaced_by_a_directory_keeps_the_directory(repo, tmp_path, ignored, in_range):
    """Someone replaced a tracked file with a directory of their own untracked (or
    ignored) files. A hard reset deleted them; --keep keeps them, refusing when
    the update changed that file and leaving them alone when it did not."""
    _retag_with(repo, "keep.txt", "tracked\n")
    if in_range:
        (repo / "keep.txt").write_text("changed by the update\n")
        _git(repo, "commit", "-qam", "update changes keep.txt")
    merged = _merge_like_commit(repo)
    if ignored:
        (repo / ".git" / "info" / "exclude").write_text("keep.txt/notes\n")
    (repo / "keep.txt").unlink()
    (repo / "keep.txt").mkdir()
    (repo / "keep.txt" / "notes").write_text("PRECIOUS\n")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert (repo / "keep.txt" / "notes").read_text() == "PRECIOUS\n", r.stdout
    if in_range:
        assert _verdict(r) == ("reset", "false"), r.stdout
        assert _git(repo, "rev-parse", "HEAD") == merged
    else:
        assert _verdict(r)[0] == "reset", r.stdout
        assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "pre-update-test")


def test_an_unchanged_assume_unchanged_symlink_does_not_refuse(repo, tmp_path):
    """A flagged but unchanged symlink must not block every rollback."""
    (repo / "link").symlink_to("code.py")
    _git(repo, "add", "link")
    _git(repo, "commit", "-qm", "track link")
    _git(repo, "tag", "-f", "pre-update-test")
    merged = _merge_like_commit(repo)
    _git(repo, "update-index", "--assume-unchanged", "link")
    r = _run(_rollback_guard(repo, own_head=merged), tmp_path)
    assert _verdict(r) == ("reset", "true"), r.stdout


@pytest.mark.parametrize("backed_up", [True, False])
def test_an_ephemeral_edit_the_update_changed_is_cleared_only_with_a_backup(
    repo, tmp_path, backed_up
):
    """An indexer rewrites AGENTS.md; if the update changed it, --keep would refuse
    every rollback over it. With a current backup the edit is cleared first (as
    before the merge); without one it is kept and the reset refuses."""
    _retag_with(repo, "AGENTS.md", "index v1\n")
    (repo / "AGENTS.md").write_text("index v2\n")
    _git(repo, "commit", "-qam", "update regenerates AGENTS.md")
    merged = _merge_like_commit(repo)
    (repo / "AGENTS.md").write_text("indexer rewrote it\n")
    backup_root = tmp_path / "backups"
    if not backed_up:
        backup_root = tmp_path / "not-a-dir"
        backup_root.write_text("x\n")
        backup_root = backup_root / "run"
    r = _run(
        _rollback_guard(repo, own_head=merged, backup_root=str(backup_root), real_backup=True),
        tmp_path,
    )
    if backed_up:
        assert _verdict(r) == ("reset", "true"), r.stdout
        saved = backup_root / "rollback" / "AGENTS.md" / "current"
        assert saved.read_text() == "indexer rewrote it\n"
        assert (repo / "AGENTS.md").read_text() == "index v1\n"
    else:
        assert _verdict(r) == ("reset", "false"), r.stdout
        assert (repo / "AGENTS.md").read_text() == "indexer rewrote it\n"
        assert _git(repo, "rev-parse", "HEAD") == merged


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "genesis.db"
    db.write_text("migrated\n")
    (tmp_path / "genesis.db.pre-update").write_text("pre-update\n")
    return db


@pytest.mark.parametrize(
    "case, restored",
    [("clean", True), ("edit-outside", True), ("edit-inside", False)],
)
def test_the_database_follows_the_code_left_on_disk(repo, tmp_path, case, restored):
    """Migrations ran. When the reset undid the merge, the pre-update database comes
    back with the old code (foreign edits outside the update's files do not change
    which code that is); when git refused, the merged code stays and so does the
    migrated database. Run, not read: the guard and the DB decision together."""
    _second_file(repo)
    merged = _merge_like_commit(repo)
    if case == "edit-outside":
        (repo / "notes.txt").write_text("their edit\n")
    elif case == "edit-inside":
        (repo / "code.py").write_text("their edit\n")
    db = _db(tmp_path)
    r = _run(_rollback_guard(repo, own_head=merged, db_file=str(db)), tmp_path)
    assert r.returncode == 0, r.stderr
    assert db.read_text() == ("pre-update\n" if restored else "migrated\n"), r.stdout
    assert ("DB_OK=true" in r.stdout) is restored, r.stdout
    if not restored:
        assert "migrated database is kept" in r.stdout


def test_a_kept_merge_keeps_the_migrated_database():
    """Both failure arms of the reset mark the code as kept (the run-based test
    above exercises the refusal; this pins the collision arm's flag too)."""
    guard = _block("rollback-code-guard")
    assert guard.count("code_kept=true") == 2


def test_old_rollback_save_refs_are_pruned(tmp_path):
    hygiene = REPO_ROOT / "scripts" / "disk_hygiene.sh"
    repo = tmp_path / "r"
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "commit", "-q", "--allow-empty", "-m", "c1")
    head = _git(repo, "rev-parse", "HEAD")
    for name in ("20200101T000000Z-1", "29991231T000000Z-2", "not-a-timestamp"):
        _git(repo, "update-ref", f"refs/genesis/rollback-save/{name}", head)
    r = subprocess.run(
        ["bash", "-c", f"source '{hygiene}'\nprune_rollback_save_refs '{repo}' 45"],
        capture_output=True,
        text=True,
        env=_env(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    left = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/genesis/rollback-save/")
    assert "20200101T000000Z-1" not in left, "older than 45 days: pruned"
    assert "29991231T000000Z-2" in left, "recent: kept"
    assert "not-a-timestamp" in left, "unparseable: left alone"


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
    moved = body.index(
        'if [ "$code_action" = "moved" ] || [ "$code_kept" = "true" ]; then', migrated
    )
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
    assert guard.count('reset -q --keep "$ROLLBACK_TAG"') == 1
    assert (
        guard.index("reset)") < guard.index('reset -q --keep "$ROLLBACK_TAG"') < guard.index("*)")
    )


def test_an_ephemeral_edit_the_update_did_not_change_is_left_in_place(repo, tmp_path):
    """--keep never touches a path outside the range, so a backed-up AGENTS.md edit
    there is not cleared: the clear is only for what would make the reset refuse."""
    _retag_with(repo, "AGENTS.md", "index v1\n")
    merged = _merge_like_commit(repo)
    (repo / "AGENTS.md").write_text("indexer rewrote it\n")
    r = _run(
        _rollback_guard(
            repo, own_head=merged, backup_root=str(tmp_path / "backups"), real_backup=True
        ),
        tmp_path,
    )
    assert _verdict(r) == ("reset", "true"), r.stdout
    assert (tmp_path / "backups" / "rollback" / "AGENTS.md" / "current").exists()
    assert (repo / "AGENTS.md").read_text() == "indexer rewrote it\n"


def test_the_rollback_resets_with_keep_after_its_checks():
    """Order inside the reset arm: record, clear, scan, refresh, then `--keep`;
    never `--hard`."""
    guard = _block("rollback-code-guard")
    code = "\n".join(ln for ln in guard.splitlines() if not ln.lstrip().startswith("#"))
    assert "reset --hard" not in code
    order = [
        '_save_tracked_changes_before_reset "$EPHEMERAL_BACKUP_ROOT"',
        '_ephemeral_clear_before_reset "$EPHEMERAL_BACKUP_ROOT"',
        "_rollback_reset_collision_free;",
        "update-index -q --refresh",
        'reset -q --keep "$ROLLBACK_TAG"',
    ]
    idx = [code.index(s) for s in order]
    assert idx == sorted(idx), idx


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
