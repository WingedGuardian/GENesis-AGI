"""Tests for scripts/bash_safety_hook.sh.

This hook is the GLOBAL PreToolUse Bash chokepoint loaded via user-level
~/.claude/settings.json, so it fires for ALL sessions including background
DirectSessions and non-genesis projects. Invariants:

1. With GENESIS_BASH_ALLOWLIST unset, behaviour is back-compatible.
2. With GENESIS_BASH_ALLOWLIST set (steward sessions), Bash is restricted to
   the allowlisted binaries; chaining/piping/redirection is blocked.

2026-08 guard-correctness changes exercised here:
  * rm safety DELEGATES to the token-parsing Python guards (no substring FP):
    deep non-protected paths are allowed; protected data dirs + broad/shallow
    targets still block.
  * force-push detection is SEGMENT-scoped: `rm -f x && git push` is not a
    force push.
  * inside a genesis checkout, an INTERACTIVE session's push/PR/merge gates are
    skipped (the richer project-level git_push_guard covers them); a DISPATCHED
    session keeps the belt.

Default cwd for _run is a NON-genesis temp dir, so the push/PR/merge gates run
(as they did before the dedup). In-genesis behavior is covered explicitly.
"""

import atexit
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = _REPO_ROOT / "scripts" / "bash_safety_hook.sh"

# A stable non-git directory so `git rev-parse --git-common-dir` fails →
# in_genesis=0 → the push/PR/merge gates run (pre-dedup behavior).
_OUTSIDE = tempfile.mkdtemp(prefix="bash_safety_outside_")
atexit.register(shutil.rmtree, _OUTSIDE, ignore_errors=True)


def _run(
    command: str,
    env_extra: dict[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook with a Bash command on stdin; return the completed proc.

    Inherits the real environment (the hook needs jq/python3/git on PATH, as in
    prod) but clears GENESIS_BASH_ALLOWLIST so each case controls it explicitly.
    Runs from a non-genesis cwd by default.
    """
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = dict(os.environ)
    env.pop("GENESIS_BASH_ALLOWLIST", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or _OUTSIDE,
    )


# --- Back-compat: no allowlist env → unchanged behaviour ---


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr view 905 --repo Shubhamsaboo/awesome-llm-apps",
        "ls -la",
        "python -m pytest tests/",
        "git status",
    ],
)
def test_no_allowlist_allows_normal_commands(cmd):
    """Without the allowlist env, ordinary commands pass (exit 0)."""
    assert _run(cmd).returncode == 0


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /",
        "git reset --hard HEAD~1",
        "git clean -fd",
        "git push --force origin main",
    ],
)
def test_no_allowlist_still_blocks_destructive(cmd):
    """Existing destructive-op blocks must still fire (exit 2)."""
    assert _run(cmd).returncode == 2


# --- git clean: widened, dry-run-aware floor (2026-08-24 FIX 1) ---


class TestGitCleanFloor:
    """`git clean` is UNrecoverable (`git stash create` can't capture untracked
    files), so it keeps a real block — but a quote-NAIVE regex mis-fires on
    `git checkout clean-branch` etc., so bash_safety DELEGATES clean to the
    precise, quote-aware git_discard_guard.py (a closed-set whitelist) and
    propagates ONLY its exit-2 clean block; a coarse regex survives just as the
    python-LESS fallback. These run with real python3, so they exercise the
    delegated (authoritative) path. The guard's closed-set whitelist OVER-BLOCKS
    exotic-but-safe dry-run forms (`-nf`, `-n -f`) — safe direction, escapable
    with `# discard-override`; see test_git_discard_guard.py for the guard logic."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git clean -f",
            "git clean -fd",
            "git clean -fdx",
            "git clean --force",
            "git clean -xf",
            "git clean -x -f",
            "git clean",  # bare
            "git clean -d",  # no dry-run token
            "git clean -f .",  # path arg
            "git clean -f -e keepme",  # exclude flag
            "git -C /tmp clean -f",  # -C before the verb
            "git clean -nd && git clean -f",  # 2nd segment
            "git clean -nd & git clean -f",  # & background — 2nd segment
            "git clean -f -e -n",  # exotic exclude value — guard blocks
            "git clean -f -- -nine",  # exotic dash-named pathspec — guard blocks
            "git clean -nf",  # dry-run cluster: guard OVER-blocks (safe)
            "git clean -n -f",  # dry-run + force: over-block (safe)
        ],
    )
    def test_clean_non_dry_run_blocked(self, cmd):
        assert _run(cmd).returncode == 2

    @pytest.mark.parametrize(
        "cmd",
        [
            "git clean -n",
            "git clean -nd",
            "git clean -dn",
            "git clean --dry-run",
            "git clean -nd && echo ok",
            "git clean -f  # discard-override",  # sanctioned escape honored
            # false-blocks a naive `clean` match would cause — the guard allows:
            "git checkout clean-branch",
            "git diff clean.py",
            'git commit -m "clean up the repo"',
        ],
    )
    def test_clean_dry_run_or_non_subcommand_allowed(self, cmd):
        assert _run(cmd).returncode == 0


