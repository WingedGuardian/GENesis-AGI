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
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _REPO_ROOT / "scripts" / "review_enforcement_commit.py"
_REVIEW_STATE = _REPO_ROOT / "scripts" / "review_state.py"
_ASK_HOOK = _REPO_ROOT / "scripts" / "hooks" / "ask_gate_demand.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "hooks"))
import review_scope  # noqa: E402
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


def test_the_shared_review_state_name_is_not_hijacked():
    """The premise every `monkeypatch.setattr(review_state, ...)` here rests on.

    `review_enforcement_commit` imports `review_state` at CALL time, so a patch
    applied in this file only reaches production code if this module's
    `review_state` and `sys.modules["review_state"]` are the SAME object.

    Other test modules load a private copy of that script under the shared name.
    pytest imports every test module at COLLECTION, so the last registration
    wins for the whole session: a module collected earlier keeps a reference to
    the object it bound, while production resolves whatever is in `sys.modules`
    now. The patch then lands on nobody and the real function runs — a failure
    invisible in a single-file run and visible only in the full suite, in
    collection order.

    Asserting the premise directly means the next unrestored hijack fails HERE,
    naming the cause, instead of surfacing as a bewildering assertion in an
    unrelated test. `tests.conftest.private_module` is the supported way to load
    a private copy without leaking the name.
    """
    assert sys.modules.get("review_state") is review_state, (
        "sys.modules['review_state'] is not the module this file imported — "
        "some test module loaded a private copy under the shared name and did "
        "not restore it, so monkeypatching this module patches nothing that "
        "production code will resolve. Load it via tests.conftest.private_module."
    )


def test_the_shared_review_scope_name_is_not_hijacked():
    """The sibling lock, for the second name the commit gate imports at call time.

    `review_scope` is resolved by a call-time import in five places, including
    `review_enforcement_commit.classify_change_substantiality`, so the premise is
    the same one the `review_state` lock above rests on: a patch applied to the
    object THIS file holds only reaches production if it is the object
    `sys.modules` hands the call-time import.

    This file binds `review_scope` at module scope purely so that identity exists
    to compare against — a canonical importer has to come from somewhere, and an
    earlier revision of this lock concluded from its absence that PATH equality
    was the best available check. It is not: two distinct module objects loaded
    from the SAME path compare equal by path and differ by identity, so a private
    copy left registered would pass a path assert while a `monkeypatch.setattr`
    on the canonical object reached nobody. That is the exact divergence this
    file exists to catch, and it is not hypothetical here —
    `tests/test_scripts/test_check_review_depth.py` patches
    `review_scope.classify_range_substantiality` against a call-time import at
    `scripts/check_review_depth.py:60`.
    """
    assert sys.modules.get("review_scope") is review_scope, (
        "sys.modules['review_scope'] is not the module this file imported — "
        "some test module loaded a private copy under the shared name and did "
        "not restore it. Same path is NOT sufficient: a same-path copy is a "
        "different object, so patches land on one and production resolves the "
        "other. Load it via tests.conftest.private_module."
    )


# ── Counter unit tests (review_state) ─────────────────────────────────────


@pytest.fixture
def _isolate_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(review_state, "_ROUND_DIR", tmp_path / "rounds")


def test_bump_increments_on_distinct_content(repo, _isolate_rounds):
    # Two one-line fixes to the SAME file (identical --stat, different content):
    # the round counter must key on CONTENT, so both count as distinct rounds.
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 1
    assert review_state.get_review_round(cwd=str(repo)) == 1
    _stage(repo, "a = 3\n")  # same stat, different content → new round
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 2
    assert review_state.get_review_round(cwd=str(repo)) == 2


def test_bump_treats_non_external_source_as_internal(repo, _isolate_rounds):
    # The counting brain is fail-safe: ONLY the literal "external" counts. The default
    # (internal) and any other value (a direct-API caller passing garbage) are treated
    # as internal → never write the round file, even across distinct staged diffs.
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo)) == 0  # default source=internal
    assert review_state.bump_review_round(cwd=str(repo), source="garbage") == 0
    _stage(repo, "a = 3\n")  # distinct diff — still must not count for a non-external
    assert review_state.bump_review_round(cwd=str(repo), source="internal") == 0
    assert review_state.get_review_round(cwd=str(repo)) == 0


def test_remark_same_content_no_increment(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 1
    # Re-mark the identical staged diff (re-ran /review, no fix) → NOT a new round.
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 1


def test_branch_change_resets(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo), source="external")
    _stage(repo, "a = 3\n")
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 2
    _git(repo, "commit", "-qm", "wip")
    _git(repo, "checkout", "-q", "-b", "feature/y")
    _stage(repo, "b = 1\n")
    assert review_state.get_review_round(cwd=str(repo)) == 0  # different branch → fresh
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 1  # reset to 1


def test_get_round_zero_for_other_branch(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo), source="external")
    _git(repo, "commit", "-qm", "wip")
    _git(repo, "checkout", "-q", "-b", "other")
    assert review_state.get_review_round(cwd=str(repo)) == 0


def test_reset_clears(repo, _isolate_rounds):
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo), source="external")
    review_state.reset_review_round(cwd=str(repo))
    assert review_state.get_review_round(cwd=str(repo)) == 0


def test_legacy_counter_without_last_source_is_discarded(repo, _isolate_rounds):
    # A round file written by the pre-source-axis (reviewer-agnostic) code has a `round`
    # but NO `last_source`; under the old model that count was built entirely from INTERNAL
    # self-reviews. On upgrade it must NOT be trusted (else the commit gate keeps blocking on
    # a never-cross-model streak — the exact false block this change removes). It is discarded:
    # get_review_round → 0, and the first EXTERNAL mark starts fresh at 1 (not legacy+1), now
    # stamping last_source so it is trusted henceforth. Self-heals without a data repair.
    branch = review_state.get_current_branch(cwd=str(repo))
    rf = review_state._round_file(cwd=str(repo))
    rf.parent.mkdir(parents=True, exist_ok=True)
    rf.write_text(json.dumps({"branch": branch, "round": 2, "last_hash": "deadbeef"}))
    assert review_state.get_review_round(cwd=str(repo)) == 0  # legacy discarded, not 2
    _stage(repo, "legacy_fix = 1\n")
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 1  # fresh, not 3
    st = json.loads(rf.read_text())
    assert st["last_source"] == "external" and st["round"] == 1


# ── Defect-bearing streak + reset-on-clean (option (e), round-4 fix) ───────


