"""Commit-side enforcement of the shared distinct-reviewed-head budget."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "review_enforcement_commit.py"
STATE = ROOT / "scripts" / "review_state.py"
CODEX = "chatgpt-codex-connector[bot]"
HEADS = tuple(f"{i:x}" * 40 for i in range(1, 7))

# The hook imports `review_deadline` and `review_scope` at MODULE scope, resolving
# them from its own `scripts/` when run as a subprocess (sys.path[0] is the script's
# directory). Loading it by spec instead gives it pytest's sys.path, where neither
# resolves — so without this insert the module raises ModuleNotFoundError AT
# COLLECTION. It collected anyway in a whole-directory run only because 32-33 sibling
# modules do the same insert at import time (AST-resolved count; a grep for the
# literal idiom undercounts, since most go through a variable) and most sort earlier.
# That meant the file could NOT be run TARGETED — `pytest tests/test_hooks/test_x.py`,
# the form this project tells every session to use — which is how a suite silently
# stops running. Explicit here, like its siblings, so importability does not depend
# on collection order.
#
# `scripts/` only, deliberately: the hook self-inserts its own `hooks/` dir at module
# scope (review_enforcement_commit.py:26), which runs during exec_module below, so a
# `hooks` insert here would be dead. MEASURED — with `scripts/` alone and the hooks
# dir asserted absent, the module imports and `review_deadline` resolves from THIS
# worktree. Keep it ROOT-relative: hardcoding an absolute main-checkout path makes
# replay_guard_corpus.py raise SystemExit over a cross-tree bare-name import, in a
# different test directory, with an error naming neither file.
sys.path.insert(0, str(ROOT / "scripts"))

import review_budget  # noqa: E402 — needs the insert above.

#: Derived, never spelled. The gate messages interpolate these, so hardcoding "four"
#: or "two" in an assertion would relocate into the tests exactly the prose-vs-constant
#: drift this change removes from the code.
_HEAD_LIMIT = review_budget.STANDING_REVIEWED_HEAD_LIMIT
_GATE_LIMIT = review_budget.GATE_DISCOVERY_ROUND_LIMIT

_spec = importlib.util.spec_from_file_location("commit_budget_guard", HOOK)
_guard = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_guard)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "-c", "init.defaultBranch=main", "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "tester")
    (path / "f.py").write_text("value = 1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    _git(path, "checkout", "-qb", "feature/review-budget")
    (path / "f.py").write_text("value = 2\n")
    _git(path, "add", "-A")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / ".genesis").mkdir(parents=True)
    return path


def _jsonl(rows) -> str:
    return "\n".join(json.dumps(row) for row in rows)


def _evidence(monkeypatch, reviewed: int, *, gate=False, head=HEADS[4]):
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_PR", "99")
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_REPO", "owner/repo")
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", head)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", _jsonl({"sha": sha} for sha in HEADS))
    monkeypatch.setenv(
        "_TEST_GH_CODEX_REVIEWS",
        _jsonl(
            {"login": CODEX, "commit_id": sha, "state": "COMMENTED"} for sha in HEADS[:reviewed]
        ),
    )
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
    filename = "scripts/review_budget.py" if gate else "src/example.py"
    monkeypatch.setenv(
        "_TEST_REVIEW_BUDGET_FILES",
        json.dumps({"filename": filename, "previous_filename": None}),
    )


def _mark(repo: Path, home: Path) -> None:
    evidence = home / "review.txt"
    evidence.write_text("adversarial review complete\n" + "specific evidence " * 30)
    env = {**os.environ, "HOME": str(home)}
    result = subprocess.run(
        [
            sys.executable,
            str(STATE),
            "mark",
            "--agent-output",
            str(evidence),
            "--source",
            "internal",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run(command: str, repo: Path, home: Path, *, dispatched=False):
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(repo),
            "tool_input": {"command": command},
        }
    )
    env = {**os.environ, "HOME": str(home)}
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    else:
        env.pop("GENESIS_CC_SESSION", None)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _decision(result) -> str:
    if result.returncode == 2:
        return "deny"
    if result.stdout.strip():
        payload = json.loads(result.stdout)
        if payload.get("hookSpecificOutput", {}).get("permissionDecision") == "ask":
            return "ask"
    return "allow"


def test_current_branch_status_resolves_cross_repository_pr_identity():
    raw = json.dumps(
        {
            "currentBranch": {
                "number": 42,
                "state": "OPEN",
                "url": "https://github.example/target/project/pull/42",
            }
        }
    )
    assert _guard._current_branch_pr_identity(raw) == ("target/project", 42)


def test_current_branch_status_distinguishes_no_pr_from_malformed_evidence():
    assert _guard._current_branch_pr_identity('{"createdBy": []}') is None
    malformed = _guard._current_branch_pr_identity(
        '{"currentBranch": {"number": 42, "state": "OPEN", "url": "bad"}}'
    )
    assert isinstance(malformed, dict)
    assert malformed["status"] == "unknown"


def test_cloud_lookup_reserves_two_seconds_for_post_lookup_checks(monkeypatch, repo):
    now = [100.0]
    outer = _guard.Deadline.after(9.5, monotonic=lambda: now[0])
    now[0] += 3.0  # Earlier mandatory local probes consume part of the hook budget.
    calls = []

    def consumes_timeout(*args, timeout, **kwargs):
        calls.append(timeout)
        now[0] += timeout
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "currentBranch": {
                        "number": 99,
                        "state": "OPEN",
                        "url": "https://github.com/owner/repo/pull/99",
                    }
                }
            ),
        )

    monkeypatch.setattr(_guard.subprocess, "run", consumes_timeout)
    result = _guard._branch_review_budget(str(repo), "feature/review-budget", deadline=outer)

    assert result["status"] == "unknown"
    assert calls == [4.5]
    assert outer.remaining() == 2.0


def test_ordinary_four_heads_asks_for_each_commit(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    # Four reviewed heads is also past the round-3 HARD STOP, which is now keyed
    # on reviewed heads; its ack is recorded at 4, so the second commit needs no
    # sigil — but the native approval is per commit and never carries forward.
    first = _run('git commit -m "fix"  # escalation-ack', repo, home)
    second = _run('git commit -m "fix"', repo, home)
    assert _decision(first) == _decision(second) == "ask"
    assert f"standing authorization ended after {_HEAD_LIMIT}" in first.stdout


def test_legacy_final_sigil_does_not_replace_native_approval(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    result = _run('git commit -m "fix"  # escalation-ack final-round-accept', repo, home)
    assert _decision(result) == "ask"


def test_round_six_commit_is_strongly_discouraged(monkeypatch, repo, home):
    _evidence(monkeypatch, 5, head=HEADS[5])
    _mark(repo, home)
    result = _run('git commit -m "more"  # escalation-ack', repo, home)
    assert _decision(result) == "ask"
    assert "strongly discouraged" in result.stdout


def test_autonomous_commit_is_denied(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    result = _run('git commit -m "fix"  # escalation-ack', repo, home, dispatched=True)
    assert _decision(result) == "deny"
    assert f"standing authorization ended after {_HEAD_LIMIT}" in result.stderr


def test_hard_checks_precede_pending_approval(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    result = _run('git commit --no-verify -m "fix"', repo, home)
    assert _decision(result) == "deny"
    assert "--no-verify" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "one" && git commit --amend -m "two"',
        'git commit -m "one" && git push origin HEAD',
        'git commit -m "one" && gh pr comment 99 --body "@codex review"',
    ],
)
def test_one_approval_cannot_cover_compound_actions(monkeypatch, repo, home, command):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    result = _run(command, repo, home)
    assert _decision(result) == "deny"
    assert "exactly one fix commit" in result.stderr


def test_docs_skip_and_review_override_still_pass_through_approval(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _git(repo, "reset", "-q", "HEAD", "--", "f.py")
    (repo / "note.md").write_text("documentation\n")
    _git(repo, "add", "note.md")
    docs = _run('git commit -m "docs"  # escalation-ack', repo, home)
    assert _decision(docs) == "ask"

    _git(repo, "reset", "-q", "HEAD", "--", "note.md")
    _git(repo, "add", "f.py")
    override = _run('git commit -m "fix"  # review-override', repo, home)
    assert _decision(override) == "ask"


def test_unknown_budget_asks_foreground_and_denies_autonomous(monkeypatch, repo, home):
    _evidence(monkeypatch, 0)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "bad-json")
    _mark(repo, home)
    assert _decision(_run('git commit -m "fix"', repo, home)) == "ask"
    assert _decision(_run('git commit -m "fix"', repo, home, dispatched=True)) == "deny"


def test_gate_round_two_fix_is_allowed_but_post_confirmation_fix_asks(monkeypatch, repo, home):
    _evidence(monkeypatch, 2, gate=True, head=HEADS[1])
    _mark(repo, home)
    # Two reviewed heads is the round-2 MODE-SWITCH tier, now keyed on reviewed
    # heads; the budget still permits the fix once the class audit is attested.
    assert _decision(_run('git commit -m "round two fix"  # audit-ack', repo, home)) == "allow"

    _evidence(monkeypatch, 3, gate=True, head=HEADS[2])
    after = _run('git commit -m "post confirmation fix"  # escalation-ack', repo, home)
    assert _decision(after) == "ask"
    assert f"Its {_GATE_LIMIT} discovery rounds plus confirmation are spent" in after.stdout


@pytest.mark.parametrize(
    ("reviewed", "gate", "head", "boundary_claim"),
    [
        # Only the branch where the count IS the ordinary limit may assert the
        # four-head boundary. At five heads that sentence is false, and at the gate
        # limit it is about a different lane entirely.
        pytest.param(4, False, HEADS[4], True, id="ordinary-at-the-terminal-boundary"),
        pytest.param(5, False, HEADS[5], False, id="ordinary-past-it-discouraged"),
        pytest.param(3, True, HEADS[2], False, id="gate-lane-budget-spent"),
    ],
)
def test_every_fix_commit_approval_states_the_terminal_decision(
    monkeypatch, repo, home, reviewed, gate, head, boundary_claim
):
    """A fix commit is protected like a review request, so it owes the same rule.

    All three branches of `_commit_budget_reason` shipped with NO terminal framing
    while the push guard's equivalent had it — the owner reading this prompt was
    told only to "approve this single fix commit", which frames continued round-5
    work as routine at the surface where the decision is actually made. Caught by
    external review on PR #2382, not by that PR's own audit.

    Parametrized across all three branches deliberately. The existing tests above
    pin the SURROUNDING sentences ("standing authorization ended after <limit>",
    "strongly discouraged", "two discovery rounds plus confirmation are spent"),
    every one of which survives if only the terminal framing is deleted — so
    without this test that mutation leaves the file GREEN.

    The NEGATIVE assertions carry as much weight as the positive one, and each was
    a real defect in the first version of this fix:

    * This gate authorizes a fix COMMIT, so no branch may tell the reader their
      approval buys "one further round" — the shared notice's round clause was
      pasted here and contradicted the very next sentence.
    * The four-head boundary claim belongs ONLY in the branch where the count is
      four. The discouraged branch fires at five, where "there is no ordinary round
      5" is simply false, and the gate lane is a different ladder.
    """
    _evidence(monkeypatch, reviewed, gate=gate, head=head)
    _mark(repo, home)
    result = _run('git commit -m "fix"  # escalation-ack', repo, home)
    assert _decision(result) == "ask"
    assert "MERGE with the outstanding issues accepted and filed" in result.stdout
    assert "SEND IT BACK for rework" in result.stdout
    # A commit gate never authorizes a round, in ANY branch.
    assert "one further round" not in result.stdout, (
        "this approval covers a fix commit, not a review round"
    )
    if boundary_claim:
        assert "ROUND 4 IS TERMINAL" in result.stdout
        assert "there is no ordinary round 5" in result.stdout
    else:
        assert "there is no ordinary round 5" not in result.stdout, (
            "the four-head boundary claim is false in this branch"
        )
        if gate:
            assert "review-gate surface" in result.stdout
        else:
            assert "past the terminal boundary" in result.stdout


def test_proven_no_open_pr_does_not_invent_a_cloud_round(monkeypatch, repo, home):
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_PR", "none")
    _mark(repo, home)
    assert _decision(_run('git commit -m "local branch"', repo, home)) == "allow"


# ── Round prescription keyed on reviewed heads ──────────────────────────────
#
# The round-2 MODE-SWITCH and round-3 HARD STOP used to read ONLY the local
# defect-bearing streak, which moves solely on a self-reported external mark.
# The ordinary fix workflow marks an INTERNAL audit, which never moves it, so a
# PR could accumulate reviewed heads with no round file at all and neither tier
# ever fired. The effective round is now max(local streak, reviewed heads), with
# acks recorded against the reviewed-head count they acknowledge.


def _cap() -> int:
    sys.path.insert(0, str(ROOT / "scripts"))
    import review_state  # noqa: PLC0415

    return review_state.ESCALATION_ROUND_CAP


def _ack_file(repo: Path, home: Path) -> Path:
    import hashlib  # noqa: PLC0415

    key = hashlib.sha256(os.path.realpath(repo).encode()).hexdigest()[:12]
    return home / ".genesis" / "review_rounds" / f"{key}.head_acks.json"


def _round_file(repo: Path, home: Path) -> Path:
    return _ack_file(repo, home).with_name(_ack_file(repo, home).name.replace(".head_acks", ""))


def test_mode_switch_fires_on_reviewed_heads_with_no_local_streak(monkeypatch, repo, home):
    """(a) Local streak 0 (no round file) + CAP-1 reviewed heads → MODE-SWITCH."""
    _evidence(monkeypatch, _cap() - 1, head=HEADS[_cap() - 1])
    _mark(repo, home)
    assert not _round_file(repo, home).exists(), "fixture must have NO local streak"
    result = _run('git commit -m "fix"', repo, home)
    assert _decision(result) == "deny"
    assert "BLOCKED (mode-switch)" in result.stderr
    assert f"{_cap() - 1} distinct REVIEWED HEADS" in result.stderr
    # The local clause is absent: nothing was self-reported, and the message must
    # not claim defect-bearing rounds it has no evidence for — the reviewed-head
    # count includes clean reviews, so a converged PR lands here too.
    assert "consecutive EXTERNAL" not in result.stderr
    assert "Two rounds of NEW defects" not in result.stderr
    assert "IF those reviewed rounds each surfaced NEW defects" in result.stderr
    assert "includes CLEAN reviews" in result.stderr


def test_hard_stop_from_reviewed_heads_does_not_claim_a_failed_audit(monkeypatch, repo, home):
    _evidence(monkeypatch, _cap(), head=HEADS[_cap()])
    _mark(repo, home)
    result = _run('git commit -m "fix"', repo, home)
    assert _decision(result) == "deny"
    assert "mode-switch audit did NOT converge" not in result.stderr
    assert "if those rounds kept surfacing NEW defects" in result.stderr


def test_escalation_ack_at_a_count_also_covers_the_audit_tier(monkeypatch, repo, home):
    """An escalation decision at count N must not be followed by a mode-switch at N."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import review_state  # noqa: PLC0415

    cap = _cap()
    _evidence(monkeypatch, cap - 1, head=HEADS[cap - 1])
    _mark(repo, home)
    env_home = os.environ.get("HOME")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(review_state, "_ROUND_DIR", home / ".genesis" / "review_rounds")
    try:
        review_state.record_reviewed_head_ack(
            str(repo), tier="escalation", repo="owner/repo", pr=99, heads=cap - 1
        )
    finally:
        if env_home is not None:
            monkeypatch.setenv("HOME", env_home)
    assert _decision(_run('git commit -m "fix"', repo, home)) == "allow"


