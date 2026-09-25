from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE = REPO_ROOT / "scripts" / "update.sh"

_INHERITED_PREFIXES = ("GENESIS_", "GIT_")


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_INHERITED_PREFIXES)
    }
    env.update(overrides)
    return env


def _block(text: str, marker: str) -> str:
    match = re.search(
        rf"# BEGIN {re.escape(marker)}.*?\n(.*?)# END {re.escape(marker)}",
        text,
        re.DOTALL,
    )
    assert match, f"missing {marker} block"
    return match.group(1)


def _function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    next_function = re.search(
        r"\n[A-Za-z_][A-Za-z0-9_]*\(\) \{", text[start + 1 :]
    )
    assert next_function, f"missing function boundary after {name}"
    return text[start : start + 1 + next_function.start()]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    ).stdout.strip()


def _merged_repo(tmp_path: Path) -> tuple[Path, str, str]:
    remote = tmp_path / "remote"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "main")
    _git(repo, "checkout", "-b", "incoming")
    (repo / "file.txt").write_text("incoming\n")
    _git(repo, "commit", "-am", "incoming")
    incoming = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "incoming:main")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "incoming", "-m", "merge incoming")
    return repo, base, incoming


def _unrelated_merge_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo, base, incoming = _merged_repo(tmp_path)
    _git(repo, "reset", "--hard", "HEAD^1")
    _git(repo, "checkout", "-b", "feature", base)
    (repo / "feature.txt").write_text("feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    feature = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    return repo, base, feature, incoming


def _json_reader_stub() -> str:
    return """_read_json_field() {
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception:
    pass
PY
}
"""


def _run_post_merge_block(
    repo: Path,
    home: Path,
    *,
    state: dict[str, object] | None,
    conflict: dict[str, object] | None,
) -> subprocess.CompletedProcess[str]:
    state_file = home / ".genesis" / "update_state.json"
    conflict_file = home / ".genesis" / "update_conflicts.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if state is not None:
        state_file.write_text(json.dumps(state))
    if conflict is not None:
        conflict_file.write_text(json.dumps(conflict))
    script = (
        "set -euo pipefail\n"
        f'GENESIS_ROOT="{repo}"\n'
        f'STATE_FILE="{state_file}"\n'
        f'CONFLICT_FILE="{conflict_file}"\n'
        'POST_MERGE=true\nDEPLOY_BRANCH=main\nUPDATE_REMOTE=origin\n'
        'OLD_TAG=old\nOLD_COMMIT=old\n'
        + _json_reader_stub()
        + _block(UPDATE.read_text(), "post-merge-target-recovery")
        + 'printf \'%s\\n%s\\n\' "$DEPLOY_HEAD" "$ROLLBACK_TAG"\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_clean_env(HOME=str(home)),
    )


def test_write_state_json_encodes_deploy_fields_without_venv(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    script = (
        "set -euo pipefail\n"
        f'HOME="{tmp_path / "home"}"\n'
        f'VENV_DIR="{tmp_path / "missing-venv"}"\n'
        f'STATE_FILE="{state}"\n'
        'ROLLBACK_TAG=\'pre-update-"x\'\n'
        'OLD_TAG=\'v"old\'\n'
        'OLD_COMMIT=\'abc"def\'\n'
        'DEPLOY_BRANCH=\'release/"x\'\n'
        'DEPLOY_HEAD=\'feed"beef\'\n'
        'STARTED_AT=\'start"ed\'\n'
        'WERE_RUNNING=("svc-a" \'svc "b"\')\n'
        + _function(UPDATE.read_text(), "_read_json_field")
        + "\n"
        + _function(UPDATE.read_text(), "_write_state")
        + '_write_state "fetching"\n'
        '_read_json_field "$STATE_FILE" deploy_branch\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(state.read_text())
    assert data["phase"] == "fetching"
    assert data["rollback_tag"] == 'pre-update-"x'
    assert data["old_tag"] == 'v"old'
    assert data["old_commit"] == 'abc"def'
    assert data["deploy_branch"] == 'release/"x'
    assert data["deploy_head"] == 'feed"beef'
    assert data["started_at"] == 'start"ed'
    assert data["services_stopped"] == ["svc-a", 'svc "b"']


def test_update_resolves_branch_from_the_remote_it_fetches() -> None:
    text = UPDATE.read_text()
    remote = text.index('UPDATE_REMOTE="$(_detect_update_remote)"')
    resolve = text.index('genesis_resolve_deploy_branch "$GENESIS_ROOT" "$UPDATE_REMOTE"')
    assert remote < resolve
    assert 'fetch \\\n        "$UPDATE_REMOTE" "+refs/heads/$DEPLOY_BRANCH:$DEPLOY_FETCH_REF" \\\n        "+refs/heads/$DEPLOY_BRANCH:$DEPLOY_TRACKING_REF"' in text
    assert 'public_repo="${GENESIS_GITHUB_PUBLIC_REPO:-$(genesis_local_github_value public_repo || true)}"' in text


def test_detect_update_remote_matches_the_repo_basename(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "remote", "add", "origin", "https://example.test/GENesis-AGI-backup.git")
    _git(repo, "remote", "add", "upstream", "https://example.test/GENesis-AGI.git")
    text = UPDATE.read_text()
    function = text[
        text.index("_detect_update_remote() {") : text.index('UPDATE_REMOTE="$(_detect_update_remote)"')
    ]
    env = _clean_env(
        HOME=str(tmp_path / "home"),
        GENESIS_GITHUB_PUBLIC_REPO="GENesis-AGI",
    )
    result = subprocess.run(
        ["bash", "-c", f'GENESIS_ROOT="{repo}"\n{function}\n_detect_update_remote'],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "upstream"


def test_private_fetch_ref_and_tracking_ref_survive_narrow_refspec(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "clone", str(remote), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "file.txt").write_text("base\n")
    _git(work, "add", "file.txt")
    _git(work, "commit", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")

    _git(work, "remote", "set-branches", "origin", "other")
    _git(work, "update-ref", "-d", "refs/remotes/origin/main")
    (work / "file.txt").write_text("incoming\n")
    _git(work, "commit", "-am", "incoming")
    incoming = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")

    _git(
        work,
        "fetch",
        "origin",
        "+refs/heads/main:refs/genesis-update-head",
        "+refs/heads/main:refs/remotes/origin/main",
    )

    assert _git(work, "rev-parse", "refs/genesis-update-head") == incoming
    assert _git(work, "rev-parse", "refs/remotes/origin/main") == incoming
    _git(work, "push", "--force", "origin", f"{base}:main")
    _git(
        work,
        "fetch",
        "origin",
        "+refs/heads/main:refs/genesis-update-head",
        "+refs/heads/main:refs/remotes/origin/main",
    )
    assert _git(work, "rev-parse", "refs/genesis-update-head") == base
    assert _git(work, "rev-parse", "refs/remotes/origin/main") == base


def test_update_merges_and_verifies_fetched_remote_head() -> None:
    text = UPDATE.read_text()
    assert 'DEPLOY_FETCH_REF="refs/genesis-update-head"' in text
    assert 'DEPLOY_HEAD=$(git -C "$GENESIS_ROOT" rev-parse "$DEPLOY_FETCH_REF")' in text
    assert 'merge "$DEPLOY_FETCH_REF" --no-edit' in text
    verify = text.index('merge-base --is-ancestor "$DEPLOY_FETCH_REF" HEAD')
    restart = text.index("--- Restarting services ---")
    success = text.index('_record_update_history "success"', verify)
    assert verify < restart
    assert verify < success


def test_record_update_history_uses_bootstrap_safe_python(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "src").symlink_to(REPO_ROOT / "src", target_is_directory=True)
    db = repo / "data" / "genesis.db"
    subprocess.run(
        [
            "python3",
            "-c",
            "import sqlite3,sys; con=sqlite3.connect(sys.argv[1]); "
            "con.execute('CREATE TABLE update_history (id TEXT, old_tag TEXT, new_tag TEXT, "
            "old_commit TEXT, new_commit TEXT, status TEXT, rollback_tag TEXT, "
            "failure_reason TEXT, degraded_subsystems TEXT, started_at TEXT, completed_at TEXT)'); "
            "con.commit()",
            str(db),
        ],
        check=True,
    )
    script = (
        "set -euo pipefail\n"
        f'GENESIS_ROOT="{repo}"\nVENV_DIR="{tmp_path / "missing-venv"}"\n'
        'OLD_TAG=old\nNEW_TAG=new\nOLD_COMMIT=oldsha\nNEW_COMMIT=newsha\n'
        'ROLLBACK_TAG=tag\nSTARTED_AT=start\nPRE_UPDATE_DEGRADED="backup:process_exit"\n'
        + _function(UPDATE.read_text(), "_record_update_history")
        + '\n_record_update_history success "" "container_cc_sync"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )

    assert result.returncode == 0, result.stderr
    row = subprocess.run(
        [
            "python3",
            "-c",
            "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("
            "'select degraded_subsystems from update_history').fetchone()[0])",
            str(db),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert row == "container_cc_sync,backup:process_exit"


def test_tier2_baseline_resolves_abbreviated_commit_objects(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "update.sh").write_text("# changed\n")
    _git(repo, "add", "scripts/update.sh")
    _git(repo, "commit", "-m", "tier2 shadow")
    _git(repo, "branch", base[:8])
    _git(repo, "reset", "--hard", base)
    (repo / "other.txt").write_text("other\n")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "unrelated")
    (repo / "data").mkdir()
    db = repo / "data" / "genesis.db"
    subprocess.run(
        [
            "python3",
            "-c",
            "import sqlite3,sys; con=sqlite3.connect(sys.argv[1]); "
            "con.execute('CREATE TABLE update_history (new_commit TEXT, status TEXT, completed_at TEXT)'); "
            "con.execute('INSERT INTO update_history VALUES (?, \"success\", \"now\")', (sys.argv[2],)); "
            "con.commit()",
            str(db),
            base[:8],
        ],
        check=True,
    )
    script = (
        "set -euo pipefail\n"
        f'GENESIS_ROOT="{repo}"\nVENV_DIR="{tmp_path / "missing-venv"}"\n'
        + _function(UPDATE.read_text(), "_resolve_commit_object")
        + "\n"
        + _block(UPDATE.read_text(), "tier2-baseline-check")
        + '\nif _tier2_pending_since_baseline; then echo pending; else echo clean; fi\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"


def test_modified_ephemeral_file_is_backed_up_before_clear(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "AGENTS.md").write_text("tracked\n")
    (repo / "config").mkdir()
    (repo / "config" / "procedure_triggers.yaml").write_text("tracked\n")
    _git(repo, "add", "AGENTS.md", "config")
    _git(repo, "commit", "-m", "initial")
    (repo / "AGENTS.md").write_text("staged work\n")
    _git(repo, "add", "AGENTS.md")
    (repo / "AGENTS.md").write_text("local work\n")

    script = (
        "set -euo pipefail\n"
        f'GENESIS_ROOT="{repo}"\n'
        f'HOME="{home}"\n'
        + _block(UPDATE.read_text(), "ephemeral-premerge-backup")
    )
    result = subprocess.run(
        ["bash", "-c", script],
        check=True,
        env=_clean_env(),
        capture_output=True,
        text=True,
    )
    backups = list((home / ".genesis" / "premerge-backups").glob("*/AGENTS.md"))
    assert len(backups) == 1
    assert (backups[0] / "current").read_text() == "local work\n"
    assert "local work" in (backups[0] / "worktree.patch").read_text()
    assert "staged work" in (backups[0] / "index.patch").read_text()
    assert (repo / "AGENTS.md").read_text() == "tracked\n"
    assert str(backups[0]) in result.stdout
    assert stat.S_IMODE(backups[0].parent.stat().st_mode) == 0o700


def test_every_success_record_uses_marker_capable_variable() -> None:
    text = UPDATE.read_text()
    calls = re.findall(r'_record_update_history "success" "" "([^"]+)"', text)
    assert calls == ["$_nd_degraded", "$_nd_degraded", "$_p6_degraded"]
    assert "_nd_degraded=\"$(_success_degraded_subsystems" in text
    assert "_p6_degraded=\"$(_success_degraded_subsystems" in text


def test_success_degraded_helper_marks_server_not_restarted() -> None:
    script = (
        "set -euo pipefail\n"
        + _block(UPDATE.read_text(), "success-degraded-subsystems")
        + '\n_success_degraded_subsystems "container_cc_sync" true\n'
        + '_success_degraded_subsystems "container_cc_sync" false\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        check=True,
        env=_clean_env(),
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == [
        "container_cc_sync,genesis-server-not-restarted",
        "container_cc_sync",
    ]


def test_post_merge_recovers_target_from_old_state_merge_parent(tmp_path: Path) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    home = tmp_path / "home"
    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base[:12], "rollback_tag": ""},
        conflict=None,
    )

    assert result.returncode == 0, result.stderr
    deploy_head, rollback_tag = result.stdout.splitlines()[-2:]
    assert deploy_head == incoming
    assert _git(repo, "rev-parse", f"{rollback_tag}^{{commit}}") == base


def test_post_merge_uses_recorded_fetch_head_without_network(tmp_path: Path) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    _git(repo, "update-ref", "refs/genesis-update-head", incoming)
    _git(repo, "remote", "rename", "origin", "gone")
    home = tmp_path / "home"

    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base, "rollback_tag": ""},
        conflict=None,
    )

    assert result.returncode == 0, result.stderr
    deploy_head, _rollback_tag = result.stdout.splitlines()[-2:]
    assert deploy_head == incoming


def test_post_merge_legacy_fetch_head_recovery_needs_no_network(tmp_path: Path) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    _git(repo, "fetch", "origin")
    _git(repo, "remote", "rename", "origin", "gone")
    home = tmp_path / "home"

    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base, "rollback_tag": ""},
        conflict=None,
    )

    assert result.returncode == 0, result.stderr
    deploy_head, _rollback_tag = result.stdout.splitlines()[-2:]
    assert deploy_head == incoming


def test_post_merge_recovers_target_from_conflict_target_commit(tmp_path: Path) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    home = tmp_path / "home"
    result = _run_post_merge_block(
        repo,
        home,
        state=None,
        conflict={"target_commit": incoming, "old_commit": base},
    )

    assert result.returncode == 0, result.stderr
    deploy_head, rollback_tag = result.stdout.splitlines()[-2:]
    assert deploy_head == incoming
    assert _git(repo, "rev-parse", f"{rollback_tag}^{{commit}}") == base


def test_post_merge_rejects_unrelated_merge_parent(tmp_path: Path) -> None:
    repo, base, _feature, _incoming = _unrelated_merge_repo(tmp_path)
    home = tmp_path / "home"
    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base[:12], "rollback_tag": ""},
        conflict=None,
    )

    assert result.returncode != 0
    assert "parent matching the deploy branch's fetched head" in result.stderr
    assert _git(repo, "tag", "--list", "pre-update-*") == ""


def test_saved_old_commit_cannot_be_shadowed_by_a_ref(tmp_path: Path) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    _git(repo, "branch", base[:12], incoming)
    home = tmp_path / "home"
    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base[:12], "rollback_tag": ""},
        conflict=None,
    )

    assert result.returncode == 0, result.stderr
    _deploy_head, rollback_tag = result.stdout.splitlines()[-2:]
    assert _git(repo, "rev-parse", f"{rollback_tag}^{{commit}}") == base


