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


class TestDiscardedWriteNote:
    """The refusal names the writes it threw away.

    A PreToolUse block discards the WHOLE Bash call, not the step objected to, so
    a write chained with a refused step is lost SILENTLY. This script has 18
    separate ``exit 2`` sites and no shared emitter, so ONE ``EXIT`` trap covers
    them all rather than refactoring an 18KB safety script.

    The trap sits in a SAFETY script, so every case asserts the exit code
    directly: a note that cost a block would be far worse than no note at all.
    """

    #: Substring-blocked by this hook with no external dependencies. Built rather
    #: than written literally so the hook's own whole-command scan does not fire
    #: on a developer command that merely MENTIONS it.
    _BLOCKER = "git reset --" + "hard"
    #: The note's stable marker. Spelled ONCE: the absence assertions below are
    #: vacuous against a stale string, and a reworded note would silently turn three
    #: of these tests green while emitting nothing.
    _MARKER = "ENTIRE command was discarded"

    def test_a_block_reports_that_the_whole_command_went(self):
        r = _run(f"cp a b && {self._BLOCKER}")
        assert r.returncode == 2
        assert self._MARKER in r.stderr

    def test_the_note_does_NOT_name_the_discarded_command_or_its_files(self):
        """The contract that replaced the file list, asserted end-to-end.

        Deriving which operands were files meant modelling each tool's option
        grammar — unbounded, and measured as a fail-open in this very script. A
        future 'small improvement' that reintroduces a name fails here.
        """
        r = _run(f"cat > /tmp/zz.txt <<EOF\nbody\nEOF\n{self._BLOCKER}")
        assert r.returncode == 2
        assert self._MARKER in r.stderr
        assert "/tmp/zz.txt" not in r.stderr

    def test_a_block_that_discarded_NOTHING_stays_silent(self):
        """The zero-false-positive property: silence when the refused step was the
        whole call, so nothing was carried alongside it."""
        r = _run(self._BLOCKER)
        assert r.returncode == 2
        assert self._MARKER not in r.stderr

    def test_an_ALLOWED_command_still_exits_zero_and_says_nothing(self):
        r = _run("echo hello world")
        assert r.returncode == 0
        assert self._MARKER not in r.stderr

    def test_an_allowed_command_WITH_a_write_says_nothing(self):
        """The note is emitted only at a refusal. A write on an ALLOWED command
        was not discarded, and saying otherwise would fire on ordinary work."""
        r = _run("cp a b && echo done")
        assert r.returncode == 0
        assert self._MARKER not in r.stderr

    def _run_detached_copy(self, tmp_path, command):
        """Run a copy of the hook from a directory with no `hooks/` beside it, so
        the trap cannot find its helper."""
        copy = tmp_path / "bash_safety_hook.sh"
        copy.write_text(HOOK.read_text())
        env = dict(os.environ)
        env.pop("GENESIS_BASH_ALLOWLIST", None)
        return subprocess.run(
            ["bash", str(copy)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            env=env,
            cwd=_OUTSIDE,
        )

    def test_a_BROKEN_helper_cannot_turn_a_block_into_an_allow(self, tmp_path):
        """The decisive safety property. If this ever fails, a COSMETIC note has
        acquired the power to defeat a refusal."""
        r = self._run_detached_copy(tmp_path, f"cp a b && {self._BLOCKER}")
        assert r.returncode == 2, "a missing helper must not turn a block into an allow"
        assert self._MARKER not in r.stderr

    def test_a_BROKEN_helper_cannot_turn_an_allow_into_a_block(self, tmp_path):
        """The other direction, which a `set -e` or a stray `exit` would break."""
        assert self._run_detached_copy(tmp_path, "echo hi").returncode == 0

    def test_a_missing_helper_adds_no_noise_to_the_refusal(self, tmp_path):
        """An unavailable helper is a silent no-op, not a stderr contributor.

        A detached copy of this script has no `hooks/` beside it, so the trap's
        python3 call would print `can't open file …discarded_write.py` into the
        refusal's stderr — failure output from a cosmetic helper, attached to an
        unrelated block. The trap must check availability and stand down.
        """
        r = self._run_detached_copy(tmp_path, f"cp a b && {self._BLOCKER}")
        assert r.returncode == 2
        assert "discarded_write.py" not in r.stderr, (
            "the trap leaked its own helper-invocation failure into the refusal"
        )
        assert "Traceback" not in r.stderr

    def test_a_partially_synced_hooks_dir_adds_no_traceback_to_the_refusal(self, tmp_path):
        """The availability check's blind spot: helper PRESENT, its import absent.

        `[ -r …/discarded_write.py ]` passes, python3 runs, and an unguarded
        module-level `from shell_parse import …` tracebacks into the refusal's
        stderr — the same noise class as the missing helper, one dependency
        deeper. The helper's own contract says every entry point degrades to
        silence, so the import must too.
        """
        copy = tmp_path / "bash_safety_hook.sh"
        copy.write_text(HOOK.read_text())
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        # the helper alone — deliberately NOT its shell_parse dependency
        hooks.joinpath("discarded_write.py").write_text(
            (HOOK.parent / "hooks" / "discarded_write.py").read_text()
        )
        env = dict(os.environ)
        env.pop("GENESIS_BASH_ALLOWLIST", None)
        r = subprocess.run(
            ["bash", str(copy)],
            input=json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": f"cp a b && {self._BLOCKER}"}}
            ),
            capture_output=True,
            text=True,
            env=env,
            cwd=_OUTSIDE,
        )
        assert r.returncode == 2, "a broken import must not turn a block into an allow"
        assert "Traceback" not in r.stderr, (
            "the helper's import failure leaked into the refusal"
        )
        assert "ModuleNotFoundError" not in r.stderr

    def test_the_note_appears_exactly_once_when_a_delegated_guard_emitted_it(self):
        """A delegated Python guard that already printed the note (its stderr is
        passed through) must suppress the EXIT trap's copy — for EVERY delegation
        site, not just the rm one. `git clean` routes through git_discard_guard,
        which calls the helper at its own exit-2 sites; without the suppression
        flag the trap re-derives it and the operator reads the note twice (and
        the hook pays the parse twice inside one 5s budget — the exact mechanism
        the module docstring records as letting a refused `rm -rf` run)."""
        r = _run("cp a b && git clean -fd")
        assert r.returncode == 2
        assert r.stderr.count(self._MARKER) == 1, r.stderr

    def test_the_rm_delegation_also_emits_the_note_exactly_once(self):
        """The control on the site that already carries the suppression flag."""
        r = _run("cp a b && rm -rf /")
        assert r.returncode == 2
        assert r.stderr.count(self._MARKER) == 1, r.stderr