class TestGitCleanFailsClosedOnGuardCrash:
    """architect SHOULD-FIX (2026-08-24): bash_safety delegates clean to the
    guard and propagates ONLY exit 2. A PRESENT-but-CRASHING guard (rc != 0,2 —
    e.g. a partially-synced scripts/hooks/ missing a sibling module) must NOT
    leave the UNrecoverable clean verb fail-OPEN: the coarse fallback runs on a
    crash too (clean fails CLOSED), while the recoverable verbs stay advisory
    (the fallback only ever matches `git clean`)."""

    def _run_broken(self, tmp_path: Path, cmd: str) -> subprocess.CompletedProcess:
        scripts = tmp_path / "scripts"
        (scripts / "hooks").mkdir(parents=True)
        shutil.copy(HOOK, scripts / "bash_safety_hook.sh")
        # a guard that raises at import time -> python exits 1 (a crash, not 2)
        (scripts / "hooks" / "git_discard_guard.py").write_text(
            "import _genesis_nonexistent_module_xyz_  # noqa\n"
        )
        payload = json.dumps({"tool_input": {"command": cmd}, "tool_name": "Bash"})
        env = dict(os.environ)
        env.pop("GENESIS_BASH_ALLOWLIST", None)
        return subprocess.run(
            ["bash", str(scripts / "bash_safety_hook.sh")],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            cwd=_OUTSIDE,
        )

    @pytest.mark.parametrize("cmd", ["git clean -f", "git clean --force", "git clean -fd"])
    def test_clean_still_blocked_on_crash(self, tmp_path, cmd):
        assert self._run_broken(tmp_path, cmd).returncode == 2

    @pytest.mark.parametrize(
        "cmd", ["git checkout foo", "git reset --soft HEAD~1", "git clean -nd", "git clean -f  # discard-override"]
    )
    def test_non_destructive_advisory_on_crash(self, tmp_path, cmd):
        assert self._run_broken(tmp_path, cmd).returncode != 2


# --- rm delegation: FP cluster fixed, real dangers still block ---


class TestRmDelegation:
    """rm safety now delegates to the token-parsing Python guards."""

    def test_deep_non_protected_allowed(self):
        """The KEY false-positive fix: a deep (>=4) non-protected path that the
        old `*"rm -rf /"*` glob blocked is now allowed (user-approved policy)."""
        assert _run("rm -rf /tmp/a/b/c/d").returncode == 0

    def test_deep_home_path_allowed(self):
        home = os.path.expanduser("~")
        assert _run(f"rm -rf {home}/tmp/scratch/oldbuild").returncode == 0

    def test_broad_root_blocked(self):
        assert _run("rm -rf /").returncode == 2

    def test_shallow_dotdir_blocked(self):
        """Shallow targets (depth<4) stay blocked per policy."""
        assert _run("rm -rf .venv").returncode == 2

    def test_protected_data_dir_blocked(self):
        home = os.path.expanduser("~")
        r = _run(f"rm -rf {home}/.claude/projects")
        assert r.returncode == 2

    def test_production_db_blocked(self):
        home = os.path.expanduser("~")
        r = _run(f"rm {home}/genesis/data/genesis.db")
        assert r.returncode == 2

    def test_mention_only_not_blocked(self):
        """A protected path merely MENTIONED (not an rm target) — old FP."""
        home = os.path.expanduser("~")
        assert _run(f"rm /tmp/x && echo {home}/backups").returncode == 0


# --- Force-push detection: FLAG token in the SAME segment ---


@pytest.mark.parametrize(
    "cmd",
    [
        "git push -f origin main",
        "git push --force origin main",
        "git push origin main --force",
        "git push --force-with-lease origin main",
        "git push origin HEAD -f",
        "git push -fv origin main",  # bundled short flags (force + verbose)
        "git push -uf origin main",  # bundled (set-upstream + force)
    ],
)
def test_force_push_variants_blocked(cmd):
    """Real force pushes (a standalone -f flag or any --force* variant) are
    hard-blocked (exit 2)."""
    assert _run(cmd).returncode == 2