def test_clean_mark_resets_streak(repo, _isolate_rounds):
    # The counter tracks CONSECUTIVE defect-bearing rounds. Two defect-bearing
    # rounds, then a clean review, must reset the streak to 0 — and the NEXT
    # defect-bearing round starts fresh at 1, not resume at 3.
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 1
    _stage(repo, "a = 3\n")
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 2
    assert review_state.bump_review_round(cwd=str(repo), source="external", clean=True) == 0
    assert review_state.get_review_round(cwd=str(repo)) == 0
    _stage(repo, "a = 4\n")
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 1


def test_clean_resets_even_on_same_diff(repo, _isolate_rounds):
    # A clean re-probe of the SAME staged diff still closes the breaker (reset),
    # even though a defect-bearing re-mark of the same diff would be idempotent.
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 1
    assert (
        review_state.bump_review_round(cwd=str(repo), source="external") == 1
    )  # same diff, idempotent
    assert review_state.bump_review_round(cwd=str(repo), source="external", clean=True) == 0


def test_defect_mark_on_same_diff_after_clean_reset_stays_zero(repo, _isolate_rounds):
    # A clean reset records last_hash=<current>; a defect-bearing re-mark of the
    # IDENTICAL staged diff is idempotent (nothing changed) → streak stays 0. The
    # moment a real fix is staged (hash changes) the streak resumes at 1.
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo), source="external")  # round 1
    review_state.bump_review_round(
        cwd=str(repo), source="external", clean=True
    )  # reset, last_hash=H(a=2)
    assert (
        review_state.bump_review_round(cwd=str(repo), source="external") == 0
    )  # same diff → no new round
    _stage(repo, "a = 3\n")
    assert (
        review_state.bump_review_round(cwd=str(repo), source="external") == 1
    )  # real change resumes at 1


def test_defect_bearing_streak_still_reaches_cap_after_clean(repo, _isolate_rounds):
    # Reset-on-clean must NOT defang the cap: after a clean reset, three fresh
    # defect-bearing rounds still reach the cap (a genuine loop is still caught).
    _stage(repo, "a = 2\n")
    review_state.bump_review_round(cwd=str(repo), source="external")
    review_state.bump_review_round(cwd=str(repo), source="external", clean=True)  # reset
    for i, val in enumerate(("b = 1\n", "b = 2\n", "b = 3\n"), 1):
        _stage(repo, val)
        assert review_state.bump_review_round(cwd=str(repo), source="external") == i
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


def _answer_demand(repo: Path, home: Path, label: str) -> subprocess.CompletedProcess:
    """Answer the live gate demand by driving the REAL PostToolUse recorder hook.

    Once this gate has BLOCKED, `# escalation-ack` / `# audit-ack` no longer clear it
    on their own: an answer from the user has to be on record, because the sigil
    attests that a fresh decision was MADE and only the recorded answer attests that
    the USER made it. (A session this gate never blocked has no demand, so the sigil
    keeps its old meaning — which is why the other tests here are untouched.)

    Deliberately drives `ask_gate_demand.py --post` with a real AskUserQuestion
    payload rather than calling `review_state.record_gate_answer` in-process. Two
    reasons: an in-process write would land in the REAL ~/.genesis instead of the test
    HOME, and a hand-written intermediate would let the hook and the gate disagree
    about the payload shape without any test noticing.
    """
    question = _gate_demand_question(repo, home)
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "AskUserQuestion",
            "tool_response": {"answers": {question: label}},
            "session_id": "test",
        }
    )
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(_ASK_HOOK), "--post"],
        input=payload,
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _gate_demand_question(repo: Path, home: Path) -> str:
    """The question the gate actually declared, read back out of the round file.

    Read rather than hardcoded: a test that retypes the question would keep passing
    after the gate's wording changed, and the match is by exact containment.
    """
    env = {**os.environ, "HOME": str(home)}
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, json; sys.path.insert(0, sys.argv[1]);"
            "import review_state as rs;"
            "d = rs.read_gate_demand(sys.argv[2]);"
            "print(json.dumps(d['question'] if d else None))",
            str(_REPO_ROOT / "scripts"),
            str(repo),
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    question = json.loads(out.stdout)
    assert question, "no gate demand was declared — the block did not record one"
    return question


def _mark(
    repo: Path, home: Path, *, clean: bool = False, source: str = "external"
) -> subprocess.CompletedProcess:
    (home / ".genesis" / "last_code_review.txt").write_text("adversarial review: OK\n")
    env = {**os.environ, "HOME": str(home)}
    # The escalation streak is CROSS-MODEL only, so these gate/streak tests mark
    # `--source external` (the default here) — that is the ONLY kind that advances or
    # resets the counter. An external mark REQUIRES an outcome flag (--clean XOR
    # --defects). Pass source="internal" to exercise a non-counting same-model review.
    args = [
        sys.executable,
        str(_REVIEW_STATE),
        "mark",
        "--agent-output",
        str(home / ".genesis" / "last_code_review.txt"),
        "--source",
        source,
    ]
    if source == "external":
        args.append("--clean" if clean else "--defects")
    elif clean:
        # internal + clean is a valid (ignored-for-streak) combination worth exercising
        args.append("--clean")
    return subprocess.run(
        args,
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _mark_raw(repo: Path, home: Path, *extra: str) -> subprocess.CompletedProcess:
    """Run the `mark` CLI with an explicit arg list (no outcome flag injected).

    Used to exercise the required-outcome validation itself — neither/both flags.
    """
    (home / ".genesis" / "last_code_review.txt").write_text("adversarial review: OK\n")
    env = {**os.environ, "HOME": str(home)}
    args = [
        sys.executable,
        str(_REVIEW_STATE),
        "mark",
        "--agent-output",
        str(home / ".genesis" / "last_code_review.txt"),
        *extra,
    ]
    return subprocess.run(args, cwd=str(repo), env=env, capture_output=True, text=True, timeout=30)


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
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 1
    _git(repo, "commit", "-qm", "wip")  # staged area now clean
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 1  # no inflation


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
    # ...and an ack lets the docs commit through — once the user's choice is on
    # record. That block declared a demand, so from here the sigil alone no longer
    # clears it: the sigil attests a fresh decision was MADE, the recorded answer
    # attests the USER made it.
    _answer_demand(repo, home, "REDESIGN robust-by-construction")
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
    result = review_state.bump_review_round(cwd=str(repo), source="external")  # must not raise
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
    assert (
        review_state.bump_review_round(cwd=str(repo), source="external") == 1
    )  # treats as fresh, no crash


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
    # A well-formed CURRENT-era dict (has last_source) whose 'round' is a non-finite JSON
    # number (1e999 → inf) must be coerced to 0 at the load boundary — int(inf) OverflowError
    # escapes the plain int() guards and would crash the gate. Both the load boundary and
    # get_review_round must fail open to 0. (last_source present so this exercises coercion,
    # not the separate legacy-discard path for pre-source-axis files.)
    branch = review_state.get_current_branch(cwd=str(repo))
    _write_round_file(
        repo,
        '{"branch": "' + branch + '", "round": 1e999, "last_hash": "x", "last_source": "external"}',
    )
    assert review_state._load_round(cwd=str(repo))["round"] == 0
    assert review_state.get_review_round(cwd=str(repo)) == 0


def test_bump_no_crash_on_infinity_round(repo, _isolate_rounds):
    # A defect-bearing bump over an infinity 'round' (in a current-era file with last_source)
    # must not raise; it coerces the bad value to 0 and increments to 1.
    branch = review_state.get_current_branch(cwd=str(repo))
    _write_round_file(
        repo,
        '{"branch": "'
        + branch
        + '", "round": 1e999, "last_hash": "old", "last_source": "external"}',
    )
    _stage(repo, "a = 2\n")
    assert review_state.bump_review_round(cwd=str(repo), source="external") == 1


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


# ── Source axis: the streak is CROSS-MODEL only (--source external) ───────────
# The escalation streak exists to catch cross-model non-convergence — an external,
# non-Anthropic reviewer finding new defects round after round. Internal same-model
# self/subagent reviews are free and share the author's blind spots, so they NEVER
# count. --source (default internal) decides; an external mark still requires an
# outcome flag (it's the only kind that moves the counter). Supersedes feea3f71/#1446:
# its unconditional required-outcome only ever bit internal re-audits, which can no
# longer inflate the streak at all.


def test_internal_mark_needs_no_outcome_and_never_counts(repo, home):
    # A bare `mark` (default --source internal, no outcome flag) is a VALID review: the
    # marker is written (depth gate satisfied) but the round counter is never created —
    # a same-model review cannot inflate the cross-model streak, so no flag is needed.
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home)  # no --source, no outcome
    assert res.returncode == 0, res.stdout + res.stderr
    key = review_state._worktree_key(cwd=str(repo))
    assert (home / ".genesis" / "review_markers" / f"{key}.json").exists()
    assert not (home / ".genesis" / "review_rounds" / f"{key}.json").exists()


