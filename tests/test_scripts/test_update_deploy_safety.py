from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE = REPO_ROOT / "scripts" / "update.sh"


# See test_deploy_checkout.py for why GIT_* is stripped as well as GENESIS_*:
# an inherited GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE redirects these fixture
# commands -- and the ephemeral-premerge-backup block they drive -- at the
# OUTER repository. None of the calls below passed an environment at all.
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


def test_all_deploy_entry_points_use_shared_checkout_guard() -> None:
    for relative in (
        "scripts/update.sh",
        "scripts/bootstrap.sh",
        "scripts/install.sh",
        "scripts/host-setup.sh",
    ):
        text = (REPO_ROOT / relative).read_text()
        assert "lib/deploy_checkout.sh" in text
        assert "genesis_assert_deploy_checkout" in text


def test_host_setup_propagates_selected_deploy_branch() -> None:
    text = (REPO_ROOT / "scripts" / "host-setup.sh").read_text()
    assert 'BRANCH="$_DEPLOY_BRANCH"' in text
    assert "GENESIS_ALLOW_NON_DEPLOY_BRANCH=1 genesis_assert" not in text
    assert 'GENESIS_DEPLOY_BRANCH="$BRANCH" genesis_resolve_deploy_branch' in text
    assert '--env "GENESIS_DEPLOY_BRANCH=$BRANCH"' in text
    assert '--env "GENESIS_PERSIST_DEPLOY_BRANCH=1"' in text


def test_update_merges_and_verifies_exact_fetched_head() -> None:
    text = UPDATE.read_text()
    assert 'DEPLOY_HEAD=$(git -C "$GENESIS_ROOT" rev-parse FETCH_HEAD)' in text
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
    subprocess.run(
        ["git", "-C", str(repo), "init", "-b", "main"],
        check=True,
        env=_clean_env(),
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
        env=_clean_env(),
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        env=_clean_env(),
    )
    (repo / "AGENTS.md").write_text("tracked\n")
    (repo / "config").mkdir()
    (repo / "config" / "procedure_triggers.yaml").write_text("tracked\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "AGENTS.md", "config"],
        check=True,
        env=_clean_env(),
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        env=_clean_env(),
    )
    (repo / "AGENTS.md").write_text("staged work\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "AGENTS.md"],
        check=True,
        env=_clean_env(),
    )
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


def test_post_merge_requires_saved_fetched_deploy_head() -> None:
    text = UPDATE.read_text()
    assert '"deploy_branch": "${DEPLOY_BRANCH:-}"' in text
    assert '"deploy_head": "${DEPLOY_HEAD:-}"' in text
    assert "post-merge update requires the fetched deploy head" in text
    assert 'DEPLOY_HEAD="$_saved_deploy_head"' in text
    assert 'rev-parse HEAD)' not in text[text.index('if [[ "$POST_MERGE" == "false" ]]; then'):text.index('_write_state "fetching"')]


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
        assert "deploy_head" in prompt
        assert "deploy_branch" in prompt
        assert "git merge <deploy_head> --no-edit" in prompt
        assert "git merge origin/main" not in prompt


def test_explicit_host_branch_persists_host_config() -> None:
    text = (REPO_ROOT / "scripts" / "host-setup.sh").read_text()
    assert 'genesis_ensure_deploy_config "$_DEPLOY_BRANCH" 1' in text


def test_post_merge_recovers_target_from_durable_conflict_context() -> None:
    text = UPDATE.read_text()
    assert 'CONFLICT_FILE="$HOME/.genesis/update_conflicts.json"' in text
    assert '"rollback_tag": os.environ.get("UC_ROLLBACK_TAG", "")' in text
    assert '_read_json_field "$CONFLICT_FILE" deploy_head' in text
    assert '_read_json_field "$CONFLICT_FILE" deploy_branch' in text
    saved_head = text.index('DEPLOY_HEAD="$_saved_deploy_head"')
    post_merge = text[saved_head:text.index('_write_state "fetching"', saved_head)]
    assert 'merge-base --is-ancestor "$DEPLOY_HEAD" HEAD' in post_merge


def test_host_branch_persists_only_after_install_succeeds() -> None:
    text = (REPO_ROOT / "scripts" / "host-setup.sh").read_text()
    install = text.index('bash scripts/install.sh $_install_flags')
    persist = text.index('genesis_ensure_deploy_config "$_DEPLOY_BRANCH" 1')
    assert install < persist
    assert 'if [ "$_install_ok" = "1" ] && [ "$_BRANCH_EXPLICIT" = "1" ]; then' in text


def test_progress_gc_preserves_state_while_conflict_context_exists() -> None:
    text = (REPO_ROOT / "src/genesis/dashboard/routes/updates.py").read_text()
    assert 'if stale and not _CONFLICT_FILE.is_file():' in text
    assert 'conflict_file=_CONFLICT_FILE,' in text


def test_noop_history_does_not_duplicate_pre_update_degraded() -> None:
    text = UPDATE.read_text()
    assert '"${HOST_CC_DEGRADED:-}" "$_nd_server_not_restarted"' in text


def test_host_repo_flag_works_outside_git_checkout() -> None:
    text = (REPO_ROOT / "scripts" / "host-setup.sh").read_text()
    assert 'if git -C "$_GENESIS_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then' in text