@pytest.mark.parametrize(
    "cmd",
    [
        "git push origin learning/lc2-honest-skill-funnel",  # '-f' inside 'skill-funnel'
        "git push origin bug-fix",  # '-f' inside 'bug-fix'
        "git push origin feature/new-flow",  # '-f' inside 'new-flow'
        "git push origin HEAD",
        "git push origin main",
    ],
)
def test_normal_push_with_dash_f_in_branch_not_blocked(cmd):
    """A branch name that merely CONTAINS the literal '-f' must NOT be treated
    as a force push (soft reminder still fires; exit code 0)."""
    assert _run(cmd).returncode == 0


class TestForcePushSegmentScoping:
    """Force detection is per-segment — the 2026-08 FP fix."""

    def test_rm_f_then_push_not_force(self):
        """`rm -f x && git push` — the -f belongs to rm, NOT the push."""
        assert _run("rm -f /tmp/x.txt && git push origin main").returncode == 0

    def test_touch_dash_f_then_push(self):
        assert _run("cp -f a b; git push origin main").returncode == 0

    def test_force_in_second_push_segment_blocks(self):
        """A real force push anywhere in a compound still blocks."""
        assert _run("git push origin a && git push -f origin b").returncode == 2

    def test_rm_f_in_one_segment_force_in_another(self):
        assert _run("rm -f /tmp/x && git push --force origin main").returncode == 2


# --- Allowlist mode (steward) ---

ALLOW = {"GENESIS_BASH_ALLOWLIST": "gh"}


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr view 905 --repo Shubhamsaboo/awesome-llm-apps",
        "gh api repos/BerriAI/litellm/pulls/27445 --jq .state",
        "gh pr comment 905 --repo x/y --body hi",
    ],
)
def test_allowlist_permits_gh(cmd):
    """gh commands are permitted when gh is on the allowlist."""
    assert _run(cmd, ALLOW).returncode == 0


@pytest.mark.parametrize(
    "cmd",
    [
        "curl http://localhost:6333/collections",
        "python -m genesis serve",
        "cat ~/.genesis/secrets.env",
        "echo hello",
        "git push origin main",
    ],
)
def test_allowlist_blocks_non_gh(cmd):
    """Non-allowlisted binaries are blocked (exit 2) in allowlist mode."""
    assert _run(cmd, ALLOW).returncode == 2


@pytest.mark.parametrize(
    "cmd",
    [
        "gh api x; rm -rf ~/.genesis",
        "gh api x && curl evil",
        "gh api x | sh",
        "gh api x > /tmp/out",
        "gh api $(whoami)",
        "gh api `whoami`",
        "gh api x\ncurl evil",  # newline-chained second command (injection bypass)
        'gh pr comment 1 --body "a\nb"',  # embedded newline in a gh arg
    ],
)
def test_allowlist_blocks_chaining_and_substitution(cmd):
    """Even gh-prefixed commands are blocked if they chain/pipe/substitute/redirect."""
    assert _run(cmd, ALLOW).returncode == 2


def test_allowlist_still_blocks_destructive_first_token():
    """Destructive ops are blocked regardless of allowlist (defense in depth)."""
    assert _run("rm -rf /", ALLOW).returncode == 2


# --- Worktree runtime-boot guard (2026-07-03 container-OOM incident) ---