def test_external_mark_requires_explicit_outcome(repo, home):
    # An `--source external` mark WITHOUT an outcome flag is REFUSED (the outcome is what
    # moves the cross-model counter): no marker written, no round file, exit 1.
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--source", "external")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "outcome" in res.stderr.lower()
    key = review_state._worktree_key(cwd=str(repo))
    assert not (home / ".genesis" / "review_markers" / f"{key}.json").exists()
    assert not (home / ".genesis" / "review_rounds" / f"{key}.json").exists()


def test_mark_rejects_invalid_source(repo, home):
    # An unrecognized --source is refused (not silently coerced at the CLI boundary).
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--source", "anthropic", "--defects")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "source" in res.stderr.lower()
    key = review_state._worktree_key(cwd=str(repo))
    assert not (home / ".genesis" / "review_markers" / f"{key}.json").exists()


def test_mark_rejects_valueless_source(repo, home):
    # A valueless --source (the flag as the LAST token, no value) must be REFUSED —
    # NOT silently defaulted to internal, which would let an intended external mark be
    # miscounted as non-counting. Fail-closed: no marker written. (bind-atomically)
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--defects", "--source")  # --source has no value
    assert res.returncode == 1, res.stdout + res.stderr
    assert "source" in res.stderr.lower()
    key = review_state._worktree_key(cwd=str(repo))
    assert not (home / ".genesis" / "review_markers" / f"{key}.json").exists()
    assert not (home / ".genesis" / "review_rounds" / f"{key}.json").exists()


def test_source_equals_form_is_parsed(repo, home):
    # `--source=external` (the conventional equals form) must parse like `--source external`,
    # not be silently dropped to the internal default — else a cross-model review writes a
    # marker WITHOUT advancing the cap (miscounted internal). Also the equals form of an
    # invalid value is still rejected. (Codex P2 — dont_hand_roll_cli_parsing.)
    key = review_state._worktree_key(cwd=str(repo))
    rf = home / ".genesis" / "review_rounds" / f"{key}.json"
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--source=external", "--defects")
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads(rf.read_text())["round"] == 1  # counted as external, not dropped to internal
    _stage(repo, "a = 3\n")
    res2 = _mark_raw(repo, home, "--source=bogus", "--defects")
    assert res2.returncode == 1 and "source" in res2.stderr.lower()


def test_mark_fails_closed_on_unknown_option_typo(repo, home):
    # A typo'd flag (e.g. `--soruce`) must FAIL CLOSED, not be silently skipped — otherwise the
    # internal default stands and an intended external review is recorded internal, silently
    # skipping the escalation cap. No marker, no round file. (Codex P2 — fail-closed-on-unknown.)
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--soruce", "external", "--defects")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "unknown option" in res.stderr.lower()
    key = review_state._worktree_key(cwd=str(repo))
    assert not (home / ".genesis" / "review_markers" / f"{key}.json").exists()
    assert not (home / ".genesis" / "review_rounds" / f"{key}.json").exists()


def test_mark_rejects_contradictory_outcome(repo, home):
    # Passing BOTH --clean and --defects is a contradiction → refused, no marker
    # (independent of source).
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--source", "external", "--clean", "--defects")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "both" in res.stderr.lower()
    key = review_state._worktree_key(cwd=str(repo))
    assert not (home / ".genesis" / "review_markers" / f"{key}.json").exists()


def test_source_decides_counting(repo, home):
    # The SAME defect-bearing outcome counts iff it is external. An internal --defects
    # does NOT create/advance the round file; an external --defects advances to 1.
    key = review_state._worktree_key(cwd=str(repo))
    rf = home / ".genesis" / "review_rounds" / f"{key}.json"
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--defects")  # internal (default) — must not count
    assert res.returncode == 0, res.stderr
    assert not rf.exists()
    _stage(repo, "a = 3\n")
    res = _mark_raw(repo, home, "--source", "external", "--defects")  # external — counts
    assert res.returncode == 0, res.stderr
    assert json.loads(rf.read_text())["round"] == 1


def test_external_clean_resets_via_cli(repo, home):
    # An external clean round is the streak-reset path through the CLI.
    _reach_rounds(repo, home, 2)  # two external --defects rounds
    _stage(repo, "clean_now = 1\n")
    res = _mark_raw(repo, home, "--source", "external", "--clean")
    assert res.returncode == 0, res.stderr
    assert "streak reset" in res.stdout
    key = review_state._worktree_key(cwd=str(repo))
    rf = home / ".genesis" / "review_rounds" / f"{key}.json"
    assert json.loads(rf.read_text())["round"] == 0


