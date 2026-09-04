"""Tests for scripts/review_enforcement_commit.py — the commit review gate.

Focus (post-#1227 hardening):
- The gate is *satisfiable without gstack*: ``review_state.py mark`` writes a
  marker on agent-output evidence alone (gstack skill-usage telemetry is
  advisory, not required — it is absent on most hosts).
- The ``# review-override`` token bypasses ONLY the review rule, must be a
  genuine trailing shell comment (outside quotes), and denies-with-explanation
  when buried in the commit message (where it would leak into public history).

Install-agnostic: builds a throwaway git repo under ``tmp_path`` and points
``HOME`` at another temp dir, so the real per-worktree markers under ``~/.genesis/review_markers/`` and
any concurrent session's markers are never touched. No network, no live server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _REPO_ROOT / "scripts" / "review_enforcement_commit.py"
_REVIEW_STATE = _REPO_ROOT / "scripts" / "review_state.py"
_INVALIDATE = _REPO_ROOT / "scripts" / "review_invalidate_on_commit.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo on a feature branch with a staged (unreviewed) change."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "f.py").write_text("base = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "feature/x")
    # A .py change: real code that Rule 2 must gate (a docs/config extension like
    # .txt is now legitimately exempt by the Change-4 skip, so it cannot stand in
    # for "unreviewed code" here).
    (r / "f.py").write_text("base = 2\n")  # staged below → has_code_changes
    _git(r, "add", "-A")
    return r


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".genesis").mkdir(parents=True)
    return h


def _run_hook(
    command: str, repo: Path, home: Path, payload_cwd: str | None = None
) -> subprocess.CompletedProcess:
    body = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "test",
    }
    if payload_cwd is not None:
        body["cwd"] = payload_cwd
    payload = json.dumps(body)
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _mark(repo: Path, home: Path) -> subprocess.CompletedProcess:
    """Run `review_state.py mark` with fresh agent-output evidence, no gstack.

    `mark` now requires an explicit review outcome; a defect-bearing `--defects` is the
    neutral choice for these Rule-0/1/2 tests (they don't exercise the escalation streak).
    """
    (home / ".genesis" / "last_code_review.txt").write_text("adversarial review: OK\n")
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [
            sys.executable,
            str(_REVIEW_STATE),
            "mark",
            "--agent-output",
            str(home / ".genesis" / "last_code_review.txt"),
            "--defects",
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ── Rule 2: review required ──────────────────────────────────────────────


def test_plain_commit_blocked_without_review(repo: Path, home: Path) -> None:
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 2
    assert "BLOCKED" in res.stderr
    assert "without review" in res.stderr


def test_git_commit_only_mentioned_in_string_not_gated(repo: Path, home: Path) -> None:
    """A command that merely MENTIONS 'git commit' (no executed commit) must
    not be gated — the cheap regex early-out is confirmed against real segments."""
    res = _run_hook("echo 'reminder: run git commit later'", repo, home)
    assert res.returncode == 0, res.stderr


def test_trailing_override_allows(repo: Path, home: Path) -> None:
    res = _run_hook('git commit -m "wip"  # review-override', repo, home)
    assert res.returncode == 0, res.stderr
    assert "review-override honored" in res.stderr


def test_override_inside_message_denied(repo: Path, home: Path) -> None:
    """Token buried in the -m string would leak into history → deny + explain."""
    res = _run_hook('git commit -m "wip # review-override"', repo, home)
    assert res.returncode == 2
    assert "not a clean trailing shell" in res.stderr


def test_override_jammed_into_unquoted_word_denied(repo: Path, home: Path) -> None:
    """`-m x#review-override` (no space) → the # is literal in the message."""
    res = _run_hook("git commit -m x#review-override", repo, home)
    assert res.returncode == 2
    assert "not a clean trailing shell" in res.stderr


def test_override_with_single_quoted_message(repo: Path, home: Path) -> None:
    """Trailing override after a single-quoted message is honored."""
    res = _run_hook("git commit -m 'wip work'  # review-override", repo, home)
    assert res.returncode == 0, res.stderr
    assert "review-override honored" in res.stderr


# ── Unresolvable cwd: accurate teach message, never the branch lie ────────
#
# The recurring worktree false-block (root-caused 2026-08-10): `cd "$WT" && git
# commit` / `git -C "$WT" commit` drove the effective cwd to UNKNOWN and Rule 1
# printed the MISLEADING "Direct commits to main". The gate still fails closed on
# an unresolvable cwd — deliberately WITHOUT trying to resolve `$VAR` (four
# adversarial-review rounds proved static resolution unbounded: source/eval/read,
# export, functions, $PWD/CDPATH, printf -v, multi -C) — but now says what is
# actually wrong and teaches the literal-path form.


def test_variable_cd_blocks_with_teach_message_not_branch_lie(repo: Path, home: Path) -> None:
    _mark(repo, home)
    res = _run_hook(f'WT={repo}; cd "$WT" && git commit -m wip', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot verify which branch" in res.stderr
    assert "LITERAL absolute path" in res.stderr
    assert "Direct commits to main" not in res.stderr


def test_variable_dash_C_blocks_with_teach_message(repo: Path, home: Path) -> None:
    # `git -C "$WT" commit` — same unresolvable class as `cd "$WT"`. (Pre-fix this
    # joined a bogus literal `<cwd>/$WT` dir instead of classifying UNKNOWN.)
    _mark(repo, home)
    res = _run_hook(f'WT={repo}; git -C "$WT" commit -m wip', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot verify which branch" in res.stderr
    assert "Direct commits to main" not in res.stderr


def test_variable_never_resolved_even_from_literal_assignment(repo: Path, home: Path) -> None:
    # Pins the TEACH-ONLY decision: even `WT=<literal>; cd "$WT"` is NOT resolved
    # (bash can mutate WT between binding and use — source/printf -v/functions —
    # so any static resolution is a false-ALLOW-to-main surface). Must block with
    # the teach message, never silently allow.
    _mark(repo, home)
    res = _run_hook(f'WT={repo}\ncd "$WT" && git commit -m wip', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot verify which branch" in res.stderr


def test_literal_absolute_cd_still_allows(repo: Path, home: Path) -> None:
    _mark(repo, home)
    res = _run_hook(f"cd {repo} && git commit -m wip", repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_literal_dash_C_still_allows(repo: Path, home: Path) -> None:
    _mark(repo, home)
    res = _run_hook(f"git -C {repo} commit -m wip", repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_genuine_main_commit_still_says_direct_commits_to_main(repo: Path, home: Path) -> None:
    _git(repo, "checkout", "-q", "main")
    (repo / "f.py").write_text("base = 3\n")
    _git(repo, "add", "-A")
    res = _run_hook(f"cd {repo} && git commit -m wip", repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "Direct commits to main" in res.stderr


# ── Audit findings (2026-08-11, execution-confirmed at base): unexpanded
# tokens and chained commit segments must not slip past Rule 1 ────────────
#
# Positive validation closes the whole unexpanded-token class: a resolved cwd
# that is not an EXISTING directory is UNKNOWN (a glob/tilde/backslash token we
# didn't expand joins to a nonexistent literal path; the old branch read against
# it returned "unknown"/OSError and slipped PAST Rule 1 — and Rule 2's staged
# check read empty, so the commit went entirely ungated).


def _mainrepo(tmp_path: Path) -> Path:
    """A second repo left ON MAIN with a staged change (the smuggle target)."""
    r = tmp_path / "mainrepo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "m.py").write_text("m = 1\n")
    _git(r, "add", "-A")
    return r


def test_glob_dash_C_target_blocks(repo: Path, home: Path, tmp_path: Path) -> None:
    # `git -C <prefix>*` — bash would glob-expand to the main repo; the guard's
    # unexpanded join is a nonexistent dir → UNKNOWN → deny (was: ungated ALLOW).
    main_repo = _mainrepo(tmp_path)
    prefix = str(main_repo)[:-1]  # strip last char so `<prefix>*` globs to it
    _mark(repo, home)
    res = _run_hook(f"git -C {prefix}* commit -m smuggle", repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot verify which branch" in res.stderr


def test_backslash_cd_target_blocks(repo: Path, home: Path, tmp_path: Path) -> None:
    # `cd /path/mainrep\o` — bash strips the backslash and enters the main repo;
    # the guard's literal join (with backslash) is nonexistent → UNKNOWN → deny.
    main_repo = _mainrepo(tmp_path)
    escaped = str(main_repo)[:-1] + "\\" + str(main_repo)[-1]
    _mark(repo, home)
    res = _run_hook(f"cd {escaped} && git commit -m smuggle", repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot verify which branch" in res.stderr


def test_second_commit_segment_on_main_blocks(repo: Path, home: Path, tmp_path: Path) -> None:
    # `git commit … && git -C <mainrepo> commit …` — the SECOND segment lands on
    # main and must be checked too (was: only the first segment was resolved).
    main_repo = _mainrepo(tmp_path)
    _mark(repo, home)
    res = _run_hook(f"git commit -m ok && git -C {main_repo} commit -m smuggle", repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "Direct commits to main" in res.stderr


def test_nonexistent_literal_cd_blocks_with_teach_message(repo: Path, home: Path) -> None:
    # Positive validation: even a clean literal path that doesn't EXIST is
    # UNKNOWN (we cannot read a branch from a dir that isn't there).
    _mark(repo, home)
    res = _run_hook("cd /nonexistent/dir/xyz && git commit -m wip", repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot verify which branch" in res.stderr


def test_tilde_dash_C_blocks_even_with_shadow_dir(repo: Path, home: Path) -> None:
    # Codex round-2 "shadow path": a literal dir named `~` under cwd would pass the
    # isdir validation while bash expands `~` to $HOME — so a `~` (or glob) `-C`
    # target is categorically UNKNOWN, existence notwithstanding.
    shadow = repo / "~" / "mainrepo"
    shadow.mkdir(parents=True)
    _mark(repo, home)
    res = _run_hook("git -C ~/mainrepo commit -m smuggle", repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot verify which branch" in res.stderr


def test_backslash_cd_blocks_even_with_shadow_dir(repo: Path, home: Path, tmp_path: Path) -> None:
    # Same shadow-path class for cd: plant the literal `mainrep\o` dir — the
    # backslash metachar rule must deny WITHOUT consulting existence.
    main_repo = _mainrepo(tmp_path)
    escaped = str(main_repo)[:-1] + "\\" + str(main_repo)[-1]
    (tmp_path / "mainrep\\o").mkdir()  # the literal shadow dir (backslash in name)
    _mark(repo, home)
    res = _run_hook(f"cd {escaped} && git commit -m smuggle", repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot verify which branch" in res.stderr


def test_chained_commits_to_different_dirs_block(repo: Path, home: Path, tmp_path: Path) -> None:
    # Two commits into DIFFERENT (both non-main) dirs share one gate — the review
    # marker covers only the first worktree. Structural deny, mirroring the push
    # guard's multiple-publish rule.
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "-c", "init.defaultBranch=main", "init", "-q")
    _git(other, "config", "user.email", "t@e.st")
    _git(other, "config", "user.name", "tester")
    (other / "o.py").write_text("o = 1\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "base")
    _git(other, "checkout", "-q", "-b", "feature/y")
    (other / "o.py").write_text("o = 2\n")
    _git(other, "add", "-A")
    _mark(repo, home)
    res = _run_hook(f"git commit -m ok && git -C {other} commit -m ride", repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "DIFFERENT worktrees" in res.stderr


def test_brace_expansion_targets_block(repo: Path, home: Path, tmp_path: Path) -> None:
    # Codex round-3: `{x..x}` brace expansion transforms the word before cd/-C —
    # the metachar set is derived from the bash manual's expansion list, and `{`
    # completes it. Shadow dir planted to prove existence is not consulted.
    main_repo = _mainrepo(tmp_path)
    braced = str(main_repo).replace("mainrepo", "mainrep{o..o}")
    (tmp_path / "mainrep{o..o}").mkdir()  # literal shadow matching the unexpanded word
    _mark(repo, home)
    for cmd in (f"cd {braced} && git commit -m smuggle", f"git -C {braced} commit -m smuggle"):
        res = _run_hook(cmd, repo, home)
        assert res.returncode == 2, cmd + res.stdout + res.stderr
        assert "cannot verify which branch" in res.stderr, cmd


def test_process_substitution_cd_blocks_even_with_shadow_dir(repo: Path, home: Path) -> None:
    # Codex round-4: `cd <(x)` — bash treats `<(…)` as process substitution (cd
    # fails; a `;` chain then commits in the ORIGINAL cwd), while a planted
    # literal dir named `<(x)` would pass the isdir check. `()<>` metacharacters
    # deny outright, existence never consulted.
    (repo / "<(x)").mkdir()
    _mark(repo, home)
    res = _run_hook(f"cd {repo}/<(x); git commit -m smuggle", repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot verify which branch" in res.stderr


def test_relative_cd_with_cdpath_env_blocks(repo: Path, home: Path, tmp_path: Path) -> None:
    # With CDPATH in the environment, bash's `cd rel` may enter a dir OUTSIDE the
    # payload-cwd join — unverifiable → UNKNOWN.
    sub = repo / "sub"
    sub.mkdir()
    _mark(repo, home)
    body = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "cd sub && git commit -m wip"},
        "session_id": "test",
        "cwd": str(repo),
    }
    env = {**os.environ, "HOME": str(home), "CDPATH": str(tmp_path)}
    res = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(body),
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot verify which branch" in res.stderr


def test_double_quoted_backslash_cd_blocks(repo: Path, home: Path) -> None:
    # `cd "main\\repo"` — double quotes collapse the escape (bash sees main\repo);
    # a literal read is unfaithful → UNKNOWN. Single quotes remain fully literal.
    _mark(repo, home)
    res = _run_hook('cd "/tmp/main\\\\repo" && git commit -m x', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot verify which branch" in res.stderr


def test_chained_commits_same_dir_still_pass(repo: Path, home: Path) -> None:
    # A same-dir chain (commit then amend) is one worktree, one review — allowed.
    _mark(repo, home)
    res = _run_hook(f"cd {repo} && git commit -m wip && git commit --amend -m better", repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_override_with_trailing_text(repo: Path, home: Path) -> None:
    """A trailing comment may carry text after the sigil (it's commented out)."""
    res = _run_hook('git commit -m "wip"  # review-override: accepted P2s', repo, home)
    assert res.returncode == 0, res.stderr
    assert "review-override honored" in res.stderr


def test_override_on_add_commit_chain(repo: Path, home: Path) -> None:
    """The git-add-&&-commit path (marker-based Rule 2) also honors override."""
    _git(repo, "reset", "-q")  # unstage so nothing is staged yet
    (repo / "g.txt").write_text("new\n")
    res = _run_hook('git add -A && git commit -m "wip"  # review-override', repo, home)
    assert res.returncode == 0, res.stderr
    assert "review-override honored" in res.stderr


# ── Override never defeats Rule 0 (--no-verify) or Rule 1 (main) ─────────


def test_no_verify_long_not_overridable(repo: Path, home: Path) -> None:
    res = _run_hook('git commit --no-verify -m "wip"  # review-override', repo, home)
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_no_verify_short_bundled_not_overridable(repo: Path, home: Path) -> None:
    """The real hole: -nm is --no-verify + -m. Override must not slip it through."""
    res = _run_hook('git commit -nm "wip"  # review-override', repo, home)
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_no_verify_short_standalone(repo: Path, home: Path) -> None:
    res = _run_hook('git commit -n -m "wip"', repo, home)
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_no_verify_mentioned_in_message_not_blocked(repo: Path, home: Path) -> None:
    """A '--no-verify' inside the commit message must NOT trip Rule 0."""
    assert _mark(repo, home).returncode == 0
    res = _run_hook('git commit -m "document --no-verify behavior"', repo, home)
    assert res.returncode == 0, res.stderr


# ── shell_parse retrofit: the Codex-flagged parsing cases ────────────────


def test_no_verify_glued_to_operator_not_overridable(repo: Path, home: Path) -> None:
    """-n glued to a shell operator is still a real flag → Rule 0 blocks."""
    res = _run_hook("git commit -m wip -n&&echo done  # review-override", repo, home)
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_no_verify_in_bash_c_not_overridable(repo: Path, home: Path) -> None:
    """The commit inside bash -c is executed → its -n must be seen and blocked."""
    res = _run_hook("bash -c 'git commit -n -m wip'  # review-override", repo, home)
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_attached_message_not_treated_as_no_verify(repo: Path, home: Path) -> None:
    """git commit -minitial is `-m initial`, not -n: allowed once reviewed."""
    assert _mark(repo, home).returncode == 0
    res = _run_hook("git commit -minitial", repo, home)
    assert res.returncode == 0, res.stderr


def test_override_on_other_segment_does_not_authorize(repo: Path, home: Path) -> None:
    """An override token that isn't a trailing comment on the commit segment
    (here it's inside an echo) must not clear Rule 2 — the commit stays blocked."""
    res = _run_hook("echo '# review-override' && git commit -m wip", repo, home)
    assert res.returncode == 2
    assert "honored" not in res.stderr  # not authorized


def test_main_branch_not_overridable(repo: Path, home: Path) -> None:
    _git(repo, "checkout", "-q", "main")
    (repo / "f.py").write_text("main_change = 1\n")
    _git(repo, "add", "-A")
    res = _run_hook('git commit -m "wip"  # review-override', repo, home)
    assert res.returncode == 2
    assert "main" in res.stderr.lower()


# ── B1: the gate is satisfiable without gstack ──────────────────────────


def test_mark_succeeds_without_gstack(repo: Path, home: Path) -> None:
    assert not (home / ".gstack").exists()  # gstack genuinely absent
    res = _mark(repo, home)
    assert res.returncode == 0, res.stderr
    assert "Review marker written" in res.stdout
    # Marker is per-worktree under review_markers/<key>.json (exactly one here).
    markers = list((home / ".genesis" / "review_markers").glob("*.json"))
    assert len(markers) == 1
    marker = json.loads(markers[0].read_text())
    assert "authoritative" in marker["review_evidence"]  # advisory annotation


def test_mark_refuses_without_agent_output(repo: Path, home: Path) -> None:
    """Authoritative evidence (agent output) is still mandatory.

    Passes a valid outcome flag (--defects) so this exercises the AGENT-OUTPUT refusal
    specifically, not the separate required-outcome refusal.
    """
    env = {**os.environ, "HOME": str(home)}
    res = subprocess.run(
        [
            sys.executable,
            str(_REVIEW_STATE),
            "mark",
            "--agent-output",
            str(home / ".genesis" / "does_not_exist.txt"),
            "--defects",
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 1
    assert "REFUSED" in res.stderr
    assert "not found" in res.stderr.lower()  # the agent-output refusal, not the outcome one


def test_commit_allowed_after_marking(repo: Path, home: Path) -> None:
    """End-to-end: mark (no gstack) → the same staged commit is allowed."""
    assert _mark(repo, home).returncode == 0
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 0, res.stderr


# ── Per-worktree marker isolation (concurrent-session clobber fix) ────────


def _second_repo(tmp_path: Path, name: str) -> Path:
    """A distinct git worktree root on its own feature branch, staged."""
    r = tmp_path / name
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "g.py").write_text("base = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "feature/y")
    (r / "g.py").write_text("base = 2\n")
    _git(r, "add", "-A")
    return r


def test_reviewed_commit_allowed(repo: Path, home: Path) -> None:
    """Baseline: after marking, the commit passes the gate (no override)."""
    assert _mark(repo, home).returncode == 0
    res = _run_hook('git commit -m "reviewed"', repo, home)
    assert res.returncode == 0, res.stderr


def test_marker_not_clobbered_by_concurrent_worktree(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    """A concurrent session marking a DIFFERENT worktree (same shared HOME)
    must not reset this worktree's marker — the global-file clobber bug."""
    repo_b = _second_repo(tmp_path, "repo_b")
    assert _mark(repo, home).returncode == 0  # our worktree reviewed
    assert _mark(repo_b, home).returncode == 0  # concurrent session marks its own
    # Our commit must STILL be allowed — B's mark did not touch A's marker.
    res = _run_hook('git commit -m "reviewed"', repo, home)
    assert res.returncode == 0, res.stderr


def test_distinct_worktrees_get_distinct_markers(repo: Path, home: Path, tmp_path: Path) -> None:
    repo_b = _second_repo(tmp_path, "repo_b2")
    assert _mark(repo, home).returncode == 0
    assert _mark(repo_b, home).returncode == 0
    markers = list((home / ".genesis" / "review_markers").glob("*.json"))
    assert len(markers) == 2, [m.name for m in markers]


def _run_invalidate(command: str, repo: Path, home: Path) -> subprocess.CompletedProcess:
    """Run the PostToolUse invalidation hook for a successful commit."""
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": {"stdout": "[feature/x abc1234] msg", "stderr": ""},
            "session_id": "test",
        }
    )
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(_INVALIDATE)],
        input=payload,
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _markers(home: Path) -> list[Path]:
    return list((home / ".genesis" / "review_markers").glob("*.json"))


def test_commit_invalidates_per_worktree_marker(repo: Path, home: Path) -> None:
    """After a commit, the per-worktree marker is cleared so the NEXT commit
    needs a fresh review. Regression for the BLOCKER: the invalidation hook must
    delete the per-worktree marker, not the retired global file (else one review
    silently authorizes every subsequent git-add+commit for the marker's TTL)."""
    assert _mark(repo, home).returncode == 0
    assert len(_markers(home)) == 1  # marker written

    # Simulate CC firing the PostToolUse invalidation hook after the commit.
    _run_invalidate("git commit -m done", repo, home)
    assert _markers(home) == [], "per-worktree marker was not cleared after commit"

    # The next commit must be blocked again (no valid marker).
    res = _run_hook('git commit -m "again"', repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_invalidate_ignores_non_commit(repo: Path, home: Path) -> None:
    """A non-commit command must NOT clear the marker."""
    assert _mark(repo, home).returncode == 0
    _run_invalidate("echo not a commit", repo, home)
    assert len(_markers(home)) == 1  # marker survives


# ── Invalidator/checker cwd symmetry (the #1254 follow-up, ab42b04f) ──────
# #1254 made the PreToolUse checker resolve the commit's real worktree via
# `git -C` / the last `cd` / the payload cwd. The PostToolUse invalidator was
# left on the leading-`cd` regex (→ process cwd), so for a `git -C W commit` or
# a decoy multi-`cd` the CHECKED marker (W) and the CLEARED marker (the shell's
# worktree) diverged: W's marker survived its TTL and a later unreviewed
# `git add && git commit` chain sailed through the existence-only gate. These
# tests pin the invalidator to the same worktree the checker validates.
#
# Live-faithful setup: a 2026-07-30 probe confirmed CC spawns the hook with
# process_cwd == payload `cwd`, and the PostToolUse `cwd` is POST-execution (it
# already reflects the command's `cd`s). So `run_from` and `payload_cwd` are set
# EQUAL here (matching reality), and divergence is produced the real way — via
# `git -C` or a decoy `cd` in the command — not by forcing process≠payload.


def _run_invalidate_at(
    command: str, *, run_from: Path, payload_cwd: Path, home: Path
) -> subprocess.CompletedProcess:
    """PostToolUse invalidator with the process cwd and payload `cwd` set apart.

    Real CC keeps them equal; callers pass them equal for live-faithful cases and
    unequal only for the defensive mechanism test that pins which field is read.
    """
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": {"stdout": "[feature/x abc1234] msg", "stderr": ""},
            "session_id": "test",
            "cwd": str(payload_cwd),
        }
    )
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(_INVALIDATE)],
        input=payload,
        cwd=str(run_from),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_git_dash_C_commit_clears_target_worktree_not_shell(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    """`git -C W commit` from a shell sitting in a DIFFERENT worktree: the
    checker validates W's marker via `-C`, so the invalidator MUST clear W's
    marker. The old invalidator (leading-`cd` regex → None → process cwd) cleared
    the shell's worktree and left W's marker alive — the stale-review bypass.
    Live-faithful: process cwd == payload cwd == the shell worktree (repo_b)."""
    repo_b = _second_repo(tmp_path, "repo_b_gitC")  # the shell's worktree
    assert _mark(repo, home).returncode == 0  # review recorded for repo (= W)
    assert len(_markers(home)) == 1
    _run_invalidate_at(
        f"git -C {repo} commit -m done", run_from=repo_b, payload_cwd=repo_b, home=home
    )
    assert _markers(home) == [], "`git -C W commit` did not clear W's marker"


def test_git_dash_C_no_stale_bypass_e2e(repo: Path, home: Path, tmp_path: Path) -> None:
    """End-to-end (both hooks): a reviewed `git -C W commit` is ALLOWED by the
    checker (resolves W via `-C`), the invalidator clears W's marker, and a NEW
    unreviewed `git -C W add && git -C W commit` chain must then BLOCK. Guards the
    exact bypass the invalidator asymmetry opened."""
    repo_b = _second_repo(tmp_path, "repo_b_e2e_gitC")  # shell worktree
    assert _mark(repo, home).returncode == 0
    # Checker allows the reviewed commit (resolves repo via -C).
    assert (
        _run_hook(
            f"git -C {repo} commit -m reviewed", repo_b, home, payload_cwd=str(repo_b)
        ).returncode
        == 0
    )
    # Invalidator clears repo's marker (same -C target).
    _run_invalidate_at(
        f"git -C {repo} commit -m reviewed", run_from=repo_b, payload_cwd=repo_b, home=home
    )
    assert _markers(home) == []
    # A new UNREVIEWED add+commit chain targeting repo must now BLOCK.
    res = _run_hook(
        f"git -C {repo} add -A && git -C {repo} commit -m unreviewed",
        repo_b,
        home,
        payload_cwd=str(repo_b),
    )
    assert res.returncode == 2, res.stderr


def test_decoy_multi_cd_clears_last_cd_worktree(repo: Path, home: Path, tmp_path: Path) -> None:
    """A decoy `cd A && … ; cd W && git commit` runs in W (bash applies cds in
    order; the shell ends in W, so the POST-execution payload cwd is W). The
    invalidator must clear W's marker — not A's (which the old leading-`cd` regex
    picked). Live-faithful: payload cwd == the post-execution dir (repo = W)."""
    repo_b = _second_repo(tmp_path, "repo_b_decoy")  # the decoy A
    assert _mark(repo, home).returncode == 0  # review for repo (= W)
    _run_invalidate_at(
        f"cd {repo_b} && true ; cd {repo} && git commit -m done",
        run_from=repo,
        payload_cwd=repo,
        home=home,
    )
    assert _markers(home) == [], "decoy multi-cd cleared the wrong (first-cd) worktree"


def test_cd_after_commit_over_clears_real_worktree(repo: Path, home: Path, tmp_path: Path) -> None:
    """`cd W && git commit && cd Z`: the commit ran in W but the POST-execution
    payload cwd is Z. The invalidator detects the `cd` AFTER the commit segment,
    treats the cwd as ambiguous, and over-clears the candidate set — so W's
    marker (recovered from the leading `cd W`) is still cleared."""
    repo_z = _second_repo(tmp_path, "repo_z_after")  # Z, where the shell ends
    assert _mark(repo, home).returncode == 0  # review for repo (= W)
    _run_invalidate_at(
        f"cd {repo} && git commit -m done && cd {repo_z}",
        run_from=repo_z,
        payload_cwd=repo_z,
        home=home,
    )
    assert _markers(home) == [], "cd-after-commit did not over-clear W's marker"


def test_decoy_plus_trailing_cd_over_clears_real_worktree(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    """`cd A && … ; cd W && git commit && cd Z`: the trailing `cd Z` makes the
    post cwd (Z) miss the real commit dir W, AND the leading-`cd` regex picks A
    (also not W). The over-clear set must still reach W via the checker's own
    walk-based resolution — else W's marker survives and a later unreviewed
    add+commit chain there reuses the stale review (a bypass Codex flagged)."""
    repo_a = _second_repo(tmp_path, "repo_a_decoytrail")  # decoy A (leading cd)
    repo_z = _second_repo(tmp_path, "repo_z_decoytrail")  # Z (post cwd)
    assert _mark(repo, home).returncode == 0  # review for repo (= W, the real dir)
    _run_invalidate_at(
        f"cd {repo_a} && true ; cd {repo} && git commit -m done && cd {repo_z}",
        run_from=repo_z,
        payload_cwd=repo_z,
        home=home,
    )
    assert _markers(home) == [], "decoy+trailing-cd left W's marker uncleared (bypass)"


def test_leading_space_cd_plus_trailing_cd_over_clears(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    """A LEADING SPACE before `cd W` defeats `_extract_working_dir`'s `^cd` anchor,
    and the trailing `cd Z` moves the post cwd off W — so only the checker's own
    walk (which strips segments) still lands on W. Regression for the reviewer's
    Blocker 3 leading-whitespace variant."""
    repo_z = _second_repo(tmp_path, "repo_z_lead")
    assert _mark(repo, home).returncode == 0  # review for repo (= W)
    _run_invalidate_at(
        f" cd {repo} && git commit -m done && cd {repo_z}",  # note leading space
        run_from=repo_z,
        payload_cwd=repo_z,
        home=home,
    )
    assert _markers(home) == [], "leading-space cd + trailing cd left W uncleared"


def test_compound_decoy_no_stale_bypass_e2e(repo: Path, home: Path, tmp_path: Path) -> None:
    """Full Blocker-3 E2E: a reviewed compound-decoy commit is ALLOWED by the
    checker against W, the invalidator clears W (via the walk candidate), and a
    later UNREVIEWED add+commit chain in W must then BLOCK — proving the stale
    marker no longer survives to authorize it."""
    repo_a = _second_repo(tmp_path, "repo_a_e2e")
    repo_z = _second_repo(tmp_path, "repo_z_e2e")
    assert _mark(repo, home).returncode == 0
    reviewed = f"cd {repo_a} && true ; cd {repo} && git commit -m ok && cd {repo_z}"
    # Checker (Pre) resolves W and allows the reviewed commit.
    assert _run_hook(reviewed, repo_a, home, payload_cwd=str(repo_a)).returncode == 0
    # Invalidator (Post, shell ended in Z) must still clear W.
    _run_invalidate_at(reviewed, run_from=repo_z, payload_cwd=repo_z, home=home)
    assert _markers(home) == []
    # A new unreviewed add+commit chain in W must BLOCK (no stale marker).
    res = _run_hook(
        f"cd {repo} && git add -A && git commit -m sneaky",
        repo_z,
        home,
        payload_cwd=str(repo_z),
    )
    assert res.returncode == 2, res.stderr


def test_invalidator_reads_payload_cwd_field_not_process_cwd(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    """DEFENSIVE (not live-faithful): with process cwd and payload `cwd` forced
    apart, a bare `git commit` clears the PAYLOAD-cwd worktree. Pins the resolver
    to the documented payload field so a future refactor to process cwd (or a CC
    release that breaks the process==payload equality) is caught."""
    repo_b = _second_repo(tmp_path, "repo_b_field")  # process cwd (wrong worktree)
    assert _mark(repo, home).returncode == 0  # review for repo (payload cwd)
    _run_invalidate_at("git commit -m done", run_from=repo_b, payload_cwd=repo, home=home)
    assert _markers(home) == [], "invalidator keyed on process cwd, not payload cwd"


# ── Docs/config-only skip (Change 4) ─────────────────────────────────────
# A commit whose ENTIRE staged set is docs/config carries no code to review
# (adaptive-review "review level: None") → Rule 2 is skipped. Conservative
# fail-toward-review default: any code/unknown extension, an empty staged set, or
# an unreadable diff falls through to normal enforcement (never skipped). Rule 0
# (--no-verify) and Rule 1 (main) are checked BEFORE the skip and stay hard.


def _restage(repo: Path, paths_contents: dict[str, str]) -> None:
    """Clean the tree, then write the given files and stage EXACTLY them.

    A hard reset drops the fixture's staged (code) change so the staged set is
    only the files under test — a soft reset would leave that change in the
    working tree, and `git add -A` would re-stage it, polluting the set.
    """
    _git(repo, "reset", "--hard", "HEAD", "-q")
    for rel, content in paths_contents.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")


def test_docs_only_commit_skips_enforcement(repo: Path, home: Path) -> None:
    """README.md + config/x.yaml with NO review marker → skipped (exit 0)."""
    _restage(repo, {"README.md": "# docs\n", "config/x.yaml": "a: 1\n"})
    res = _run_hook('git commit -m "docs"', repo, home)
    assert res.returncode == 0, res.stderr


def test_single_readme_skips(repo: Path, home: Path) -> None:
    _restage(repo, {"README.md": "# docs\n"})
    res = _run_hook('git commit -m "docs"', repo, home)
    assert res.returncode == 0, res.stderr


def test_extensionless_docs_basenames_skip(repo: Path, home: Path) -> None:
    """CHANGELOG / LICENSE / .gitignore basenames are on the allowlist."""
    _restage(repo, {"CHANGELOG": "v1\n", "LICENSE": "MIT\n", ".gitignore": "*.pyc\n"})
    res = _run_hook('git commit -m "chores"', repo, home)
    assert res.returncode == 0, res.stderr


def test_code_file_still_enforced(repo: Path, home: Path) -> None:
    """A .py in the staged set → NOT docs-only → Rule 2 still blocks."""
    _restage(repo, {"foo.py": "print(1)\n"})
    res = _run_hook('git commit -m "code"', repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_prompt_surface_only_commit_not_skipped(repo: Path, home: Path) -> None:
    """A prompt/agent surface (.claude/agents/*.md) is NOT docs-config, so a prompt-only
    commit does NOT take the pure-docs skip — it is substantial (behavior risk) and hits
    the depth gate."""
    _restage(repo, {".claude/agents/reviewer.md": "You are a reviewer.\n"})
    res = _run_hook('git commit -m "prompt"', repo, home)
    assert res.returncode == 2
    assert "review depth" in res.stderr.lower()


def test_user_caps_behavior_doc_only_commit_skips(repo: Path, home: Path) -> None:
    """User-sovereign CAPS docs (SOUL.md/USER.md) stay docs-config → a commit touching
    only them still takes the docs skip (the user editing their own behavior files is not
    gated)."""
    _restage(repo, {"SOUL.md": "# Who you are\nBe helpful.\n", "USER.md": "# User\nBrief.\n"})
    res = _run_hook('git commit -m "soul"', repo, home)
    assert res.returncode == 0, res.stderr


def test_commit_dash_am_of_unstaged_edit_is_gated(repo: Path, home: Path) -> None:
    """`git commit -am` stages tracked edits AT COMMIT TIME, so the hook-time index is
    empty. It must be treated as content-adding (require a valid marker) — not slip through
    as 'no staged changes'. Regression for the -a/-am (and pathspec/-i/-o/-p) bypass class
    that the old bare-`git add` check missed (Codex round-2 P2-A)."""
    _git(repo, "reset", "-q")  # unstage the fixture change → clean index, unstaged f.py edit
    res = _run_hook('git commit -am "x"', repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_mixed_docs_and_code_enforced(repo: Path, home: Path) -> None:
    """One code file among docs → the whole set is NOT docs-only → enforced."""
    _restage(repo, {"README.md": "# docs\n", "foo.py": "print(1)\n"})
    res = _run_hook('git commit -m "mixed"', repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_unknown_extension_enforced(repo: Path, home: Path) -> None:
    """An unrecognized extension is treated as code (fail toward review)."""
    _restage(repo, {"data.json": "{}\n"})
    res = _run_hook('git commit -m "json"', repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_empty_staged_set_falls_back_to_enforcement(repo: Path, home: Path) -> None:
    """A git-add-&&-commit chain stages nothing at hook time → NOT skipped →
    marker-based Rule 2 blocks when unreviewed (fallback, not a docs skip)."""
    _git(repo, "reset", "-q")  # nothing staged now
    (repo / "g.txt").write_text("new\n")  # a docs-ext file, but not yet staged
    res = _run_hook('git add -A && git commit -m "wip"', repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_docs_only_no_verify_still_blocked(repo: Path, home: Path) -> None:
    """Rule 0 precedes the docs skip — --no-verify on a docs-only commit blocks."""
    _restage(repo, {"README.md": "# docs\n"})
    res = _run_hook('git commit --no-verify -m "docs"', repo, home)
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_docs_only_on_main_still_blocked(repo: Path, home: Path) -> None:
    """Rule 1 precedes the docs skip — a docs-only commit on main still blocks."""
    _git(repo, "checkout", "-q", "main")
    _restage(repo, {"README.md": "# docs\n"})
    res = _run_hook('git commit -m "docs"', repo, home)
    assert res.returncode == 2
    assert "main" in res.stderr.lower()


# ── SHOULD-FIX 4: .github/workflows/*.yml is executable CI config ─────────


def test_github_workflow_yaml_still_enforced(repo: Path, home: Path) -> None:
    """A workflow-only commit is NOT docs/config (arbitrary `run:` with repo
    secrets) → Rule 2 still fires even though the file is .yml."""
    _restage(repo, {".github/workflows/ci.yml": "on: push\njobs: {}\n"})
    res = _run_hook('git commit -m "ci"', repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_workflow_plus_docs_still_enforced(repo: Path, home: Path) -> None:
    """One workflow file among docs → the set is not all-docs → enforced."""
    _restage(repo, {"README.md": "# d\n", ".github/workflows/ci.yml": "on: push\n"})
    res = _run_hook('git commit -m "mix"', repo, home)
    assert res.returncode == 2


def test_nonworkflow_yaml_still_skips(repo: Path, home: Path) -> None:
    """A legit config .yaml (not under .github/) still skips review."""
    _restage(repo, {"config/recon_watchlist.yaml": "watch: []\n"})
    res = _run_hook('git commit -m "cfg"', repo, home)
    assert res.returncode == 0, res.stderr


# ── BLOCKER 2: decoy-cd cannot point the docs skip at the wrong repo ───────


def _docs_only_repo(tmp_path: Path, name: str) -> Path:
    """A throwaway repo on a feature branch whose ENTIRE staged set is docs."""
    r = tmp_path / name
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "seed.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "feature/docs")
    (r / "README.md").write_text("# only docs staged\n")
    _git(r, "add", "-A")
    return r


def test_decoy_cd_docs_skip_uses_real_wt(repo: Path, home: Path, tmp_path: Path) -> None:
    """`cd <docs-repo> && true; cd <real-wt> && git commit` must inspect the REAL
    wt (code staged, no marker) → enforced. The old first-cd resolver would have
    read the decoy docs repo's staged set and skipped review for real code."""
    decoy = _docs_only_repo(tmp_path, "decoy_docs")
    cmd = f"cd {decoy} && true; cd {repo} && git commit -m x"
    res = _run_hook(cmd, repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_ambiguous_cd_before_commit_enforced(repo: Path, home: Path) -> None:
    """A cd into a variable before the commit ⇒ UNKNOWN cwd ⇒ fail closed (Rule 1
    blocks; the docs skip is never reached)."""
    res = _run_hook("cd $WT && git commit -m x", repo, home)
    assert res.returncode == 2


# ── P1-D: staged index at hook time ≠ what the command commits ────────────


def test_commit_dash_a_with_docs_staged_enforced(repo: Path, home: Path) -> None:
    """`git commit -a` stages tracked code the --cached snapshot doesn't show yet
    → NOT a pure commit → the docs-only skip must NOT fire."""
    _restage(repo, {"README.md": "# docs\n"})
    (repo / "f.py").write_text("changed = 3\n")  # tracked, unstaged code change
    res = _run_hook('git commit -a -m "x"', repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_commit_pathspec_with_docs_staged_enforced(repo: Path, home: Path) -> None:
    """`git commit -m x app.py` selects a pathspec → not pure → not skipped."""
    _restage(repo, {"README.md": "# docs\n"})
    res = _run_hook('git commit -m "x" app.py', repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_add_then_commit_chain_with_docs_staged_enforced(repo: Path, home: Path) -> None:
    """`git add app.py && git commit` stages code in a prior segment → not pure."""
    _restage(repo, {"README.md": "# docs\n"})
    (repo / "app.py").write_text("y = 2\n")
    res = _run_hook('git add app.py && git commit -m "x"', repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_bare_docs_commit_still_skips_after_p1d(repo: Path, home: Path) -> None:
    """Control: a genuinely pure bare docs commit still skips."""
    _restage(repo, {"README.md": "# docs\n"})
    res = _run_hook('git commit -m "docs"', repo, home)
    assert res.returncode == 0, res.stderr


# ── P2-E: renames surface both source and destination ────────────────────


def test_rename_from_code_to_docs_enforced(repo: Path, home: Path) -> None:
    """A staged rename `foo.py → README.md` shows only README under --name-only,
    but the SOURCE is code → must enforce (uses --name-status -M, both sides)."""
    _git(repo, "reset", "--hard", "HEAD", "-q")
    (repo / "foo.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add foo")
    _git(repo, "mv", "foo.py", "README.md")  # staged rename foo.py → README.md
    res = _run_hook('git commit -m "rename"', repo, home)
    assert res.returncode == 2
    assert "without review" in res.stderr


def test_rename_docs_to_docs_still_skips(repo: Path, home: Path) -> None:
    """A docs→docs rename (both sides docs) still skips."""
    _git(repo, "reset", "--hard", "HEAD", "-q")
    (repo / "NOTES.md").write_text("# notes\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add notes")
    _git(repo, "mv", "NOTES.md", "README.md")
    res = _run_hook('git commit -m "rename docs"', repo, home)
    assert res.returncode == 0, res.stderr


# ── P1-A: relative cd resolved against the payload cwd (commit) ────────────


def test_relative_cd_commit_to_sibling_main_blocked(repo: Path, home: Path, tmp_path: Path) -> None:
    """`cd ../main && git commit` from a feature-wt payload cwd resolves to the
    sibling main tree → Rule 1 blocks (relative path resolved against the cwd)."""
    trees = tmp_path / "trees"
    feature = trees / "feature"
    main = trees / "main"
    for d, br in ((feature, "feature/x"), (main, "main")):
        d.mkdir(parents=True)
        _git(d, "-c", "init.defaultBranch=main", "init", "-q")
        _git(d, "config", "user.email", "t@e.st")
        _git(d, "config", "user.name", "tester")
        (d / "seed.py").write_text("x = 1\n")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "base")
        if br != "main":
            _git(d, "checkout", "-q", "-b", br)
    res = _run_hook("cd ../main && git commit -m x", feature, home, payload_cwd=str(feature))
    assert res.returncode == 2
    assert "main" in res.stderr.lower()


# ── `git -C` / `git -c` commit forms reach the gate (d21db5ea, honest-use half) ──
# The cheap early-out used to key on a rigid `\bgit\s+commit\b`, which does NOT
# match `git -C <dir> commit` or `git -c k=v commit` (global options sit between
# "git" and "commit"), so BOTH hooks exited early and those commit forms skipped
# the ENTIRE gate — review (Rule 2), --no-verify (Rule 0), and direct-to-main
# (Rule 1). Broadened to the "commit" token; `analyze()` remains the precise
# filter. These pin every rule for the `git -C`/`git -c` forms.


def _main_repo(tmp_path: Path, name: str) -> Path:
    """A git repo left ON `main` with a staged change (for Rule 1 via `-C`)."""
    r = tmp_path / name
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "h.py").write_text("base = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    (r / "h.py").write_text("base = 2\n")
    _git(r, "add", "-A")
    return r


def test_git_dash_C_commit_blocked_without_review(repo: Path, home: Path) -> None:
    """`git -C W commit` on unreviewed code is BLOCKED (Rule 2) — previously it
    slipped past the early-out and was allowed unconditionally."""
    res = _run_hook(f"git -C {repo} commit -m x", repo, home, payload_cwd=str(repo))
    assert res.returncode == 2, res.stderr
    assert "without review" in res.stderr


def test_git_dash_c_config_commit_blocked_without_review(repo: Path, home: Path) -> None:
    """`git -c k=v commit` on unreviewed code is BLOCKED (Rule 2)."""
    res = _run_hook("git -c commit.gpgsign=false commit -m x", repo, home, payload_cwd=str(repo))
    assert res.returncode == 2, res.stderr
    assert "without review" in res.stderr


def test_git_dash_C_commit_allowed_after_marking(repo: Path, home: Path) -> None:
    """A reviewed `git -C W commit` passes the gate."""
    assert _mark(repo, home).returncode == 0
    res = _run_hook(f"git -C {repo} commit -m ok", repo, home, payload_cwd=str(repo))
    assert res.returncode == 0, res.stderr


def test_git_dash_C_override_allows(repo: Path, home: Path) -> None:
    """A trailing `# review-override` bypasses Rule 2 on a `git -C` commit."""
    res = _run_hook(
        f"git -C {repo} commit -m x  # review-override", repo, home, payload_cwd=str(repo)
    )
    assert res.returncode == 0, res.stderr


def test_git_dash_C_no_verify_still_blocked(repo: Path, home: Path) -> None:
    """`git -C W commit --no-verify` is BLOCKED by Rule 0 (which previously the
    early-out let slip). Reviewed or not, --no-verify is never allowed."""
    assert _mark(repo, home).returncode == 0  # even reviewed, --no-verify is denied
    res = _run_hook(f"git -C {repo} commit -m x --no-verify", repo, home, payload_cwd=str(repo))
    assert res.returncode == 2, res.stderr
    assert "no-verify" in res.stderr.lower()


def test_git_dash_C_to_main_blocked(repo: Path, home: Path, tmp_path: Path) -> None:
    """`git -C <main-tree> commit` is BLOCKED by Rule 1 even when the shell sits
    in a feature worktree — the `-C` target's branch decides."""
    main = _main_repo(tmp_path, "maintree")
    assert _mark(main, home).returncode == 0  # reviewed, to isolate Rule 1 from Rule 2
    res = _run_hook(f"git -C {main} commit -m x", repo, home, payload_cwd=str(repo))
    assert res.returncode == 2, res.stderr
    assert "main" in res.stderr.lower()


def test_commit_dash_C_reuse_message_is_not_a_worktree(repo: Path, home: Path) -> None:
    """`git commit -C HEAD` reuses HEAD's message — the `-C` is the commit's own
    flag, NOT a `git -C <dir>` redirect. It must resolve the SHELL's worktree
    (here `repo`), so an unreviewed one still BLOCKS (Rule 2) and a reviewed one
    passes — not resolve a bogus `<cwd>/HEAD` dir. Regression for the Codex P1."""
    res = _run_hook("git commit -C HEAD", repo, home, payload_cwd=str(repo))
    assert res.returncode == 2, res.stderr  # unreviewed → still gated in `repo`
    assert "without review" in res.stderr
    assert _mark(repo, home).returncode == 0
    res_ok = _run_hook("git commit -C HEAD", repo, home, payload_cwd=str(repo))
    assert res_ok.returncode == 0, res_ok.stderr  # reviewed `repo` → allowed


def test_invalidate_commit_dash_C_reuse_clears_shell_worktree(repo: Path, home: Path) -> None:
    """The invalidator must clear the SHELL's worktree marker for
    `git commit -C HEAD`, not a bogus `<cwd>/HEAD` (which would leave the real
    marker stale). Pairs with the checker regression above."""
    assert _mark(repo, home).returncode == 0
    _run_invalidate_at("git commit -C HEAD", run_from=repo, payload_cwd=repo, home=home)
    assert _markers(home) == [], "`git commit -C HEAD` did not clear the real marker"


def test_commit_pattern_matches_git_dash_c_and_dash_C() -> None:
    """Drift guard: BOTH hooks' `_COMMIT_PATTERN` must detect `git -C`/`git -c`
    commit forms (and not regress to the rigid `git commit` adjacency), and the
    two patterns must stay identical so the pair detects the same commit set."""
    import importlib.util

    def _pattern(mod_name: str):
        spec = importlib.util.spec_from_file_location(
            mod_name, str(_REPO_ROOT / "scripts" / f"{mod_name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        # scripts/ and scripts/hooks/ on path for the modules' own imports
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        sys.path.insert(0, str(_REPO_ROOT / "scripts" / "hooks"))
        spec.loader.exec_module(mod)
        return mod._COMMIT_PATTERN

    checker_pat = _pattern("review_enforcement_commit")
    invalidate_pat = _pattern("review_invalidate_on_commit")
    forms = [
        "git commit -m x",
        "git -C /some/wt commit -m x",
        "git -c commit.gpgsign=false commit -m x",
        "cd /wt && git commit -m x",
    ]
    for f in forms:
        assert checker_pat.search(f), f"checker pattern missed: {f}"
        assert invalidate_pat.search(f), f"invalidator pattern missed: {f}"
    assert checker_pat.pattern == invalidate_pat.pattern, "hook patterns drifted apart"


# ── Codex PR-#1371 findings (P1 + three P2s) ─────────────────────────────────


def test_identical_raw_chained_commit_on_main_blocks(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    # Codex P1: two commit segments with IDENTICAL raw text — a raw-equality
    # break resolved BOTH to the first segment's cwd, so the second commit (on
    # main) inherited the feature worktree's cwd and bypassed Rule 1 AND the
    # different-dirs check. Occurrence-index matching must catch it.
    main_repo = _mainrepo(tmp_path)
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git commit -m same; cd {main_repo} && git commit -m same",
        repo,
        home,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "Direct commits to main" in res.stderr


def test_identical_raw_chained_different_dirs_block(repo: Path, home: Path, tmp_path: Path) -> None:
    # Same P1 class, both dirs non-main: identical raws must not blind the
    # structural different-dirs deny either.
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "-c", "init.defaultBranch=main", "init", "-q")
    _git(other, "config", "user.email", "t@e.st")
    _git(other, "config", "user.name", "tester")
    (other / "f.py").write_text("base = 1\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "base")
    _git(other, "checkout", "-q", "-b", "feature/y")
    (other / "f.py").write_text("base = 2\n")
    _git(other, "add", "-A")
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git commit -m same; cd {other} && git commit -m same",
        repo,
        home,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "DIFFERENT worktrees" in res.stderr


def test_cdpath_dot_prefixed_relative_cd_allows(repo: Path, home: Path, tmp_path: Path) -> None:
    # Codex P2: POSIX cd (verified vs bash 5.2) skips the CDPATH search when the
    # first pathname component is dot or dot-dot — `cd ./sub` is deterministic
    # even under CDPATH and must not be denied.
    sub = repo / "sub"
    sub.mkdir()
    _mark(repo, home)
    body = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "cd ./sub && git commit -m wip"},
        "session_id": "test",
        "cwd": str(repo),
    }
    env = {**os.environ, "HOME": str(home), "CDPATH": str(tmp_path)}
    res = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(body),
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_double_quoted_backslash_before_ordinary_char_allows(home: Path, tmp_path: Path) -> None:
    # Codex P2: bash consumes a backslash in double quotes ONLY before $ ` " \
    # or newline (verified vs bash 5.2) — a quoted literal pathname containing a
    # backslash before an ordinary char must not be denied.
    weird = tmp_path / "repo\\q"  # a real dir whose name contains a backslash
    weird.mkdir()
    _git(weird, "-c", "init.defaultBranch=main", "init", "-q")
    _git(weird, "config", "user.email", "t@e.st")
    _git(weird, "config", "user.name", "tester")
    (weird / "f.py").write_text("base = 1\n")
    _git(weird, "add", "-A")
    _git(weird, "commit", "-qm", "base")
    _git(weird, "checkout", "-q", "-b", "feature/bs")
    (weird / "f.py").write_text("base = 2\n")
    _git(weird, "add", "-A")
    _mark(weird, home)
    res = _run_hook(f'cd "{weird}" && git commit -m wip', weird, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_diff_hash_stable_across_terminal_widths(repo: Path, tmp_path: Path) -> None:
    # get_current_diff_hash hashes `git diff --cached --stat`, whose path
    # truncation follows COLUMNS (honored even without a tty). The mark is
    # written from one process and checked from the hook process — with
    # different terminal widths, a LONG staged path hashed differently and the
    # gate intermittently denied a freshly-marked commit (measured 2026-08-11).
    # The subprocess env pins COLUMNS, so the hash must be width-independent.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "review_state_widths", _REPO_ROOT / "scripts" / "review_state.py"
    )
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)

    long_name = "a_deliberately_long_test_module_name_that_git_stat_truncates_at_80_cols.py"
    (repo / long_name).write_text("x = 1\n")
    _git(repo, "add", long_name)

    hashes = set()
    for cols in (None, "80", "120", "200"):
        env_patch = dict(os.environ)
        if cols is None:
            env_patch.pop("COLUMNS", None)
        else:
            env_patch["COLUMNS"] = cols
        old = os.environ.copy()
        os.environ.clear()
        os.environ.update(env_patch)
        try:
            hashes.add(rs.get_current_diff_hash(cwd=str(repo)))
        finally:
            os.environ.clear()
            os.environ.update(old)
    assert len(hashes) == 1, f"diff hash varies with terminal width: {hashes}"


def test_symlink_alias_same_dir_chain_passes(repo: Path, home: Path, tmp_path: Path) -> None:
    # Codex P2: the same worktree addressed via its real path and a symlink
    # alias is ONE dir (one marker, one index) — filesystem identity, not
    # string equality, decides the different-dirs deny.
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git commit -m a && git -C {alias} commit --amend -m b",
        repo,
        home,
    )
    assert res.returncode == 0, res.stdout + res.stderr


# ── Codex PR-#1371 re-review (2 P1s + P2): branch/content-binding + worktree id ──


def test_switch_to_main_before_commit_blocks(repo: Path, home: Path) -> None:
    # P1: a `git switch main` before a commit lands it on main, but the hook reads
    # the branch once (pre-switch). A switch-to-main before a commit → block.
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git commit -m ok && git switch main && git commit --allow-empty -m x",
        repo,
        home,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "switches branches" in res.stderr


def test_single_switch_to_main_then_commit_blocks(repo: Path, home: Path) -> None:
    _mark(repo, home)
    res = _run_hook(f"cd {repo} && git switch main && git commit -m x", repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "switches branches" in res.stderr


def test_switch_to_variable_branch_before_commit_blocks(repo: Path, home: Path) -> None:
    _mark(repo, home)
    res = _run_hook(f'cd {repo} && git switch "$BR" && git commit -m x', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "switches branches" in res.stderr


def test_attached_short_branch_create_to_main_blocks(repo: Path, home: Path) -> None:
    # Codex P1 (round 3): the branch-create value ATTACHED to the short flag
    # (`git checkout -Bmain` / `git switch -Cmain`) — a bare-token check skipped it,
    # letting HEAD reach main. Verified vs real git: `checkout -Bmain` lands on main.
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git checkout -Bmain && git commit --allow-empty -m bypass",
        repo,
        home,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "switches branches" in res.stderr


def test_clustered_short_branch_create_to_main_blocks(repo: Path, home: Path) -> None:
    # A boolean short flag clustered before the create flag: `-qB main` → -q + -B main.
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git checkout -qB main && git commit --allow-empty -m x", repo, home
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "switches branches" in res.stderr


def test_long_force_create_to_main_blocks(repo: Path, home: Path) -> None:
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git switch --force-create=main && git commit --allow-empty -m x",
        repo,
        home,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "switches branches" in res.stderr


def test_attached_short_branch_create_to_feature_allowed(repo: Path, home: Path) -> None:
    # The attached form to a NON-main branch (`git switch -cfeature`) must stay allowed.
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git switch -cfeature/x && git commit --amend --no-edit", repo, home
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_create_branch_then_commit_still_allowed(repo: Path, home: Path) -> None:
    # The flow the gate ITSELF recommends — create a NON-main branch and commit —
    # must NOT be blocked by the branch-mutation guard.
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git checkout -b feature/new && git commit --amend --no-edit",
        repo,
        home,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_checkout_file_restore_then_commit_allowed(repo: Path, home: Path) -> None:
    # `git checkout HEAD -- <file>` restores a file; HEAD is not moved, so it must
    # not trip the branch-mutation guard (the checkout branch-vs-path ambiguity).
    (repo / "other.txt").write_text("v1\n")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "add other")
    (repo / "other.txt").write_text("v2-uncommitted\n")
    _mark(repo, home)
    res = _run_hook(f"cd {repo} && git checkout HEAD -- other.txt && git commit -m wip", repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_staging_between_commits_blocks(repo: Path, home: Path) -> None:
    # P1: a chain that stages new content between commits records unreviewed content
    # in the 2nd commit under the 1st's marker (git add between → git-between-commits).
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git commit -m reviewed && echo x > n.py && git add n.py && "
        f"git commit -m unseen",
        repo,
        home,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot bind to the reviewed diff" in res.stderr


def test_dash_am_second_commit_blocks(repo: Path, home: Path) -> None:
    # BLOCKER-1 (architect): the 2nd commit stages its OWN content with -a. No `git
    # add` segment exists to catch, but the impure-later-commit allowlist check does.
    (repo / "tracked.py").write_text("v = 1\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-qm", "add tracked")
    (repo / "f.py").write_text("base = 9\n")
    _git(repo, "add", "-A")
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git commit -m reviewed && echo x >> tracked.py && git commit -am quick",
        repo,
        home,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "pure" in res.stderr


def test_stash_pop_between_commits_blocks(repo: Path, home: Path) -> None:
    # SHOULD-FIX-2 (architect): `git stash pop` re-stages the index and would bypass a
    # staging BLOCKLIST — the allowlist blocks it as a non-commit git segment between.
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git commit -m a && git stash pop && git commit --amend --no-edit",
        repo,
        home,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "another git command runs between" in res.stderr


def test_non_git_command_between_amends_allowed(repo: Path, home: Path) -> None:
    # A non-git command (a build/echo step) between a commit and a pure --amend is
    # safe — a pure --amend never stages unstaged working-tree edits.
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git commit -m wip && echo built && git commit --amend --no-edit",
        repo,
        home,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_amend_with_no_intervening_staging_allowed(repo: Path, home: Path) -> None:
    # `git commit && git commit --amend` (nothing staged between) is content-
    # preserving and must stay allowed.
    _mark(repo, home)
    res = _run_hook(f"cd {repo} && git commit -m wip && git commit --amend -m better", repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_add_before_single_commit_still_allowed(repo: Path, home: Path) -> None:
    # A staging op BEFORE the (single) commit is the normal add-then-commit path.
    _git(repo, "reset", "-q")
    (repo / "g.py").write_text("g = 1\n")
    _mark(repo, home)
    res = _run_hook(f"cd {repo} && git add -A && git commit -m wip", repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_same_worktree_different_subdir_chain_allowed(repo: Path, home: Path) -> None:
    # P2: two commits addressed from DIFFERENT SUBDIRECTORIES of the SAME worktree
    # share one index+marker — worktree-identity compare must not false-block them.
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "keep.txt").write_text("k\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed subdir")
    (repo / "f.py").write_text("base = 3\n")
    _git(repo, "add", "-A")
    _mark(repo, home)
    res = _run_hook(
        f"cd {repo} && git commit -m a && cd {sub} && git commit --amend --no-edit",
        repo,
        home,
    )
    assert res.returncode == 0, res.stdout + res.stderr