def test_post_merge_rejects_saved_rollback_tag_on_incoming_side(
    tmp_path: Path,
) -> None:
    repo, base, _incoming = _merged_repo(tmp_path)
    home = tmp_path / "home"
    _git(repo, "tag", "pre-update-saved", "HEAD")

    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base, "rollback_tag": "pre-update-saved"},
        conflict=None,
    )

    assert result.returncode != 0
    assert "not on the pre-merge side of HEAD" in result.stderr


def test_post_merge_rejects_wrong_head_before_creating_rollback_tag(
    tmp_path: Path,
) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    home = tmp_path / "home"
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-b", "main")
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    (other / "file.txt").write_text("other\n")
    _git(other, "add", "file.txt")
    _git(other, "commit", "-m", "other")
    wrong = _git(other, "rev-parse", "HEAD")
    _git(repo, "fetch", str(other), "main")
    fetched_wrong = _git(repo, "rev-parse", "FETCH_HEAD")

    result = _run_post_merge_block(
        repo,
        home,
        state={
            "deploy_branch": "main",
            "deploy_head": fetched_wrong or wrong,
            "old_commit": base,
        },
        conflict=None,
    )

    assert result.returncode != 0
    assert "does not contain the fetched deploy head" in result.stderr
    assert _git(repo, "tag", "--list", "pre-update-*") == ""