def test_internal_clean_does_not_reset_external_streak(repo, home):
    # A self-review must NOT be able to reset a standing cross-model streak (that would
    # be a self-rubber-stamp reset). After two external rounds, an internal --clean
    # leaves the streak at 2.
    _reach_rounds(repo, home, 2)  # external streak == 2
    _stage(repo, "internal_look = 1\n")
    res = _mark_raw(repo, home, "--source", "internal", "--clean")
    assert res.returncode == 0, res.stderr
    key = review_state._worktree_key(cwd=str(repo))
    rf = home / ".genesis" / "review_rounds" / f"{key}.json"
    assert json.loads(rf.read_text())["round"] == 2


def test_external_defect_loop_still_reaches_cap(repo, home):
    # LOCKING: the source axis must NOT weaken the cap — three EXTERNAL defect rounds
    # still hard-block the commit at the escalation cap.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)  # 3 external rounds
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "escalation cap" in res.stderr


def test_acceptance_bar_incident_replay(repo, home):
    # THE ACCEPTANCE BAR — replays the 2026-09-01 false-block. The real sequence was:
    # external Codex wave-1 (defects), external Codex wave-2 (defects → mode-switch
    # fired CORRECTLY, forcing the audit that found F1/F2), then the MANDATED internal
    # fresh-context audit — which under the old model counted as round 3 and tripped
    # the HARD cap, penalizing the very remedy the gate demanded. New semantics: the
    # two external rounds reach 2 (mode-switch), the internal audit does NOT advance,
    # so the commit proceeds behind `# audit-ack` and the hard cap NEVER fires.
    _stage(repo, "codex_w1 = 1\n")
    _mark(repo, home)  # external round 1 (Codex wave-1)
    _stage(repo, "codex_w2 = 1\n")
    _mark(repo, home)  # external round 2 (Codex wave-2) → mode-switch territory
    _stage(repo, "audit_fix = 1\n")
    _mark(repo, home, source="internal")  # the mandated fresh-context audit — internal
    key = review_state._worktree_key(cwd=str(repo))
    rf = home / ".genesis" / "review_rounds" / f"{key}.json"
    assert json.loads(rf.read_text())["round"] == 2, "internal audit must not advance the streak"
    # Hard cap must NOT fire; the mode-switch audit-ack lets the commit proceed.
    res_blocked = _run_hook('git commit -m "wip"', repo, home)
    assert res_blocked.returncode == 2, res_blocked.stdout + res_blocked.stderr
    assert (
        "mode-switch" in res_blocked.stderr and "escalation cap reached" not in res_blocked.stderr
    )
    # The mode-switch block declared its own demand, so the audit-ack now needs the
    # user's choice on record too: (B) is "the premise holds — fix the CLASS", which
    # is exactly what this replay says happened.
    _answer_demand(repo, home, "(B) The premise holds — fix the CLASS, not the instance")
    res_ok = _run_hook('git commit -m "wip"  # audit-ack', repo, home)
    assert res_ok.returncode == 0, res_ok.stdout + res_ok.stderr


def test_marker_and_round_json_carry_source(repo, home):
    # Observability: the marker records the review source; the round file records the
    # last external source. An internal mark writes the marker but no round file.
    key = review_state._worktree_key(cwd=str(repo))
    marker = home / ".genesis" / "review_markers" / f"{key}.json"
    rf = home / ".genesis" / "review_rounds" / f"{key}.json"
    _stage(repo, "a = 2\n")
    _mark(repo, home, source="internal")
    assert json.loads(marker.read_text())["source"] == "internal"
    assert not rf.exists()
    _stage(repo, "a = 3\n")
    _mark(repo, home)  # external default
    assert json.loads(marker.read_text())["source"] == "external"
    assert json.loads(rf.read_text())["last_source"] == "external"


# ── Fix C: class-sweep reminder at round>=2 (369bbe0e) — advisory, never blocks ──


def test_class_sweep_reminder_at_round_two(repo, home):
    # On the SECOND defect-bearing round (you're looping), `mark` prints a class-sweep
    # reminder to stderr — advisory only (exit 0), fired at the decision moment.
    _reach_rounds(repo, home, 1)  # round 1 (no reminder)
    _stage(repo, "second = 1\n")
    res = _mark(repo, home)  # round 2
    assert res.returncode == 0, res.stderr
    assert "round 2" in res.stderr
    assert "class" in res.stderr.lower() and "sweep" in res.stderr.lower()


def test_no_reminder_at_round_one(repo, home):
    # The first defect-bearing round is not a loop yet → no reminder.
    _stage(repo, "first = 1\n")
    res = _mark(repo, home)  # round 1
    assert res.returncode == 0, res.stderr
    assert "sweep" not in res.stderr.lower()