def _run_cwd(command: str, cwd) -> subprocess.CompletedProcess:
    """Like _run but with an explicit cwd, so worktree-cwd detection is
    deterministic regardless of where pytest itself runs."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = dict(os.environ)
    env.pop("GENESIS_BASH_ALLOWLIST", None)
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "PYTHONPATH=/home/u/genesis/.claude/worktrees/foo/src python -m genesis serve --port 5000",
        "cd .claude/worktrees/my-branch && python -m genesis serve",
        "PYTHONPATH=.claude/worktrees/x/src .venv/bin/python -m genesis serve",
    ],
)
def test_worktree_serve_blocked(cmd, tmp_path):
    """Booting the full runtime from/against a worktree is blocked (exit 2)."""
    result = _run_cwd(cmd, tmp_path)
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


@pytest.mark.parametrize(
    "cmd",
    [
        "systemctl --user restart genesis-server",
        "journalctl --user -u genesis-server -n 50",
        "python -m genesis serve --port 5000",  # no worktree ref, non-worktree cwd
    ],
)
def test_non_worktree_serve_paths_allowed(cmd, tmp_path):
    """Server management and plain serve (outside a worktree) pass this guard."""
    assert _run_cwd(cmd, tmp_path).returncode == 0


# --- gh pr merge: PR resolution fails CLOSED (2026-07-10 P1 triage) ---
# Run from a non-genesis cwd (via _run's default) so the merge gate is active;
# inside genesis, an interactive session defers to git_push_guard (tested below).


def _gh_stub(tmp_path: Path, script: str) -> dict[str, str]:
    """Put a fake `gh` first on PATH so no test touches the network."""
    stub = tmp_path / "gh"
    stub.write_text(f"#!/usr/bin/env bash\n{script}\n")
    stub.chmod(0o755)
    return {"PATH": f"{tmp_path}:{os.environ['PATH']}"}


def test_merge_no_arg_unresolvable_blocks(tmp_path):
    """No number in the command AND no open PR for the branch -> exit 2."""
    env = _gh_stub(tmp_path, "exit 1")
    result = _run("gh pr merge --squash --admin", env_extra=env)
    assert result.returncode == 2
    assert "cannot resolve" in result.stderr


def test_merge_no_arg_resolves_branch_pr(tmp_path):
    """No number, but the branch has an open PR -> gates run against it."""
    env = _gh_stub(
        tmp_path,
        'case "$*" in *"--json number"*) echo 42;; '
        '*"--json mergeable"*) echo MERGEABLE;; esac',
    )
    result = _run("gh pr merge --squash", env_extra=env)
    assert result.returncode == 0
    assert "PR #42" in result.stderr


def test_merge_numbered_conflicting_blocks(tmp_path):
    env = _gh_stub(
        tmp_path,
        'case "$*" in *"--json mergeable"*) echo CONFLICTING;; esac',
    )
    result = _run("gh pr merge 123 --squash", env_extra=env)
    assert result.returncode == 2
    assert "merge conflicts" in result.stderr


# --- gh pr merge: PR number after a FLAG must resolve correctly ---
# (2026-07-10 review: an anchored "merge <digits>" match missed
#  `gh pr merge --admin 123` and fell back to the WRONG branch PR.)


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr merge --admin 123",
        "gh pr merge 123 --admin",
        "gh pr merge --squash 123 --admin",
        "gh pr merge https://github.com/o/r/pull/123 --squash",
    ],
)
def test_merge_number_after_flag_resolves_correctly(tmp_path, cmd):
    """The PR named in the command is checked, regardless of flag order.

    The gh stub returns branch-PR #55; a correct parse must report #123,
    not #55.
    """
    env = _gh_stub(
        tmp_path,
        'case "$*" in *"--json number"*) echo 55;; *"--json mergeable"*) echo MERGEABLE;; esac',
    )
    result = _run(cmd, env_extra=env)
    assert "PR #123" in result.stderr
    assert "PR #55" not in result.stderr


def test_merge_digits_in_quoted_subject_not_a_pr(tmp_path):
    """Digits inside a quoted --subject must not be taken as the PR."""
    env = _gh_stub(
        tmp_path,
        'case "$*" in *"--json number"*) echo 77;; *"--json mergeable"*) echo MERGEABLE;; esac',
    )
    result = _run('gh pr merge --subject "merge 999 now"', env_extra=env)
    assert "PR #77" in result.stderr
    assert "999" not in result.stderr


def test_merge_chained_command_digits_ignored(tmp_path):
    """`gh pr merge 123; echo 456` must check PR #123, not #456."""
    env = _gh_stub(
        tmp_path,
        'case "$*" in *"--json mergeable"*) echo MERGEABLE;; esac',
    )
    result = _run("gh pr merge 123 --admin; echo 456", env_extra=env)
    assert "PR #123" in result.stderr
    assert "456" not in result.stderr


def test_merge_cross_repo_uses_repo_flag(tmp_path):
    """--repo threads through to the mergeable check so a cross-repo merge gates
    the RIGHT repo. The stub records its argv to a file (the hook suppresses
    gh's stderr with 2>/dev/null, so a file is the only visible channel)."""
    argfile = tmp_path / "gh_args"
    env = _gh_stub(
        tmp_path,
        f'echo "$*" >> "{argfile}"; case "$*" in *"--json mergeable"*) echo MERGEABLE;; esac',
    )
    result = _run("gh pr merge 43 --repo octo/voice --squash", env_extra=env)
    assert result.returncode == 0
    recorded = argfile.read_text()
    assert "--repo octo/voice" in recorded
    assert "43" in recorded  # gated the explicit PR number, in the named repo


