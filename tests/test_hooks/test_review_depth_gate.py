"""Integration tests for the commit-gate review-DEPTH rule (Rule 2.5).

Hermetic: throwaway git repo under ``tmp_path`` with ``HOME`` redirected, so real
~/.genesis markers are untouched. Drives scripts/review_enforcement_commit.py as a
subprocess exactly as the CC PreToolUse hook does (JSON payload on stdin).

The rule: a SUBSTANTIAL staged change requires an ADVERSARIAL review marker; a
shallow/inline pass BLOCKS (exit 2). '# review-override' waives FINDINGS but NOT
depth (D1); '# depth-ack' is the audited escape. Inline changes are unaffected.
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

# A review artifact with adversarial-audit structure: a severity ladder + a
# file:line pointer + past the length floor.
_ADVERSARIAL = (
    "Scope Check: CLEAN\n"
    "BLOCKER 1 — off-by-one at f.py:1 mishandles the empty case.\n"
    "SHOULD-FIX 2 — missing boundary validation.\n"
    "NOTE 3 — consider a test for the None input.\n"
    "Completion status: DONE.\n" + "detail " * 80
)
_SHALLOW = "Reviewed the change. Looks good to me. 88% confident.\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "f.py").write_text("base = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "feature/x")
    return r


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".genesis").mkdir(parents=True)
    return h


def _stage_substantial(repo: Path) -> None:
    (repo / "f.py").write_text("base = 1\n" + "".join(f"x{i} = {i}\n" for i in range(60)))
    _git(repo, "add", "-A")


def _stage_inline(repo: Path) -> None:
    (repo / "f.py").write_text("base = 2\n")
    _git(repo, "add", "-A")


def _mark(
    repo: Path, home: Path, evidence: str, *, sid: str | None = None
) -> subprocess.CompletedProcess:
    """Write review evidence then run `review_state.py mark` (computes level/adversarial).

    ``mark`` now requires an outcome flag; these depth tests pass ``--defects`` (the
    escalation streak is irrelevant here). ``sid`` overrides CLAUDE_CODE_SESSION_ID so a
    test can control which planted transcript dir (if any) the depth fallback resolves.
    """
    (home / ".genesis" / "last_code_review.txt").write_text(evidence)
    env = {**os.environ, "HOME": str(home)}
    if sid is not None:
        env["CLAUDE_CODE_SESSION_ID"] = sid
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


def _plant_transcript(
    home: Path, sid: str, text: str, *, slug: str = "-test-repo", age_s: float = 0.0
) -> Path:
    """Write a fake CC subagent transcript whose assistant text block is ``text``.

    Mirrors the real layout ~/.claude/projects/<slug>/<session-id>/subagents/agent-*.jsonl
    that Fix B's _transcript_is_adversarial globs by session-id. ``age_s`` back-dates the
    file mtime (to exercise the freshness window).
    """
    d = home / ".claude" / "projects" / slug / sid / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "agent-abc123.jsonl"
    rec = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    f.write_text(json.dumps(rec) + "\n")
    if age_s:
        old = f.stat().st_mtime - age_s
        os.utime(f, (old, old))
    return f


def _run_hook(command: str, repo: Path, home: Path) -> subprocess.CompletedProcess:
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "test",
        }
    )
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


# --------------------------------------------------------------------------- #
def test_substantial_without_marker_blocked_on_depth(repo, home):
    _stage_substantial(repo)
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 2
    assert "review depth" in r.stderr.lower()


def test_substantial_with_shallow_marker_blocked(repo, home):
    _stage_substantial(repo)
    _mark(repo, home, _SHALLOW)  # marker: level=substantial, adversarial=False
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 2
    assert "review depth" in r.stderr.lower()


def test_substantial_with_adversarial_marker_allowed(repo, home):
    _stage_substantial(repo)
    m = _mark(repo, home, _ADVERSARIAL)
    assert m.returncode == 0
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 0, r.stderr


def test_inline_with_shallow_marker_allowed(repo, home):
    # A small inline change never triggers the depth requirement.
    _stage_inline(repo)
    _mark(repo, home, _SHALLOW)
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 0, r.stderr


def test_depth_ack_allows_substantial_shallow(repo, home):
    _stage_substantial(repo)
    _mark(repo, home, _SHALLOW)
    r = _run_hook('git commit -m "x"  # depth-ack', repo, home)
    assert r.returncode == 0, r.stderr
    assert "depth-ack honored" in r.stderr.lower()


def test_review_override_does_not_waive_depth(repo, home):
    # D1: '# review-override' waives FINDINGS but must NOT waive the depth requirement.
    _stage_substantial(repo)
    _mark(repo, home, _SHALLOW)
    r = _run_hook('git commit -m "x"  # review-override', repo, home)
    assert r.returncode == 2
    assert "review depth" in r.stderr.lower()


def test_depth_ack_inside_quotes_does_not_waive(repo, home):
    # '# depth-ack' inside the commit MESSAGE (quoted) is not a clean trailing shell
    # comment, so it must NOT waive depth — mirrors the other sigils' in-quote handling.
    _stage_substantial(repo)
    _mark(repo, home, _SHALLOW)
    r = _run_hook('git commit -m "fix bug  # depth-ack"', repo, home)
    assert r.returncode == 2
    assert "review depth" in r.stderr.lower()


def test_stale_adversarial_marker_does_not_clear_new_diff(repo, home):
    # D1-diffbind (Codex P2): an adversarial audit of diff A must NOT clear a DIFFERENT
    # substantial diff B. The marker is adversarial + current for A; restaging B makes it
    # stale. Rule 2's staleness block is waivable by '# review-override', so depth must
    # catch B on its own — the marker's adversarial bit counts ONLY when it is CURRENT for
    # the staged diff. (verify-RED: B committed cleanly before the diff-binding fix.)
    #
    # Depth clearance binds the marker's FULL-content hash (marker_content_current), so
    # even a SAME-SHAPE swap — B shares A's files and ±line counts, differing only in
    # content — is caught (the stat-only diff_hash would collide and wrongly read current).
    _stage_substantial(repo)  # A: f.py, +60 lines of "x{i} = {i}"
    m = _mark(repo, home, _ADVERSARIAL)  # adversarial marker bound to diff A's content
    assert m.returncode == 0
    # restage a SAME-SHAPE substantial diff B (f.py, +60 lines, different content)
    (repo / "f.py").write_text("base = 1\n" + "".join(f"z{i} = {i}\n" for i in range(60)))
    _git(repo, "add", "-A")
    r = _run_hook('git commit -m "x"  # review-override', repo, home)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "review depth" in r.stderr.lower()


def test_add_and_commit_chain_falls_back_to_marker_level(repo, home):
    # `git add && commit` in ONE command: the index is EMPTY at hook time, so the
    # current-staged classify reads "inline" — depth must fall back to the marker's
    # RECORDED substantial level, or a substantial change would slip the gate here.
    _stage_substantial(repo)
    _mark(repo, home, _SHALLOW)  # marker.level = substantial, adversarial = False
    _git(repo, "reset", "-q")  # empty the index → classify(cwd) would see "inline"
    r = _run_hook('git add -A && git commit -m "x"', repo, home)
    assert r.returncode == 2
    assert "review depth" in r.stderr.lower()


# ── Fix B (e1372b30): session architect transcript corroborates a thin summary ──
# A real genesis-architect audit's file:line findings live in the AGENT transcript; a
# terse hand-written summary can lack a literal file:line token and be false-blocked.
# The fallback consults THIS session's own fresh transcripts under the SAME structural
# bar — additive (only ever GRANTS recognition) and fail-closed on any gap.

_SID = "11111111-2222-3333-4444-555555555555"


def test_substantial_thin_summary_dense_transcript_allowed(repo, home):
    # Reproduces e1372b30: a substantial change, a THIN summary (no file:line), but a
    # genuine adversarial architect transcript in THIS session → recognized → commit passes.
    _stage_substantial(repo)
    _plant_transcript(home, _SID, _ADVERSARIAL)  # dense file:line + ladder + length
    m = _mark(repo, home, _SHALLOW, sid=_SID)  # thin summary; sid points at the transcript
    assert m.returncode == 0, m.stderr
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 0, r.stdout + r.stderr


def test_substantial_shallow_no_transcript_still_blocked(repo, home):
    # LOCKING: thin summary AND no in-session transcript → still blocks (the fallback
    # grants nothing when there is no real audit to find).
    _stage_substantial(repo)
    m = _mark(repo, home, _SHALLOW, sid=_SID)  # sid set, but no transcript planted
    assert m.returncode == 0
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "review depth" in r.stderr.lower()


def test_shallow_summary_with_stale_transcript_still_blocked(repo, home):
    # LOCKING: a real architect transcript that is STALE (older than the freshness
    # window) must not vouch — it could be from an unrelated earlier change.
    _stage_substantial(repo)
    _plant_transcript(home, _SID, _ADVERSARIAL, age_s=2000)  # > 1800s → stale
    m = _mark(repo, home, _SHALLOW, sid=_SID)
    assert m.returncode == 0
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "review depth" in r.stderr.lower()


def test_shallow_summary_and_shallow_transcript_still_blocked(repo, home):
    # LOCKING: the SAME bar is applied to the transcript — a rubber-stamp transcript
    # (no ladder / file:line / length) does NOT vouch.
    _stage_substantial(repo)
    _plant_transcript(home, _SID, _SHALLOW)  # transcript is itself shallow
    m = _mark(repo, home, _SHALLOW, sid=_SID)
    assert m.returncode == 0
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "review depth" in r.stderr.lower()


def test_missing_session_id_env_falls_back_fail_closed(repo, home):
    # LOCKING: with CLAUDE_CODE_SESSION_ID empty the fallback is unavailable and the
    # summary answer stands — a present transcript is ignored (no session to bind to).
    _stage_substantial(repo)
    _plant_transcript(home, _SID, _ADVERSARIAL)  # present, but env has no session id
    m = _mark(repo, home, _SHALLOW, sid="")
    assert m.returncode == 0
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "review depth" in r.stderr.lower()


def test_generic_non_review_transcript_does_not_vouch(repo, home):
    # LOCKING (SHOULD-FIX 1): an incidental NON-review subagent transcript that passes the
    # generic bar (file:line + caps ladder word + length) but has no review signature must
    # NOT clear the depth gate — the fallback accepts only review-report-shaped transcripts.
    _stage_substantial(repo)
    generic = "The function at f.py:12 has HIGH cyclomatic complexity to note. " * 12
    _plant_transcript(home, _SID, generic)
    m = _mark(repo, home, _SHALLOW, sid=_SID)
    assert m.returncode == 0
    r = _run_hook('git commit -m "x"', repo, home)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "review depth" in r.stderr.lower()