def test_no_reminder_on_clean(repo, home):
    # A clean mark resets the streak to 0 → never a loop → no reminder, even after
    # prior defect-bearing rounds.
    _reach_rounds(repo, home, 2)  # two defect rounds
    _stage(repo, "clean_now = 1\n")
    res = _mark(repo, home, clean=True)
    assert res.returncode == 0, res.stderr
    assert "sweep" not in res.stderr.lower()


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
    assert review_state.bump_review_round(str(repo), source="external") == 1

    _fabricate_merge_sentinel(repo)

    _stage(repo, "base = 1\nfix_two = True\n")
    assert review_state.bump_review_round(str(repo), source="external") == 2, (
        "a forged merge sentinel suppressed a real defect-bearing round"
    )
    _stage(repo, "base = 1\nfix_three = True\n")
    assert review_state.bump_review_round(str(repo), source="external") == 3, (
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
    assert review_state.bump_review_round(str(repo), source="external") == 1

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

    assert review_state.bump_review_round(str(repo), source="external") == 2


# ── Round-7 terminal: the LIFETIME counter and its gate ───────────────────
#
# The consecutive streak alone has no terminal. `# escalation-ack` calls
# reset_review_round (review_enforcement_commit.py), so the cap is
# indefinitely repeatable: rounds 1-2-3, ack, 4-5-6, ack, 7-8-9, ack, forever.
# A PR can grind through fifteen external rounds and the gate never says
# "enough" — only "enough, for now". The lifetime counter is what closes that:
# acks never reset it, so round 7 forces a terminal decision (accept the
# remaining findings and merge, or abandon and start from a clean design).


def test_lifetime_survives_the_escalation_ack_reset(repo, _isolate_rounds):
    """THE POINT OF THE WHOLE FEATURE: reset_review_round must not wipe it.

    The consecutive counter going back to 0 is exactly what makes the existing
    cap repeatable; if lifetime reset with it, round 7 would be unreachable and
    this gate would be decorative.
    """
    for i, val in enumerate(("b = 1\n", "b = 2\n", "b = 3\n"), 1):
        _stage(repo, val)
        assert review_state.bump_review_round(cwd=str(repo), source="external") == i
    assert review_state.get_review_lifetime(cwd=str(repo)) == 3
    review_state.reset_review_round(cwd=str(repo))
    assert review_state.get_review_round(cwd=str(repo)) == 0, "streak must reset"
    assert review_state.get_review_lifetime(cwd=str(repo)) == 3, "lifetime must NOT reset"


def test_lifetime_accumulates_across_reset_cycles(repo, _isolate_rounds):
    """Seven rounds spread over the real ack cycle still reach seven."""
    for n, val in enumerate((f"b = {i}\n" for i in range(1, 8)), 1):
        _stage(repo, val)
        review_state.bump_review_round(cwd=str(repo), source="external")
        if n % review_state.ESCALATION_ROUND_CAP == 0:
            review_state.reset_review_round(cwd=str(repo))  # simulates # escalation-ack
    assert review_state.get_review_lifetime(cwd=str(repo)) == 7


def test_lifetime_not_advanced_by_internal_review(repo, _isolate_rounds):
    """Same rule as the streak: internal same-model audits are free.

    The round-2 mode-switch gate MANDATES an internal audit; counting it toward
    a terminal cap would penalise the remedy the gate itself demands (#1577).
    """
    for val in ("b = 1\n", "b = 2\n", "b = 3\n"):
        _stage(repo, val)
        review_state.bump_review_round(cwd=str(repo), source="internal")
    assert review_state.get_review_lifetime(cwd=str(repo)) == 0


def test_lifetime_resets_on_branch_change(repo, _isolate_rounds):
    """A different branch is a different change, so it gets a fresh budget."""
    _stage(repo, "b = 1\n")
    review_state.bump_review_round(cwd=str(repo), source="external")
    assert review_state.get_review_lifetime(cwd=str(repo)) == 1
    _git(repo, "checkout", "-q", "-b", "feature/y")
    assert review_state.get_review_lifetime(cwd=str(repo)) == 0


def test_lifetime_absent_in_legacy_file_reads_zero(repo, _isolate_rounds):
    """A counter written before this feature has no lifetime key.

    It must read 0 rather than raising — an in-flight branch gets a fresh
    budget, which errs toward LESS friction, the right direction for a new gate.
    """
    _stage(repo, "b = 1\n")
    review_state.bump_review_round(cwd=str(repo), source="external")
    rf = review_state._round_file(str(repo))
    state = json.loads(rf.read_text())
    del state["lifetime"]
    rf.write_text(json.dumps(state))
    assert review_state.get_review_lifetime(cwd=str(repo)) == 0


def _reach_lifetime(repo: Path, home: Path, n: int) -> None:
    """Drive the LIFETIME counter to n, resetting the streak as an ack would."""
    for i in range(1, n + 1):
        _stage(repo, f"life = {i}\n")
        m = _mark(repo, home)
        assert m.returncode == 0, m.stderr
        if i % review_state.ESCALATION_ROUND_CAP == 0:
            env = {**os.environ, "HOME": str(home)}
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"import sys; sys.path.insert(0, {str(_REPO_ROOT / 'scripts')!r}); "
                    f"import review_state as r; r.reset_review_round(cwd={str(repo)!r})",
                ],
                env=env,
                check=True,
                capture_output=True,
                timeout=30,
            )


def test_commit_blocked_at_final_round(repo, home):
    res_before = _run_hook('git commit -m "wip"', repo, home)
    assert res_before.returncode == 0, "control: an unmarked branch must not block"
    _reach_lifetime(repo, home, review_state.FINAL_ROUND_CAP)
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "final" in res.stderr.lower()


def test_escalation_ack_does_not_clear_the_final_round_block(repo, home):
    """The whole point: the repeatable sigil must stop working at the terminal."""
    _reach_lifetime(repo, home, review_state.FINAL_ROUND_CAP)
    res = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "final" in res.stderr.lower()