def test_hard_stop_fires_on_reviewed_heads_with_no_local_streak(monkeypatch, repo, home):
    """(b) CAP reviewed heads, no local streak → HARD STOP, in every session type."""
    _evidence(monkeypatch, _cap(), head=HEADS[_cap()])
    _mark(repo, home)
    for dispatched in (False, True):
        result = _run('git commit -m "fix"', repo, home, dispatched=dispatched)
        assert _decision(result) == "deny"
        assert "review escalation cap reached" in result.stderr
        assert f"{_cap()} distinct REVIEWED HEADS" in result.stderr


def test_audit_ack_satisfies_its_count_and_rearms_at_the_next_head(monkeypatch, repo, home):
    """(c) An ack is tied to the reviewed-head count it acknowledged."""
    cap = _cap()
    _evidence(monkeypatch, cap - 1, head=HEADS[cap - 1])
    _mark(repo, home)
    assert _decision(_run('git commit -m "fix"  # audit-ack', repo, home)) == "allow"
    assert json.loads(_ack_file(repo, home).read_text())["audit"] == cap - 1
    # Same count, no sigil: the recorded ack satisfies it.
    assert _decision(_run('git commit -m "fix"', repo, home)) == "allow"

    # Next reviewed head: the audit ack does NOT reach it, and the HARD STOP arms.
    _evidence(monkeypatch, cap, head=HEADS[cap])
    stopped = _run('git commit -m "fix"', repo, home)
    assert _decision(stopped) == "deny"
    assert "review escalation cap reached" in stopped.stderr
    # An audit-ack is not an escalation-ack.
    assert _decision(_run('git commit -m "fix"  # audit-ack', repo, home)) == "deny"
    assert _decision(_run('git commit -m "fix"  # escalation-ack', repo, home)) == "allow"
    assert _decision(_run('git commit -m "fix"', repo, home)) == "allow"

    # One more reviewed head re-arms the stop: one ack never silences later rounds.
    _evidence(monkeypatch, cap + 1, head=HEADS[cap + 1])
    rearmed = _run('git commit -m "fix"', repo, home)
    assert _decision(rearmed) == "deny"
    assert "review escalation cap reached" in rearmed.stderr
    assert f"recorded against {cap + 1} reviewed heads" in rearmed.stderr


