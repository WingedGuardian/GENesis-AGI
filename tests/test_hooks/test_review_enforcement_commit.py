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
    """Run `review_state.py mark` with fresh agent-output evidence, no gstack."""
    (home / ".genesis" / "last_code_review.txt").write_text("adversarial review: OK\n")
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [
            sys.executable,
            str(_REVIEW_STATE),
            "mark",
            "--agent-output",
            str(home / ".genesis" / "last_code_review.txt"),
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
    """Authoritative evidence (agent output) is still mandatory."""
    env = {**os.environ, "HOME": str(home)}
    res = subprocess.run(
        [
            sys.executable,
            str(_REVIEW_STATE),
            "mark",
            "--agent-output",
            str(home / ".genesis" / "does_not_exist.txt"),
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 1
    assert "REFUSED" in res.stderr


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