# --- Genesis dedup (D4): interactive-in-genesis skips the duplicate gates ---


class TestGenesisDedup:
    """Inside a genesis checkout, the richer project-level git_push_guard owns
    the push/PR/merge gates for an interactive session, so this belt steps
    aside (no duplicate live gh calls). A dispatched session keeps the belt.

    cwd = the repo root (which carries scripts/hooks/git_push_guard.py) → the
    hook's in_genesis detection fires. Portable: true for the CI checkout too.
    """

    _GENESIS_CWD = str(_REPO_ROOT)

    def test_interactive_push_reminder_skipped(self):
        r = _run("git push origin main", cwd=self._GENESIS_CWD)
        assert r.returncode == 0
        assert "STOP: git push" not in r.stderr  # git_push_guard handles it

    def test_interactive_pr_create_skipped(self):
        r = _run("gh pr create --fill", cwd=self._GENESIS_CWD)
        assert r.returncode == 0
        assert "gh pr create detected" not in r.stderr

    def test_interactive_merge_gate_skipped(self, tmp_path):
        """The expensive duplicate — the merge gate's live gh calls — is not run."""
        env = _gh_stub(tmp_path, 'echo "SHOULD-NOT-RUN" >&2; exit 1')
        r = _run("gh pr merge --squash --admin", env_extra=env, cwd=self._GENESIS_CWD)
        assert r.returncode == 0
        assert "SHOULD-NOT-RUN" not in r.stderr
        assert "cannot resolve" not in r.stderr

    def test_dispatched_keeps_the_belt(self):
        r = _run(
            "git push origin main",
            env_extra={"GENESIS_CC_SESSION": "1"},
            cwd=self._GENESIS_CWD,
        )
        assert r.returncode == 0
        assert "STOP: git push" in r.stderr  # belt still fires for autonomous

    def test_reset_hard_still_blocks_in_genesis(self):
        """reset --hard is bash_safety-exclusive (git_push_guard doesn't cover
        it), so it must fire BEFORE the dedup skip."""
        r = _run("git reset --hard HEAD~1", cwd=self._GENESIS_CWD)
        assert r.returncode == 2

    def test_force_push_still_blocks_in_genesis(self):
        r = _run("git push -f origin main", cwd=self._GENESIS_CWD)
        assert r.returncode == 2

    def test_broad_rm_still_blocks_in_genesis(self):
        r = _run("rm -rf /", cwd=self._GENESIS_CWD)
        assert r.returncode == 2
# --- pip editable arm + force-removal arm: anchored predicates (2026-09-03) ---
#
# Both arms matched raw command text, so the phrase inside a grep pattern, a
# heredoc body, a docstring or a commit message read as the operation. A block
# discards the WHOLE Bash call, so a false match on the last step silently threw
# away the file writes in the earlier ones while the error named only the rule
# that fired. Reproduced four times on 2026-09-03, twice while editing this file.
#
# EVERY allow-case below carries a worktree marker ON PURPOSE. Without one, the
# old predicate's second condition ("is a worktree named, or is cwd a worktree?")
# is false anyway, the decision is never reached, and the case passes against the
# OLD hook too — i.e. it pins nothing. A first version of these tests had exactly
# that defect: 12 of 14 passed unchanged against the pre-fix hook. Each allow-case
# here was verified to return rc=2 from origin/main's hook and rc=0 from this one,
# EXCEPT the one explicitly labelled forward-looking below, which returns rc=0 from
# BOTH and says so in its own docstring.
#
# Built from a fragment so this file's own text cannot trip the live hook when a
# future session greps or edits it — the same defect, one level up.
_E = "-e"
_WT = "/srv/genesis/.claude/worktrees/somebranch"