def test_ack_for_another_pull_request_does_not_satisfy(monkeypatch, repo, home):
    cap = _cap()
    _evidence(monkeypatch, cap, head=HEADS[cap])
    _mark(repo, home)
    assert _decision(_run('git commit -m "fix"  # escalation-ack', repo, home)) == "allow"
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_PR", "100")
    assert _decision(_run('git commit -m "fix"', repo, home)) == "deny"


def test_unreadable_budget_keeps_todays_local_only_behaviour(monkeypatch, repo, home):
    """(d) Unknown evidence never becomes a round block — only the existing ask."""
    _evidence(monkeypatch, _cap() + 1, head=HEADS[_cap() + 1])
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "bad-json")
    _mark(repo, home)
    foreground = _run('git commit -m "fix"', repo, home)
    assert _decision(foreground) == "ask"
    assert "could not be read reliably" in foreground.stdout
    background = _run('git commit -m "fix"', repo, home, dispatched=True)
    assert _decision(background) == "deny"
    assert "escalation cap" not in background.stderr
    assert "mode-switch" not in background.stderr
    assert not _ack_file(repo, home).exists()


def test_no_open_pr_keeps_todays_local_only_behaviour(monkeypatch, repo, home):
    """(e) No PR → no reviewed-head input; nothing recorded."""
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_PR", "none")
    _mark(repo, home)
    assert _decision(_run('git commit -m "fix"', repo, home)) == "allow"
    assert not _ack_file(repo, home).exists()


