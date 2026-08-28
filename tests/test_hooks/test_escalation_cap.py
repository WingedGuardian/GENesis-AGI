"""Tests for the review escalation cap — the machine-enforced round counter +
commit-gate block (scripts/review_state.py + scripts/review_enforcement_commit.py).

Hermetic: throwaway git repo under ``tmp_path`` with ``HOME`` pointed at another
temp dir, so the real per-worktree markers/round counters under ``~/.genesis/``
are never touched. No network, no live server. (Scratch git in a pytest
subprocess is not blocked by the interactive CC guards — per the hook-testing
convention.)
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

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "hooks"))
import review_state  # noqa: E402


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


def _stage(repo: Path, content: str) -> None:
    (repo / "f.py").write_text(content)
    _git(repo, "add", "-A")


# ── Counter unit tests (review_state) ─────────────────────────────────────


@pytest.fixture
def _isolate_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(review_state, "_ROUND_DIR", tmp_path / "rounds")


def test_bump_increments_on_distinct_content(repo, _isolate_rounds):
    # Two one-line fixes to the SAME file (identical --stat, different content):
    # the round counter must key on CONTENT, so both count as distinct rounds.
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1
    assert review_state.get_review_round(cwd=str(repo)) == 1
    _stage(repo, "a = 3\n")  # same stat, different content → new round
    assert review_state.bump_review_round(cwd=str(repo)) == 2
    assert review_state.get_review_round(cwd=str(repo)) == 2


def test_remark_same_content_no_increment(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1
    # Re-mark the identical staged diff (re-ran /review, no fix) → NOT a new round.
    assert review_state.bump_review_round(cwd=str(repo)) == 1


def test_branch_change_resets(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo))
    _stage(repo, "a = 3\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 2
    _git(repo, "commit", "-qm", "wip")
    _git(repo, "checkout", "-q", "-b", "feature/y")
    _stage(repo, "b = 1\n")
    assert review_state.get_review_round(cwd=str(repo)) == 0  # different branch → fresh
    assert review_state.bump_review_round(cwd=str(repo)) == 1  # reset to 1


def test_get_round_zero_for_other_branch(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo))
    _git(repo, "commit", "-qm", "wip")
    _git(repo, "checkout", "-q", "-b", "other")
    assert review_state.get_review_round(cwd=str(repo)) == 0


def test_reset_clears(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo))
    review_state.reset_review_round(cwd=str(repo))
    assert review_state.get_review_round(cwd=str(repo)) == 0


# ── Defect-bearing streak + reset-on-clean (option (e), round-4 fix) ───────


def test_clean_mark_resets_streak(repo, _isolate_rounds):
    # The counter tracks CONSECUTIVE defect-bearing rounds. Two defect-bearing
    # rounds, then a clean review, must reset the streak to 0 — and the NEXT
    # defect-bearing round starts fresh at 1, not resume at 3.
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1
    _stage(repo, "a = 3\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 2
    assert review_state.bump_review_round(cwd=str(repo), clean=True) == 0
    assert review_state.get_review_round(cwd=str(repo)) == 0
    _stage(repo, "a = 4\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1


def test_clean_resets_even_on_same_diff(repo, _isolate_rounds):
    # A clean re-probe of the SAME staged diff still closes the breaker (reset),
    # even though a defect-bearing re-mark of the same diff would be idempotent.
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1
    assert review_state.bump_review_round(cwd=str(repo)) == 1  # same diff, idempotent
    assert review_state.bump_review_round(cwd=str(repo), clean=True) == 0


def test_defect_mark_on_same_diff_after_clean_reset_stays_zero(repo, _isolate_rounds):
    # A clean reset records last_hash=<current>; a defect-bearing re-mark of the
    # IDENTICAL staged diff is idempotent (nothing changed) → streak stays 0. The
    # moment a real fix is staged (hash changes) the streak resumes at 1.
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo))  # round 1
    review_state.bump_review_round(cwd=str(repo), clean=True)  # reset, last_hash=H(a=2)
    assert review_state.bump_review_round(cwd=str(repo)) == 0  # same diff → no new round
    _stage(repo, "a = 3\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1  # real change resumes at 1


def test_defect_bearing_streak_still_reaches_cap_after_clean(repo, _isolate_rounds):
    # Reset-on-clean must NOT defang the cap: after a clean reset, three fresh
    # defect-bearing rounds still reach the cap (a genuine loop is still caught).
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo))
    review_state.bump_review_round(cwd=str(repo), clean=True)  # reset
    for i, val in enumerate(("b = 1\n", "b = 2\n", "b = 3\n"), 1):
        _stage(repo, val)
        assert review_state.bump_review_round(cwd=str(repo)) == i
    assert review_state.get_review_round(cwd=str(repo)) == review_state.ESCALATION_ROUND_CAP


# ── Gate integration tests (review_enforcement_commit, subprocess) ────────


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


def _mark(repo: Path, home: Path, *, clean: bool = False) -> subprocess.CompletedProcess:
    (home / ".genesis" / "last_code_review.txt").write_text("adversarial review: OK\n")
    env = {**os.environ, "HOME": str(home)}
    args = [
        sys.executable,
        str(_REVIEW_STATE),
        "mark",
        "--agent-output",
        str(home / ".genesis" / "last_code_review.txt"),
    ]
    if clean:
        args.append("--clean")
    return subprocess.run(
        args,
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _reach_rounds(repo: Path, home: Path, n: int) -> None:
    """Drive the round counter to ``n`` via distinct staged diffs + marks."""
    for i in range(1, n + 1):
        _stage(repo, f"a = {i}\n")
        m = _mark(repo, home)
        assert m.returncode == 0, m.stderr


def test_commit_blocked_at_round_cap(repo, home):
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)  # 3 rounds
    # Review IS current for the latest staged diff (just marked), so Rule 2 passes —
    # the escalation cap (Rule 3) is what must now block.
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "escalation cap" in res.stderr


def test_escalation_ack_allows_past_cap(repo, home):
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    res = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert res.returncode == 0, res.stderr


def test_ack_inside_message_does_not_bypass(repo, home):
    # The token buried in the -m string (not a clean trailing comment) must NOT ack.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    res = _run_hook('git commit -m "escalation-ack please"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "escalation cap" in res.stderr


def test_first_defect_round_not_blocked(repo, home):
    # One defect-bearing round (below the mode-switch tier) → allowed with no ack.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 2)  # 1 round
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 0, res.stderr


# ── Tier 1: round CAP-1 mode-switch (audit-ack) ───────────────────────────


def test_mode_switch_blocks_at_second_defect_round(repo, home):
    # Two consecutive defect-bearing rounds → the mode-switch tier blocks (one round
    # BEFORE the hard cap), demanding a fresh-eyes audit rather than another patch.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)  # 2 rounds
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "mode-switch" in res.stderr
    assert "audit-ack" in res.stderr


def test_audit_ack_allows_past_mode_switch(repo, home):
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)
    res = _run_hook('git commit -m "wip"  # audit-ack', repo, home)
    assert res.returncode == 0, res.stderr


def test_audit_ack_inside_message_does_not_bypass(repo, home):
    # The token buried in -m (not a clean trailing comment) must NOT ack.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)
    res = _run_hook('git commit -m "did the audit-ack thing"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "mode-switch" in res.stderr


def test_escalation_ack_does_not_satisfy_mode_switch(repo, home):
    # The two acks are distinct: at the round-2 mode-switch the gate wants the
    # AUDIT ack, not the round-3 user-decision ack. A wrong sigil stays blocked.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)
    res = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "mode-switch" in res.stderr


def test_audit_ack_does_not_reset_counter_so_round3_still_hard_stops(repo, home):
    # The mode-switch ack lets THIS commit through but must NOT reset the streak:
    # if the "audit" didn't actually converge and a third defect round follows,
    # the hard cap must still fire. (Only a CLEAN review resets the streak.)
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)  # round 2
    r_mode = _run_hook('git commit -m "audited"  # audit-ack', repo, home)
    assert r_mode.returncode == 0, r_mode.stderr
    _git(repo, "commit", "-qm", "audited")  # land it
    _stage(repo, "third = 1\n")  # a THIRD distinct defect round
    assert _mark(repo, home).returncode == 0
    # Assert via the gate (subprocess under the test HOME) — an in-process
    # get_review_round() would read the REAL ~/.genesis, not the test home. The
    # hard stop firing proves the counter reached the cap (audit-ack did NOT reset).
    r_hard = _run_hook('git commit -m "wip3"', repo, home)
    assert r_hard.returncode == 2, r_hard.stdout + r_hard.stderr
    assert "escalation cap" in r_hard.stderr


def test_audit_ack_then_clean_review_resets(repo, home):
    # The intended happy path: mode-switch → do the audit → a CLEAN review resets
    # the streak, so the branch is back to zero friction (no round-3 stop).
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)  # round 2
    r_mode = _run_hook('git commit -m "audited"  # audit-ack', repo, home)
    assert r_mode.returncode == 0, r_mode.stderr
    _git(repo, "commit", "-qm", "audited")
    _stage(repo, "post_audit = 1\n")
    m = _mark(repo, home, clean=True)  # audit converged → clean review resets streak
    assert m.returncode == 0 and "streak reset" in m.stdout
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_review_override_does_not_bypass_cap(repo, home):
    # The cap is checked BEFORE Rule 2, so a '# review-override' (which would exit
    # the review rule early) can NOT sneak a past-cap commit through.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    _stage(repo, "unreviewed = 1\n")  # make review stale so override would apply
    res = _run_hook('git commit -m "wip"  # review-override', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "escalation cap" in res.stderr


def test_ack_resets_budget_no_permanent_friction(repo, home):
    # Acking is a fresh decision → resets the round budget, so subsequent commits
    # (back under the cap) don't each need a fresh ack.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    r1 = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert r1.returncode == 0, r1.stderr  # ack allows + resets the counter
    _git(repo, "commit", "-qm", "wip")  # actually land it
    _stage(repo, "more = 1\n")
    assert _mark(repo, home).returncode == 0  # one fresh review → round 1
    r2 = _run_hook('git commit -m "wip2"', repo, home)
    assert r2.returncode == 0, r2.stderr  # under cap again, no ack needed


def test_codex_scenario_independent_clean_reviews_never_block(repo, home):
    # Codex round-4 finding, verbatim: "When a branch contains three independently
    # reviewed commits, or simply three clean reviews of distinct staged diffs,
    # [the old counter] increments every time ... The commit gate then hard-blocks
    # routine multi-commit development at round 3 even though no escalating
    # review→fix loop occurred." With reset-on-clean the streak stays 0 throughout,
    # so four clean-reviewed commits all pass the gate.
    for i in range(1, 5):
        _stage(repo, f"clean_{i} = {i}\n")
        m = _mark(repo, home, clean=True)
        assert m.returncode == 0, m.stderr
        res = _run_hook(f'git commit -m "commit {i}"', repo, home)
        assert res.returncode == 0, res.stdout + res.stderr
        _git(repo, "commit", "-qm", f"commit {i}")  # actually land it


def test_clean_mark_via_cli_prevents_block_after_defect_rounds(repo, home):
    # Full CLI plumbing: two defect-bearing rounds, then a clean review via the
    # `--clean` flag resets the streak, so the next commit is not blocked.
    _reach_rounds(repo, home, 2)  # 2 defect-bearing rounds
    _stage(repo, "clean_pass = 1\n")
    m = _mark(repo, home, clean=True)
    assert m.returncode == 0, m.stderr
    assert "streak reset" in m.stdout
    key = review_state._worktree_key(cwd=str(repo))
    rf = home / ".genesis" / "review_rounds" / f"{key}.json"
    assert json.loads(rf.read_text())["round"] == 0
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_clean_staged_mark_does_not_inflate(repo, _isolate_rounds):
    # A mark with nothing staged ("clean" content hash) is not a review round.
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1
    _git(repo, "commit", "-qm", "wip")  # staged area now clean
    assert review_state.bump_review_round(cwd=str(repo)) == 1  # no inflation


def test_docs_only_commit_still_blocked_at_cap(repo, home):
    # The hard stop must NOT be bypassable by file extension: a docs/skill-only
    # commit at the cap (which would otherwise hit the docs-skip) is still blocked.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    _git(repo, "commit", "-qm", "land code")  # clear staging (direct git, no gate)
    (repo / "README.md").write_text("# docs change\n")
    _git(repo, "add", "-A")  # staged set is now docs-only
    res = _run_hook('git commit -m "docs"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "escalation cap" in res.stderr
    # ...and an ack lets the docs commit through.
    res2 = _run_hook('git commit -m "docs"  # escalation-ack', repo, home)
    assert res2.returncode == 0, res2.stderr


def test_corrupt_round_counter_does_not_crash(repo, _isolate_rounds):
    # A non-integer 'round' (corrupt / partial write / version skew) must NOT raise
    # from bump — marking a review must always succeed (best-effort counter).
    import json

    _stage(repo, "a = 2\n")
    rf = review_state._round_file(cwd=str(repo))
    rf.parent.mkdir(parents=True, exist_ok=True)
    branch = review_state.get_current_branch(cwd=str(repo))
    rf.write_text(json.dumps({"branch": branch, "round": "not-an-int", "last_hash": "old"}))
    result = review_state.bump_review_round(cwd=str(repo))  # must not raise
    assert result == 1  # coerced the bad value to 0, then +1


# ── Malformed counter-file shape (round-3 class fix) ──────────────────────


def _write_round_file(repo, content: str) -> None:
    rf = review_state._round_file(cwd=str(repo))
    rf.parent.mkdir(parents=True, exist_ok=True)
    rf.write_text(content)


@pytest.mark.parametrize("bad", ["[]", "42", '"str"', "null", "[1, 2, 3]"])
def test_load_round_returns_empty_for_non_dict(repo, _isolate_rounds, bad):
    # Valid JSON that is not an object (manual edit / schema skew) must normalize to
    # {} at the load boundary, so no caller's .get() can ever raise.
    _write_round_file(repo, bad)
    assert review_state._load_round(cwd=str(repo)) == {}


def test_get_review_round_no_crash_on_truthy_non_dict(repo, _isolate_rounds):
    # A TRUTHY non-dict ([1,2,3], 42) is the real crasher (empty [] is falsy and was
    # already short-circuited) — get_review_round must fail open to 0, not raise.
    _write_round_file(repo, "[1, 2, 3]")
    assert review_state.get_review_round(cwd=str(repo)) == 0


def test_bump_no_crash_on_truthy_non_dict(repo, _isolate_rounds):
    _write_round_file(repo, "42")
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1  # treats as fresh, no crash


def test_gate_fails_open_on_non_dict_counter(repo, home):
    # End-to-end: a truthy non-dict counter file must NOT crash the commit gate.
    _stage(repo, "a = 2\n")
    assert _mark(repo, home).returncode == 0
    key = review_state._worktree_key(cwd=str(repo))
    rf = home / ".genesis" / "review_rounds" / f"{key}.json"
    rf.parent.mkdir(parents=True, exist_ok=True)
    rf.write_text("[1, 2, 3]")  # non-dict → gate must fail open, not crash
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


# ── Malformed counter-file VALUE (round-5 class fix: 1e999 → inf → OverflowError) ──


def test_coerce_finite_int_rejects_non_finite():
    # int(float('inf')) raises OverflowError (not caught by TypeError/ValueError);
    # int(nan) raises ValueError. The helper must absorb ALL of them → default.
    assert review_state._coerce_finite_int(float("inf")) == 0
    assert review_state._coerce_finite_int(float("-inf")) == 0
    assert review_state._coerce_finite_int(float("nan")) == 0
    assert review_state._coerce_finite_int("not-a-number") == 0
    assert review_state._coerce_finite_int(None) == 0
    assert review_state._coerce_finite_int(5) == 5
    assert review_state._coerce_finite_int(3.9) == 3  # finite float still truncates


def test_load_round_coerces_non_finite_round_to_zero(repo, _isolate_rounds):
    # A well-formed dict whose 'round' is a non-finite JSON number (1e999 → inf)
    # must be coerced to 0 at the load boundary — int(inf) OverflowError escapes the
    # plain int() guards and would crash the gate. Both the load boundary and
    # get_review_round must fail open to 0.
    branch = review_state.get_current_branch(cwd=str(repo))
    _write_round_file(repo, '{"branch": "' + branch + '", "round": 1e999, "last_hash": "x"}')
    assert review_state._load_round(cwd=str(repo))["round"] == 0
    assert review_state.get_review_round(cwd=str(repo)) == 0


def test_bump_no_crash_on_infinity_round(repo, _isolate_rounds):
    # A defect-bearing bump over an infinity 'round' must not raise; it coerces the
    # bad value to 0 and increments to 1.
    branch = review_state.get_current_branch(cwd=str(repo))
    _write_round_file(repo, '{"branch": "' + branch + '", "round": 1e999, "last_hash": "old"}')
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 1


def test_gate_fails_open_on_infinity_round(repo, home):
    # End-to-end: the gate calls get_review_round() unguarded; an infinity 'round'
    # in the counter file must fail open (round→0 < cap → allowed), not crash.
    _stage(repo, "a = 2\n")
    assert _mark(repo, home).returncode == 0
    branch = review_state.get_current_branch(cwd=str(repo))
    key = review_state._worktree_key(cwd=str(repo))
    rf = home / ".genesis" / "review_rounds" / f"{key}.json"
    rf.parent.mkdir(parents=True, exist_ok=True)
    rf.write_text('{"branch": "' + branch + '", "round": 1e999, "last_hash": "x"}')
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


# ── A merge must NOT be able to silence the counter ───────────────────────


def _fabricate_merge_sentinel(repo: Path) -> None:
    """One file write. No git command, no real merge."""
    (repo / ".git" / "MERGE_HEAD").write_text("garbage-not-a-sha\n")


def test_a_forged_merge_sentinel_cannot_freeze_the_counter(repo, _isolate_rounds):
    """The counter must not trust unauthenticated on-disk state.

    An earlier attempt at this exempted rounds whenever a merge sentinel was
    present. Those sentinels are plain files: `echo x > .git/MERGE_HEAD` needs
    no git command, and `git merge --no-commit` leaves one indefinitely with no
    forgery at all. Measured against that version, a single write froze the
    counter across three further distinct defect rounds — letting the exact
    actor this gate constrains disable the anti-runaway cap permanently.

    The exemption was abandoned for that reason. The gate now only MENTIONS a
    merge in its denial text; it never lets one decide the verdict.
    """
    _stage(repo, "base = 1\nfix_one = True\n")
    assert review_state.bump_review_round(str(repo)) == 1

    _fabricate_merge_sentinel(repo)

    _stage(repo, "base = 1\nfix_two = True\n")
    assert review_state.bump_review_round(str(repo)) == 2, (
        "a forged merge sentinel suppressed a real defect-bearing round"
    )
    _stage(repo, "base = 1\nfix_three = True\n")
    assert review_state.bump_review_round(str(repo)) == 3, (
        "the escalation cap can be silenced by writing a file"
    )


def test_a_real_merge_also_does_not_suppress_rounds(repo, _isolate_rounds):
    """Not even a genuine merge exempts a round — by design.

    A real conflicted merge is indistinguishable from a forged sentinel to any
    check cheap enough to run in a hook, so the counter treats neither as
    special. The cost is one ack on a merge commit; the alternative was a gate
    that could be turned off silently.
    """
    _stage(repo, "base = 1\nfix_one = True\n")
    assert review_state.bump_review_round(str(repo)) == 1

    _git(repo, "checkout", "-q", "main")
    (repo / "f.py").write_text("base = 1\nupstream = True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "upstream")
    _git(repo, "checkout", "-q", "feature/x")
    (repo / "f.py").write_text("base = 1\nmine = True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "mine")
    subprocess.run(["git", "merge", "main"], cwd=repo, capture_output=True, text=True)
    (repo / "f.py").write_text("base = 1\nmine = True\nupstream = True\n")
    _git(repo, "add", "-A")

    assert review_state.bump_review_round(str(repo)) == 2