def test_final_round_accept_allows_the_commit(repo, home):
    """Carries its own negative control, because `returncode == 0` alone is vacuous.

    A bare assertion that the acked commit passes would ALSO pass if the terminal
    did not exist at all. The un-acked run in the same test is what makes the
    green mean something: same state, same command, one sigil apart.
    """
    _reach_lifetime(repo, home, review_state.FINAL_ROUND_CAP)
    without = _run_hook('git commit -m "wip"', repo, home)
    assert without.returncode == 2, "control: the terminal must be blocking here"
    res = _run_hook('git commit -m "wip"  # final-round-accept', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_final_round_accept_is_one_shot(repo, home):
    """Chosen deliberately over a latch: the decision must END the loop.

    An ack that kept working would be one more repeatable sigil, which is the
    defect this gate exists to close.
    """
    _reach_lifetime(repo, home, review_state.FINAL_ROUND_CAP)
    first = _run_hook('git commit -m "wip"  # final-round-accept', repo, home)
    assert first.returncode == 0, first.stderr
    again = _run_hook('git commit -m "another"', repo, home)
    assert again.returncode == 2, again.stdout + again.stderr
    assert "final" in again.stderr.lower()


def test_final_round_accept_cannot_be_reused(repo, home):
    """The sigil must be CONSUMED, not merely re-required on every commit.

    Its sibling above only retries WITHOUT the sigil, so it proves the block
    returns — not that the acceptance was spent. Re-applying the same sigil is the
    reuse path, and if it keeps working the terminal is just a fourth repeatable
    escape hatch, which is the whole defect this tier exists to close.
    """
    _reach_lifetime(repo, home, review_state.FINAL_ROUND_CAP)
    first = _run_hook('git commit -m "accept"  # final-round-accept', repo, home)
    assert first.returncode == 0, first.stderr
    reused = _run_hook('git commit -m "more fixes"  # final-round-accept', repo, home)
    assert reused.returncode == 2, reused.stdout + reused.stderr
    assert "already used" in reused.stderr.lower(), reused.stderr


def test_a_denied_command_does_not_spend_the_acceptance(repo, home):
    """The token must be spent at the ALLOW, never inside the tier that honours it.

    Rule 3a is checked FIRST, but four later rules can still deny the same command.
    Consuming inside the tier burned the one-shot token on a commit that never ran,
    and since the consuming tier is checked first on the retry, the branch became
    permanently uncommittable. MEASURED before the fix, at streak 7 / lifetime 7 —
    a state this gate's own comment calls reachable — every sigil combination
    returned 2, including the one the block message itself prints.
    """
    _reach_rounds(repo, home, review_state.FINAL_ROUND_CAP)
    # Terminal cleared, but the escalation cap still denies: both sigils are
    # genuinely required in this state, so this command does NOT commit.
    denied = _run_hook('git commit -m "accept"  # final-round-accept', repo, home)
    assert denied.returncode == 2, denied.stdout + denied.stderr
    assert "already used" not in denied.stderr.lower()
    # That denial came from the escalation cap, so it declared a demand. Record the
    # user's choice before the co-required form, or the cap refuses the ack for the
    # separate reason that nobody was ever asked.
    _answer_demand(repo, home, "NARROW the scope")
    # The acceptance must still be available to the co-required form.
    ok = _run_hook('git commit -m "accept"  # final-round-accept escalation-ack', repo, home)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # ...and only NOW is it spent.
    after = _run_hook('git commit -m "more"  # final-round-accept escalation-ack', repo, home)
    assert after.returncode == 2, after.stdout + after.stderr
    assert "already used" in after.stderr.lower(), after.stderr


def test_consumption_survives_a_later_external_round(repo, home):
    """A new review round after the accept must not hand back a fresh acceptance.

    `bump_review_round` rebuilds the state dict on every write, so a field that is
    not explicitly carried is silently dropped — the exact way `last_source` was
    lost once already. If the flag vanished here, running one more Codex round
    would reopen the terminal, which is precisely the loop being ended.
    """
    _reach_lifetime(repo, home, review_state.FINAL_ROUND_CAP)
    assert _run_hook('git commit -m "accept"  # final-round-accept', repo, home).returncode == 0
    _stage(repo, "after = 1\n")
    m = _mark(repo, home)
    assert m.returncode == 0, m.stderr
    again = _run_hook('git commit -m "post-round"  # final-round-accept', repo, home)
    # Assert the REASON, not just the code. A bare `returncode == 2` is satisfied by
    # any of the sibling rules that can also block here, so it stays green even when
    # the carry-through is deleted — measured, this exact test passed under that
    # mutation until the message check was added.
    assert again.returncode == 2, again.stdout + again.stderr
    assert "already used" in again.stderr.lower(), again.stderr


def test_a_new_branch_gets_a_fresh_acceptance(repo, home):
    """Consumption is scoped to the change, not the machine.

    Without this the flag would be a global latch that permanently disarms the
    sigil for every future branch on the install.
    """
    _reach_lifetime(repo, home, review_state.FINAL_ROUND_CAP)
    assert _run_hook('git commit -m "accept"  # final-round-accept', repo, home).returncode == 0
    subprocess.run(
        ["git", "checkout", "-b", "a-different-change"], cwd=repo, check=True, capture_output=True
    )
    # ARM THE TERMINAL ON THE NEW BRANCH. Without this the test never reaches
    # get_final_accept_consumed at all — a fresh branch has lifetime 0, so the tier
    # is skipped entirely and the assertion passes on a GLOBAL latch too. Measured:
    # deleting the branch check from that accessor left this test green.
    _reach_lifetime(repo, home, review_state.FINAL_ROUND_CAP)
    res = _run_hook('git commit -m "fresh start"  # final-round-accept', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "already used" not in res.stderr.lower(), res.stderr


def test_final_ack_inside_message_does_not_bypass(repo, home):
    """A token buried in -m is not a trailing comment (mirrors the sibling acks)."""
    _reach_lifetime(repo, home, review_state.FINAL_ROUND_CAP)
    res = _run_hook('git commit -m "wip # final-round-accept"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr


def test_below_final_round_the_normal_cycle_still_applies(repo, home):
    """Guard the guard: below the terminal the ORDINARY escalation ack still works.

    The first version of this test was VACUOUS and it is worth saying why.
    It drove the counter with `_reach_lifetime`, which resets the streak on every
    third round — so at n=3 it reset on its own last iteration and left the streak
    at 1, where NOTHING gates. The commit passed because no rule fired, not because
    the ack worked; deleting the sigil it names left it green. `_reach_rounds` does
    not reset, and the un-acked control is what makes the pass mean something.
    """
    # NB: the counter is NOT readable in-process here. _ROUND_DIR resolves against
    # the real $HOME at import time and only the unit tests monkeypatch it, so an
    # in-process get_review_* in an integration test reads someone else's state —
    # 0, which made an earlier `< FINAL_ROUND_CAP` assertion pass vacuously. State
    # is asserted through the GATE'S BEHAVIOUR instead, which is what matters.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)  # streak 3, no reset
    without = _run_hook('git commit -m "wip"', repo, home)
    assert without.returncode == 2, "control: the escalation cap must be blocking here"
    assert "escalation cap" in without.stderr.lower()
    # The control block above declared a demand, so the ordinary cycle now runs
    # block -> the user is asked -> ack, rather than block -> ack.
    _answer_demand(repo, home, "NARROW the scope")
    res = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def _refund_final_accept(repo: Path, home: Path) -> None:
    """Clear the spent-acceptance flag so one test can exercise two acked commits.

    Only for tests whose subject is something OTHER than consumption (sigil parse
    order, message content). Consumption itself is covered by
    test_final_round_accept_cannot_be_reused and friends, which must never call this.
    """
    env = {**os.environ, "HOME": str(home)}
    subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys, json; sys.path.insert(0, {str(_REPO_ROOT / 'scripts')!r}); "
            f"import review_state as r; st = r._load_round({str(repo)!r}); "
            # Refuse to write back an empty counter: that would silently disarm the
            # terminal and turn the caller GREEN while testing nothing.
            "assert st.get('lifetime'), f'refund would blank the counter: {st!r}'; "
            "st.pop('final_accept_consumed', None); "
            f"r._write_round(st, {str(repo)!r})",
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )


def test_terminal_and_cap_together_accept_either_sigil_order(repo, home):
    """streak>=3 AND lifetime>=7 is reachable, and BOTH sigils are then required.

    This is the state the unregistered-sigil bug deadlocked in ONE token order:
    an unlisted token reads as prose and ends the leading sigil run, so everything
    written behind it is ignored. Order must not decide the verdict, and the
    terminal's own message must not print the losing order.
    """
    # _reach_rounds never resets, so 7 marks leave streak AND lifetime at 7 — the
    # state is asserted through behaviour below, not by reading the counter (see
    # the note in test_below_final_round_the_normal_cycle_still_applies).
    _reach_rounds(repo, home, review_state.FINAL_ROUND_CAP)
    blocked = _run_hook('git commit -m "wip"', repo, home)
    assert blocked.returncode == 2, "control: both tiers must be live here"
    assert "FINAL ROUND" in blocked.stderr, "the terminal must be the one blocking"
    for cmd in (
        'git commit -m "wip"  # final-round-accept escalation-ack',
        'git commit -m "wip"  # escalation-ack final-round-accept',
    ):
        # The first accepted commit SPENDS the acceptance, so without this the
        # second order would be blocked by consumption rather than by parsing —
        # a green/red that says nothing about the property under test.
        _refund_final_accept(repo, home)
        res = _run_hook(cmd, repo, home)
        assert res.returncode == 0, f"{cmd} -> {res.returncode}: {res.stderr}"


def test_terminal_message_names_the_co_required_sigil(repo, home):
    """A block that prints an insufficient command teaches an unescapable loop."""
    _reach_rounds(repo, home, review_state.FINAL_ROUND_CAP)
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 2
    assert "final-round-accept escalation-ack" in res.stderr


# ── The two-path disposition doctrine, in the block MESSAGES ──────────────
#
# A gate's enumerated remedies ARE the option set a session picks from — it does
# not invent a menu of its own. So a remedy the messages never name is a remedy
# nobody takes, and before this every option at both tiers PRESERVED the change
# (audit harder / redesign / narrow / shelve). Handing it back to a builder
# session — the only remedy that helps when the PREMISE is what is wrong — was
# reachable from neither message. These tests pin the option SET and its ORDER,
# which is the part a later edit can silently drop; the prose around it is free
# to change.


def _stderr_at_round(repo, home, n: int) -> str:
    """Block stderr with the counter standing at exactly ``n`` rounds.

    The blocked commit never lands, so the counter is unchanged afterwards and
    a caller can advance to the next tier and read again in the same test.
    """
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 2, f"round {n} did not block: {res.stdout + res.stderr}"
    return res.stderr


def test_mode_switch_offers_handing_back_as_a_named_alternative(repo, home):
    """Round 2 must present BOTH remedies, because they are opposites.

    Fixing the whole class is right when the premise holds. When it does not,
    no further round can help — and a message that only says "audit harder"
    sends the author back into the loop it is trying to end.
    """
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)
    err = _stderr_at_round(repo, home, review_state.ESCALATION_ROUND_CAP - 1)
    assert "PREMISE is wrong" in err
    assert "BUILDER session" in err
    # …and the class-level audit must SURVIVE alongside it: this is a fork, not
    # a replacement. A message offering only hand-back would send every
    # ordinary two-round change back to a builder.
    assert "CLASS" in err
    assert "FRESH-CONTEXT adversarial reviewer" in err


def test_mode_switch_states_the_evidence_bar_for_handing_back(repo, home):
    """Hand-back is the MINORITY case and must read that way.

    The failure mode guarded here is the gate handing back a change that only
    needed polish — which costs more than the extra round, and costs most of
    all when the gate turns out to have been the thing that was wrong.
    """
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)
    err = _stderr_at_round(repo, home, review_state.ESCALATION_ROUND_CAP - 1)
    # The wrong-shape signals — which is what "evidence" means at this tier.
    assert "CONCENTRATING" in err
    assert "GROWING" in err
    # TWO OR MORE signals, not one. The review mandate sets that bar and this
    # message must not undercut it: one signal alone is an ordinary local defect
    # wearing an architectural shape — findings concentrate in any large parser.
    # An earlier version of this message listed the signals with `or` and a bare
    # "absent those" default, which classified a single signal as (A).
    assert re.search(r"TWO OR MORE", err), err
    # …and the explicit default short of that bar. Matched loosely on punctuation
    # so an ordinary prose edit does not break a structural claim.
    assert re.search(r"Short of two[,\s]+it is \(B\)", err), err