def test_host_setup_pending_branch_allows_branchless_retry() -> None:
    text = (REPO_ROOT / "scripts" / "host-setup.sh").read_text()
    pending_read = text.index('_PENDING_BRANCH="$(genesis_read_deploy_branch_file "$_PENDING_FILE"')
    pending_match = text.index('if [ "$_CURRENT_BRANCH" = "$_PENDING_RESOLVED" ]; then')
    write_pending = text.index('genesis_write_deploy_pending "$_DEPLOY_BRANCH"')
    install = text.index('bash scripts/install.sh $_install_flags')
    persist = text.index('genesis_ensure_deploy_config "$_DEPLOY_BRANCH" 1')
    clear_pending = text.index('rm -f "$(genesis_deploy_pending_file)"')
    assert pending_read < pending_match < write_pending < install < persist < clear_pending


def test_printed_manual_install_command_carries_the_deploy_branch() -> None:
    """The fallback command printed for a human must run as printed.

    install.sh now ASSERTS the checkout matches the resolved deploy branch, so
    a pasted command without GENESIS_DEPLOY_BRANCH fails the very validation
    this change adds. A parenthetical telling the reader to add the arguments
    is not the same as a command that works -- the reader pastes the command.
    """
    text = (REPO_ROOT / "scripts" / "host-setup.sh").read_text()
    printed = [
        line
        for line in text.splitlines()
        if "bash scripts/install.sh" in line and "incus exec" in line
    ]
    assert printed, "no printed manual install command found"
    for line in printed:
        assert "GENESIS_DEPLOY_BRANCH" in line, (
            f"printed install command omits the deploy branch: {line.strip()}"
        )
        assert "GENESIS_PERSIST_DEPLOY_BRANCH" in line, (
            f"printed install command omits the persist flag: {line.strip()}"
        )


def _container_repo_block(dest: Path) -> str:
    """The existing-repo arm of host-setup.sh, retargeted at a tmp checkout.

    The block is EXTRACTED from the shipped script rather than retyped, so it
    cannot drift from what actually runs. _DEST is rewritten because the real
    value is the hardcoded container path -- executing the block unmodified
    would operate on this machine's own checkout.
    """
    text = (REPO_ROOT / "scripts" / "host-setup.sh").read_text()
    start = text.index("    _DEST=/home/ubuntu/genesis")
    end = text.index("\n    fi\n", start) + len("\n    fi\n")
    block = text[start:end]
    assert "_DEST=/home/ubuntu/genesis" in block
    return block.replace("_DEST=/home/ubuntu/genesis", f'_DEST="{dest}"', 1)


def _two_branch_repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(
        ["git", "-C", str(origin), "init", "-b", "main", "--bare"],
        check=True,
        env=_clean_env(),
    )
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(origin), str(work)],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    for cmd in (
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(
            ["git", "-C", str(work), *cmd], check=True, env=_clean_env()
        )
    (work / "f").write_text("main\n")
    subprocess.run(["git", "-C", str(work), "add", "f"], check=True, env=_clean_env())
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    subprocess.run(
        ["git", "-C", str(work), "checkout", "-b", "stable"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    (work / "f").write_text("stable\n")
    subprocess.run(["git", "-C", str(work), "add", "f"], check=True, env=_clean_env())
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "stable"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-u", "origin", "stable"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    subprocess.run(
        ["git", "-C", str(work), "checkout", "main"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    return origin, work


def _run_block(work: Path, branch: str) -> subprocess.CompletedProcess[str]:
    script = f'_BRANCH="{branch}"\n_REPO_URL="unused"\n' + _container_repo_block(work)
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=_clean_env()
    )


def test_existing_container_checkout_switches_to_the_requested_branch(
    tmp_path: Path,
) -> None:
    """`--branch X` on an EXISTING container must leave the checkout on X.

    install.sh asserts the checkout matches the deploy branch it is handed, so
    an existing-repo path that only pulls turns the flag into a mid-setup
    assertion failure naming the checkout rather than the flag.
    """
    _origin, work = _two_branch_repo(tmp_path)
    assert (
        subprocess.run(
            ["git", "-C", str(work), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            env=_clean_env(),
        ).stdout.strip()
        == "main"
    )

    result = _run_block(work, "stable")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (
        subprocess.run(
            ["git", "-C", str(work), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            env=_clean_env(),
        ).stdout.strip()
        == "stable"
    ), "checkout did not end on the requested deploy branch"


def test_branch_switch_refuses_rather_than_discarding_container_edits(
    tmp_path: Path,
) -> None:
    """The paired control: a switch that would lose local work must FAIL.

    Without this, the test above would pass just as well against a `checkout
    -f`, which is the one implementation that must not ship -- an operator
    with uncommitted work in the container gets told, not overwritten.
    """
    _origin, work = _two_branch_repo(tmp_path)
    (work / "f").write_text("uncommitted container edit\n")

    result = _run_block(work, "stable")

    assert result.returncode != 0, (
        "switch succeeded despite uncommitted work that it would have to "
        f"discard\n{result.stdout}\n{result.stderr}"
    )
    assert (work / "f").read_text() == "uncommitted container edit\n", (
        "local container edit was discarded by the branch switch"
    )