def test_conflict_context_carries_deploy_target() -> None:
    text = UPDATE.read_text()
    assert 'UC_DEPLOY_BRANCH="$DEPLOY_BRANCH"' in text
    assert 'UC_DEPLOY_HEAD="$DEPLOY_HEAD"' in text
    assert '"deploy_branch": os.environ.get("UC_DEPLOY_BRANCH", "")' in text
    assert '"deploy_head": os.environ.get("UC_DEPLOY_HEAD", "")' in text


def test_conflict_resolution_prompts_merge_saved_deploy_head() -> None:
    text = (REPO_ROOT / "src/genesis/dashboard/routes/updates.py").read_text()
    tier2 = text[text.index("_TIER2_PROMPT"):text.index("_TIER3_PROMPT")]
    tier3 = text[text.index("_TIER3_PROMPT"):text.index("# Files used")]
    for prompt in (tier2, tier3):
        assert "deploy_branch" in prompt
        assert "deploy_head" in prompt
        assert "target_commit" in prompt
        assert "checkout's deploy branch" in prompt
        assert "git merge <resolved-fetched-head> --no-edit" in prompt
        assert "git checkout <resolved-deploy-branch>" in prompt
        assert "git merge origin/main" not in prompt


def test_post_merge_recovers_target_from_durable_conflict_context() -> None:
    text = UPDATE.read_text()
    assert 'CONFLICT_FILE="$HOME/.genesis/update_conflicts.json"' in text
    assert 'python3 - > "$HOME/.genesis/update_conflicts.json.tmp"' in text
    assert '"rollback_tag": os.environ.get("UC_ROLLBACK_TAG", "")' in text
    assert '_read_json_field "$CONFLICT_FILE" deploy_head' in text
    assert '_read_json_field "$CONFLICT_FILE" deploy_branch' in text
    assert '_read_json_field "$CONFLICT_FILE" target_commit' in text


def test_progress_gc_preserves_state_while_conflict_context_exists() -> None:
    text = (REPO_ROOT / "src/genesis/dashboard/routes/updates.py").read_text()
    assert 'if stale and not _CONFLICT_FILE.is_file():' in text
    assert 'conflict_file=_CONFLICT_FILE,' in text


def test_noop_history_does_not_duplicate_pre_update_degraded() -> None:
    text = UPDATE.read_text()
    assert '"${HOST_CC_DEGRADED:-}" "$_nd_server_not_restarted"' in text