def test_the_premise_check_DOC_states_the_same_two_signal_bar():
    """The gate message and the doc it points at must set the SAME bar.

    This is the lock on a two-instance class whose second instance a reviewer
    found, not this suite: the message above was corrected to TWO OR MORE, and
    `.claude/docs/premise-check.md` — the document that message directs the
    reader to — was left stating the one-signal version. A doc that sets a lower
    bar than the gate is the easier surface to read, so it wins in practice.

    Asserted on the DOC rather than by diffing the two texts: they are written
    for different readers and should not be byte-identical. What must not drift
    is the bar itself and the fallback short of it.
    """
    doc = (
        Path(__file__).resolve().parents[2] / ".claude" / "docs" / "premise-check.md"
    ).read_text()
    assert "TWO OR MORE" in doc, "the doc must state the same evidence bar as the gate message"
    assert re.search(r"[Ss]hort of\s+two", doc), (
        "the doc must state the fallback short of that bar, or a single signal "
        "silently reads as sufficient for a hand-back"
    )
    assert "class-level audit" in doc, "…and name what that fallback IS"


def test_escalation_cap_names_handing_back_FIRST(repo, home):
    """At the cap, option (a) must be hand-back — not another way to keep going.

    Three rounds each finding something NEW after a class-level audit is the
    strongest evidence available that the premise is wrong. A menu whose first
    entry preserves the change reads as "try harder" at precisely the moment
    that is the wrong instruction.
    """
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    err = _stderr_at_round(repo, home, review_state.ESCALATION_ROUND_CAP)
    hand_back = err.find("HAND IT BACK")
    redesign = err.find("(b) REDESIGN")
    shelve = err.find("(d) SHELVE")
    assert hand_back != -1, "the cap message must name handing it back"
    assert redesign != -1 and shelve != -1, "the other options must survive"
    assert hand_back < redesign < shelve, (
        "hand-back must be the FIRST option at the cap — a session takes the "
        f"menu in the order the gate prints it (got {hand_back}, {redesign}, "
        f"{shelve})"
    )


def test_the_cap_gives_hand_back_no_sigil_exit(repo, home):
    """Option (a) must NOT be told to rerun the commit with `# escalation-ack`.

    At the cap the index still holds the design just judged premise-broken and
    its review marker is current, so that sigil would permit exactly that commit
    AND reset the streak — landing the work and erasing the evidence that
    stopped it. The same conditional-exit defect was fixed at the round-2 tier
    and left here, which is why this is pinned separately rather than trusted to
    the neighbouring test.
    """
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    err = _stderr_at_round(repo, home, review_state.ESCALATION_ROUND_CAP)
    assert "THE EXIT DEPENDS ON WHICH OPTION" in err, err
    assert "do NOT run that command" in err, err
    # The sigil must still be offered to the options that legitimately continue.
    assert "escalation-ack" in err
    # Guard the guard: the hand-back warning must sit AFTER the sigil it warns
    # about, or a reader takes the command and never reaches the caveat.
    assert err.index("escalation-ack") < err.index("do NOT run that command")