class TestPipEditableArm:
    """Both directions. A benign-block rate of zero reads identically for a
    correct guard and an inert one, so every allow-case is paired with a block
    case that must still fire."""

    @pytest.mark.parametrize(
        "cmd",
        [
            f"pip install {_E} {_WT}",
            f"pip install --editable {_WT}",
            f"pip install {_E}{_WT}",
            f"python -m pip install {_E} {_WT}",
            f"python3.12 -m pip install {_E} {_WT}",
            f"cd /tmp && pip install {_E} {_WT}",
            f"VIRTUAL_ENV=/x pip install {_E} {_WT}",
            f"pip install --editable={_WT}",
        ],
    )
    def test_real_editable_install_to_a_worktree_still_blocks(self, cmd):
        """TRUE-POSITIVE CONTROL — the reason the arm exists."""
        r = _run(cmd)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "PYTHONPATH" in r.stderr

    def test_flag_must_belong_to_the_pip_command(self):
        """The decoupling defect: two independent whole-command greps let an
        unrelated `-e` (here, grep's) satisfy the editable half while an
        unrelated path satisfied the worktree half — a block with no editable
        install anywhere in the command."""
        r = _run(f"pip install ruff && ls {_WT} && grep {_E} foo /dev/null")
        assert r.returncode == 0, r.stderr

    def test_long_flag_containing_dash_e_is_not_an_editable_install(self):
        """`--extra-index-url` contains `-e`; the old unanchored alternative
        matched inside it."""
        r = _run(f"pip install requests --extra-index-url https://example.invalid/s && ls {_WT}")
        assert r.returncode == 0, r.stderr

    @pytest.mark.parametrize(
        "cmd",
        [
            f"grep -rn 'pip install {_E}' {_WT}",
            f"echo 'never pip install {_E} from a worktree' >> {_WT}/notes.md",
            f"git commit -m 'docs: explain pip install {_E} risk in {_WT}'",
        ],
    )
    def test_mention_only_is_not_an_install(self, cmd):
        r = _run(cmd)
        assert r.returncode == 0, r.stderr

    def test_heredoc_body_mentioning_it_is_not_an_install(self):
        """The exact shape that cost a file write on 2026-09-03: a heredoc whose
        BODY quotes the phrase. The block discarded the whole call, the write
        included, and reported only the pip rule."""
        r = _run(
            f"cat > {_WT}/probe_doc.py <<'PYEOF'\n"
            f'"""Use PYTHONPATH, never pip install {_E} from a worktree."""\n'
            "PYEOF"
        )
        assert r.returncode == 0, r.stderr

    def test_pip_subcommand_other_than_install_is_ignored(self):
        """Forward-looking only: this does NOT discriminate old from new (the old
        predicate required `pip install` too, so it allowed this as well). Kept as
        a pin against a future widening to any pip subcommand, and labelled so it
        is not mistaken for a regression test."""
        r = _run(f"pip download {_E} {_WT}")
        assert r.returncode == 0, r.stderr


class TestWorktreeForceRemovalArm:
    """The same class, six lines below the pip arm — fixed together, because
    fixing only the arm that was reported is fixing the list, not the class."""

    def test_force_removal_still_blocks(self):
        r = _run("git worktree remove --force /tmp/wt")
        assert r.returncode == 2, r.stdout + r.stderr

    def test_short_force_flag_still_blocks(self):
        r = _run("git worktree remove -f /tmp/wt")
        assert r.returncode == 2, r.stdout + r.stderr

    def test_global_flag_before_the_subcommand_still_blocks(self):
        """TRUE-POSITIVE CONTROL, not a regression pin. MEASURED old=BLOCK /
        new=BLOCK: this hook's old predicate never required `git` at all, so a
        global flag was never a hole HERE. (It was one in worktree_cwd_guard,
        whose own test at test_worktree_guard.py is the genuine pin — measured
        old=ALLOW / new=BLOCK.) The `-C` operand is inert; the verdict is
        identical with any path."""
        r = _run("git -C /srv/genesis worktree remove --force /tmp/wt")
        assert r.returncode == 2, r.stdout + r.stderr

    @pytest.mark.parametrize(
        "cmd",
        [
            "grep -rn 'worktree remove --force' /dev/null",
            'echo "worktree remove --force is blocked"',
            "git worktree remove /tmp/wt && rm -f /tmp/x",
        ],
    )
    def test_mention_or_unrelated_force_flag_is_allowed(self, cmd):
        """The last case is the segment-scoping one: an `rm -f` in a LATER
        segment is not a force flag on the removal."""
        r = _run(cmd)
        assert r.returncode == 0, r.stderr


class TestHookIsSyntacticallyValid:
    """A syntax error in THIS file blocks every Bash command on the machine.

    bash exits 2 on a syntax error, and Claude Code reads a PreToolUse exit 2 as
    BLOCK — so a typo here is a self-inflicted lockout that also blocks the
    command needed to undo it. The hook is wired at USER level, so it is not
    scoped to one project. CI had no shell syntax gate when this was added
    (verified 2026-09-03); this is that gate.
    """

    def test_bash_n_parses_the_hook(self):
        r = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