def test_reviewed_heads_pr_accepts_only_a_clean_read():
    ok = {"status": "ok", "count": 3, "repo": "o/r", "pr": 7}
    assert _guard._reviewed_heads_pr(ok) == ("o/r", 7, 3)
    assert _guard._reviewed_heads_pr(None) is None
    assert _guard._reviewed_heads_pr({**ok, "status": "unknown"}) is None
    for bad in ({"count": None}, {"count": True}, {"count": -1}, {"repo": ""}, {"pr": "7"}):
        assert _guard._reviewed_heads_pr({**ok, **bad}) is None, bad


def test_ack_store_is_scoped_and_tolerates_corruption(monkeypatch, tmp_path, repo):
    sys.path.insert(0, str(ROOT / "scripts"))
    import review_state  # noqa: PLC0415

    monkeypatch.setattr(review_state, "_ROUND_DIR", tmp_path / "rounds")
    cwd = str(repo)
    assert review_state.get_reviewed_head_acks(cwd, repo="o/r", pr=1) == {}
    review_state.record_reviewed_head_ack(cwd, tier="audit", repo="o/r", pr=1, heads=2)
    review_state.record_reviewed_head_ack(cwd, tier="escalation", repo="o/r", pr=1, heads=3)
    assert review_state.get_reviewed_head_acks(cwd, repo="O/R", pr=1) == {
        "audit": 2,
        "escalation": 3,
    }
    assert review_state.get_reviewed_head_acks(cwd, repo="o/r", pr=2) == {}
    # A record for another PR is replaced, not merged.
    review_state.record_reviewed_head_ack(cwd, tier="audit", repo="o/r", pr=2, heads=5)
    assert review_state.get_reviewed_head_acks(cwd, repo="o/r", pr=1) == {}
    assert review_state.get_reviewed_head_acks(cwd, repo="o/r", pr=2) == {"audit": 5}
    # Unknown tiers are never written.
    review_state.record_reviewed_head_ack(cwd, tier="bogus", repo="o/r", pr=2, heads=9)
    assert "bogus" not in json.loads(review_state._head_ack_file(cwd).read_text())
    for garbage in ("{", "[]", '{"repo": "o/r", "pr": 2, "audit": true}', "\xff"):
        review_state._head_ack_file(cwd).write_text(garbage)
        assert review_state.get_reviewed_head_acks(cwd, repo="o/r", pr=2) == {}