def test_both_tiers_route_the_fork_to_evidence_not_feel(repo, home):
    """Neither tier may leave premise-or-polish to gut feeling, and both must
    say the discarded work was not wasted.

    Sunk cost is the force that keeps a wrong-shaped change in review: a
    session reading hand-back as failure patches one more time instead, which
    is the loop both tiers exist to end. Checked at BOTH tiers in one test —
    the counter survives a blocked commit, so round 2 is read, then advanced.
    """
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)
    mode_switch = _stderr_at_round(repo, home, review_state.ESCALATION_ROUND_CAP - 1)
    # Advance one distinct defect-bearing round into the hard cap.
    _stage(repo, "cap = 1\n")
    assert _mark(repo, home).returncode == 0
    cap = _stderr_at_round(repo, home, review_state.ESCALATION_ROUND_CAP)
    # Guard the guard: two DIFFERENT tiers were read, not the same one twice.
    # Anchor on the mode-switch message's OPENING PREFIX, not the bare word:
    # the cap message also contains "mode-switch" (in "the round-2 mode-switch
    # audit did NOT converge"), so a bare-substring guard passes when BOTH
    # reads land on the cap — and since every other assertion here holds in
    # both messages, the whole test would pass on a doubled cap read. Measured
    # by mutation during this PR's own pre-push review.
    assert "BLOCKED (mode-switch)" in mode_switch
    assert "BLOCKED (mode-switch)" not in cap, "the two reads collapsed onto one tier"
    assert "escalation cap" in cap
    for tier, err in (("mode-switch", mode_switch), ("cap", cap)):
        assert ".claude/docs/premise-check.md" in err, f"no method named at {tier}"
        assert "bought the understanding" in err, f"no sunk-cost framing at {tier}"
    # …and the doc both tiers send the reader to must actually exist. Without
    # this, renaming or moving it rots all four references while every test
    # above stays green — they assert on the STRING, not the file.
    assert (_REPO_ROOT / ".claude" / "docs" / "premise-check.md").is_file(), (
        "both gate tiers cite .claude/docs/premise-check.md — it is missing"
    )


# ─── Gate demands: the substitution contract, at the gate ────────────────────
#
# These are the locks for the ENFORCEMENT itself. The four tests above that call
# `_answer_demand` would pass whether or not the gate checks the demand — they
# answer it either way — so without these, deleting `_demand_refusal` would leave
# the whole suite green. That gap was found while planning the verify-RED sweep,
# which is exactly what that sweep is for.


def test_a_cap_block_declares_its_remedy_set(repo, home):
    """The block is the ONLY place a cap demand is created. Enforcement binds
    after a real refusal, never speculatively."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    res = _run_hook('git commit -m "wip"', repo, home)
    assert res.returncode == 2
    question = _gate_demand_question(repo, home)  # asserts a demand exists
    assert "escalation cap" in question.lower()


def test_a_bare_ack_is_REFUSED_once_the_gate_has_actually_blocked(repo, home):
    """THE ENFORCEMENT LOCK, and the founding incident in one test.

    The gate blocked and printed its remedies; the sigil attests that a fresh
    decision was MADE. Only a recorded answer attests that the USER made it. On
    2026-08-31 the relay dropped the first option, invented a fourth and added
    "ship as-is" — and a bare ack would have landed that.
    """
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    blocked = _run_hook('git commit -m "wip"', repo, home)
    assert blocked.returncode == 2, "control: the cap must block here"
    res = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "not enough on its own" in res.stderr
    assert "LIVE and UNANSWERED" in res.stderr


def test_a_session_the_gate_never_blocked_keeps_the_old_sigil_meaning(repo, home):
    """The scope limit, stated as a test. No block -> no demand -> the sigil works
    exactly as before. This is what keeps the change off ~11 other tests, and it
    is also the PRE-EXISTING hole (a preemptive sigil is never demanded) that is
    deliberately out of scope here and filed as an issue."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    res = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_a_TERMINAL_choice_refuses_the_commit_even_with_the_sigil(repo, home):
    """Choosing hand-back blocks the branch, and the sigil cannot override it.
    That is the decision working, not a malfunction."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    assert _run_hook('git commit -m "wip"', repo, home).returncode == 2
    _answer_demand(repo, home, "HAND IT BACK")
    res = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "TERMINAL remedy" in res.stderr
    assert "retire-gate-demand" in res.stderr, "the documented re-ask must be named"


def test_an_UNRECOGNISED_answer_is_quoted_back_not_ignored(repo, home):
    """The operator answered. Telling them they were never asked would read as the
    gate being broken, which is why free text records a state instead of nothing."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    assert _run_hook('git commit -m "wip"', repo, home).returncode == 2
    _answer_demand(repo, home, "narrow it but keep the tests")
    res = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "narrow it but keep the tests" in res.stderr, "the answer must be quoted back"


def test_an_answer_authorises_exactly_ONE_commit(repo, home):
    """One-shot: the decision authorises the commit it was made about, not the
    branch's remaining life."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    assert _run_hook('git commit -m "wip"', repo, home).returncode == 2
    _answer_demand(repo, home, "NARROW the scope")
    first = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert first.returncode == 0, first.stdout + first.stderr
    _git(repo, "commit", "-qm", "wip")
    # Drive the streak back to the cap; the spent demand must not authorise again.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    assert _run_hook('git commit -m "again"', repo, home).returncode == 2
    again = _run_hook('git commit -m "again"  # escalation-ack', repo, home)
    assert again.returncode == 2, again.stdout + again.stderr


def test_the_mode_switch_tier_enforces_the_same_contract(repo, home):
    """Both tiers, per the owner's decision — one declaration API, two data sets."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)
    blocked = _run_hook('git commit -m "wip"', repo, home)
    assert blocked.returncode == 2 and "mode-switch" in blocked.stderr
    bare = _run_hook('git commit -m "wip"  # audit-ack', repo, home)
    assert bare.returncode == 2, bare.stdout + bare.stderr
    assert "not enough on its own" in bare.stderr
    _answer_demand(repo, home, "(B) The premise holds — fix the CLASS, not the instance")
    ok = _run_hook('git commit -m "wip"  # audit-ack', repo, home)
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_a_background_session_is_told_how_to_EXIT_not_just_that_it_is_stuck(repo, home):
    """A dispatched session cannot be asked, so it cannot clear either tier — the
    gate's own text already says the decision is not the session's to make alone.
    That is a deliberate capability reduction, so the block must name the exit
    (hand back: label + a `ready` follow-up) rather than leaving it to grind.
    """
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "wip"'},
            "session_id": "test",
        }
    )
    env = {**os.environ, "HOME": str(home), "GENESIS_CC_SESSION": "1"}
    res = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 2
    assert "NO HUMAN IS PRESENT" in res.stderr
    assert "needs-architecture-session" in res.stderr
    assert "ready" in res.stderr and "follow-up" in res.stderr


def test_the_kill_switch_restores_the_pre_demand_behaviour(repo, home):
    """A RECOVERY tool for a wedged gate. It disables ENFORCEMENT, not just the
    append — disabling only the append would leave the demand unsatisfiable, which
    is the opposite of a recovery tool."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    assert _run_hook('git commit -m "wip"', repo, home).returncode == 2
    blocked = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert blocked.returncode == 2, "control: without the switch the ack is refused"
    marker = home / ".genesis" / "config" / "gate_ack_disabled"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    res = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
