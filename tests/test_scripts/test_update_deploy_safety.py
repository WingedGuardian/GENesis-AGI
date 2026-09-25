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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    ).stdout.strip()


def _merged_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "incoming")
    (repo / "file.txt").write_text("incoming\n")
    _git(repo, "commit", "-am", "incoming")
    incoming = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "incoming", "-m", "merge incoming")
    return repo, base, incoming


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
        'POST_MERGE=true\nDEPLOY_BRANCH=main\nOLD_TAG=old\nOLD_COMMIT=old\n'
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


def test_update_resolves_branch_from_the_remote_it_fetches() -> None:
    text = UPDATE.read_text()
    remote = text.index('UPDATE_REMOTE="$(_detect_update_remote)"')
    resolve = text.index('genesis_resolve_deploy_branch "$GENESIS_ROOT" "$UPDATE_REMOTE"')
    assert remote < resolve
    assert 'fetch \\\n        "$UPDATE_REMOTE" "+$DEPLOY_BRANCH:$DEPLOY_FETCH_REF"' in text


def test_private_fetch_ref_bypasses_narrow_tracking_refspec(tmp_path: Path) -> None:
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

    _git(work, "fetch", "origin", "+main:refs/genesis-update-head")

    assert _git(work, "rev-parse", "refs/genesis-update-head") == incoming
    _git(work, "push", "--force", "origin", f"{base}:main")
    _git(work, "fetch", "origin", "+main:refs/genesis-update-head")
    assert _git(work, "rev-parse", "refs/genesis-update-head") == base
    assert subprocess.run(
        ["git", "-C", str(work), "show-ref", "--verify", "refs/remotes/origin/main"],
        capture_output=True,
        env=_clean_env(),
    ).returncode != 0


def test_update_merges_and_verifies_fetched_remote_head() -> None:
    text = UPDATE.read_text()
    assert 'DEPLOY_FETCH_REF="refs/genesis-update-head"' in text
    assert 'DEPLOY_HEAD=$(git -C "$GENESIS_ROOT" rev-parse "$DEPLOY_FETCH_REF")' in text
    assert 'merge "$DEPLOY_HEAD" --no-edit' in text
    verify = text.index('merge-base --is-ancestor "$DEPLOY_HEAD" HEAD')
    restart = text.index("--- Restarting services ---")
    success = text.index('_record_update_history "success"', verify)
    assert verify < restart
    assert verify < success


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
