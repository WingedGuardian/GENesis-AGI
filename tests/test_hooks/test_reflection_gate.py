"""Rule R: the commit gate denies a content commit while a round owes a reflection.

End to end through the real hook (a subprocess, a real temp repository, the
review-budget seams for GitHub), plus in-process checks of the paths a seam
cannot reach. One row per state of the plan's writer-state x reader table.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "review_enforcement_commit.py"
STATE = ROOT / "scripts" / "review_state.py"
sys.path.insert(0, str(ROOT / "scripts"))

import review_reflection as rr  # noqa: E402

_spec = importlib.util.spec_from_file_location("reflection_gate_hook", HOOK)
_gate = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_gate)

CODEX = "chatgpt-codex-connector[bot]"
P2 = "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow)</sub></sub>  Off by one**"


def _reviewed() -> str:
    """Two hours ago, read when a round is built (never at import): the round is
    then settled, and its reflection is later than its review."""
    return (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


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
    _git(path, "checkout", "-qb", "feature/reflection")
    (path / "f.py").write_text("value = 2\n")
    _git(path, "commit", "-qam", "the reviewed head")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / ".genesis").mkdir(parents=True)
    return path


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _open_round(monkeypatch, repo: Path, *, gate: bool = False) -> str:
    """One Codex finding review (key c11) on the repo's head: an open round 1."""
    head = _head(repo)
    base = _git(repo, "rev-parse", "HEAD~1").strip()
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_PR", "99")
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_REPO", "owner/repo")
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", head)
    commits = [{"sha": base, "parents": []}, {"sha": head, "parents": [base]}]
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "\n".join(json.dumps(c) for c in commits))
    review = {
        "id": 1,
        "login": CODEX,
        "commit_id": head,
        "state": "COMMENTED",
        "submitted_at": _reviewed(),
        "body": "",
        "top_level": [P2],
        "top_level_ids": [11],
    }
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", json.dumps(review))
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
    name = "scripts/review_budget.py" if gate else "src/example.py"
    monkeypatch.setenv(
        "_TEST_REVIEW_BUDGET_FILES", json.dumps({"filename": name, "previous_filename": None})
    )
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_BODY", "")
    return head


def _reflection(head: str, keys=("c11",), *, premise_round: bool = False) -> str:
    premise = (
        [
            "Premise-check: P1 TRUE the bound is the right place to check",
            "Premise-check: P2 TRUE the one helper covers both callers",
            "Premise-evidence: ~/premise-check.txt SOUND",
        ]
        if premise_round
        else []
    )
    return (
        "\n".join(
            [
                f"Round-reflection: keys={','.join(keys)} head={head}",
                "Class: unchecked boundary value = 1",
                f"Premise: {'SOUND' if premise_round else 'LEAD'}",
                *premise,
                "Premise-why: the bound is checked in one place and trusted in another",
                "Impossible: route every reader through the one checked helper",
                "Scope: covered: the bound holds at the edge (tests/test_x.py)",
                "Decision: close-class",
                "Decision-why: the same unchecked read appears at both call sites",
                *[f"Disposition: {k} fix-now test=1" for k in keys],
                "Escalate: no",
            ]
        )
        + "\n"
    )


def _mark(repo: Path, home: Path) -> None:
    evidence = home / "review.txt"
    evidence.write_text(
        "SHOULD-FIX: none. adversarial review of f.py:1 complete\n" + "specific evidence " * 30
    )
    env = {**os.environ, "HOME": str(home)}
    subprocess.run(
        [sys.executable, str(STATE), "mark", "--agent-output", str(evidence)],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run(
    command: str, repo: Path, home: Path, *, dispatched: bool = False, mode: str | None = None
):
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(repo),
            "tool_input": {"command": command},
        }
    )
    env = {**os.environ, "HOME": str(home), "PYTEST_CURRENT_TEST": "reflection-gate"}
    env.pop("GENESIS_CC_SESSION", None)
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    if mode is not None:
        env["_TEST_REFLECTION_GATE"] = mode
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _stage_fix(repo: Path) -> None:
    (repo / "f.py").write_text("value = 3\n")
    _git(repo, "add", "f.py")


def _write_reflection(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "reflection.md"
    path.write_text(text)
    return path


# -- the deny ----------------------------------------------------------------


@pytest.mark.parametrize("dispatched", [False, True])
def test_a_content_commit_is_denied_while_the_round_owes_a_reflection(
    repo, home, monkeypatch, dispatched
):
    _open_round(monkeypatch, repo)
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home, dispatched=dispatched)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "owes a round reflection" in res.stderr
    assert "Owed: c11." in res.stderr
    assert "review_reflection.py status" in res.stderr
    assert "--allow-empty --cleanup=verbatim -F" in res.stderr


def test_the_deny_names_the_second_reflection_route_before_a_push(repo, home, monkeypatch):
    """A committed reflection usually credits nothing because it failed its full
    check; a corrected second one clears it. Pushing is the exit only when a fix
    already sits after the head, so the deny must not prescribe it alone."""
    _open_round(monkeypatch, repo)
    _stage_fix(repo)
    res = _run('git commit -m "fix"', repo, home)
    assert "commit a second reflection" in res.stderr
    assert res.stderr.index("second reflection") < res.stderr.index("then push")


def test_a_refused_kill_switch_explains_itself_on_the_deny(repo, home, monkeypatch):
    """The operator who wrote advisory mode but a duplicate key must see why the
    gate still enforces, on the deny that the refusal caused."""
    _open_round(monkeypatch, repo)
    cfg = home / ".genesis" / "config" / "genesis.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("hooks:\n  reflection_gate: advisory\nhooks:\n  reflection_gate: advisory\n")
    _stage_fix(repo)
    env_mode = os.environ.pop("_TEST_REFLECTION_GATE", None)
    try:
        res = _run('git commit -m "fix"', repo, home)
    finally:
        if env_mode is not None:
            os.environ["_TEST_REFLECTION_GATE"] = env_mode
    assert res.returncode == 2 and "owes a round reflection" in res.stderr
    assert "declares hooks more than once" in res.stderr and "ENFORCES" in res.stderr


def test_a_docs_only_fix_is_denied_too(repo, home, monkeypatch):
    """Rule R sits before the docs/config skip, as the streak tiers did."""
    _open_round(monkeypatch, repo)
    (repo / "notes.md").write_text("why\n")
    _git(repo, "add", "notes.md")
    res = _run('git commit -m "docs"', repo, home)
    assert res.returncode == 2 and "owes a round reflection" in res.stderr


def test_the_gate_lane_deny_names_its_audit(repo, home, monkeypatch):
    _open_round(monkeypatch, repo, gate=True)
    _stage_fix(repo)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 2
    assert "gate lane" in res.stderr and "Audit-evidence" in res.stderr
    assert "--gate" in res.stderr


# -- the reflection commit -----------------------------------------------------


def test_a_valid_reflection_commit_is_admitted_without_an_ask(repo, home, monkeypatch, tmp_path):
    head = _open_round(monkeypatch, repo)
    msg = _write_reflection(tmp_path, _reflection(head))
    res = _run(f"git commit --allow-empty --cleanup=verbatim -F {msg}", repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    assert '"permissionDecision"' not in res.stdout


def test_after_the_reflection_the_fix_commit_passes_rule_r(repo, home, monkeypatch, tmp_path):
    head = _open_round(monkeypatch, repo)
    msg = _write_reflection(tmp_path, _reflection(head))
    _git(repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(msg))
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "owes a round reflection" not in res.stdout + res.stderr


@pytest.mark.parametrize(
    "setup, why",
    [
        ("staged", "files are staged"),
        ("merging", "merge, rebase, cherry-pick or revert is in progress"),
        ("wrong_head", "not the PR head"),
        ("bad_grammar", "does not pass its grammar"),
        ("message_flag", "the reflection form is exactly"),
    ],
)
def test_a_near_miss_reflection_is_denied_with_its_exit(
    repo, home, monkeypatch, tmp_path, setup, why
):
    head = _open_round(monkeypatch, repo)
    text = _reflection("f" * 40 if setup == "wrong_head" else head)
    if setup == "bad_grammar":
        text = text.replace("Escalate: no\n", "")
    msg = _write_reflection(tmp_path, text)
    command = f"git commit --allow-empty --cleanup=verbatim -F {msg}"
    if setup == "staged":
        _stage_fix(repo)
    if setup == "merging":
        (repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    if setup == "message_flag":
        command = f'git commit --allow-empty -m "x" -F {msg}'
    res = _run(command, repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "looks like a round reflection" in res.stderr and why in res.stderr


def _past_the_budget(monkeypatch, repo: Path) -> str:
    """Four rounds on four REAL commits (so their ancestry is readable whatever the
    clock says relative to the cutover): the ordinary lane's standing budget is
    spent, so every commit would get the round-approval ask."""
    _open_round(monkeypatch, repo)
    for n in (1, 2, 3):
        (repo / "f.py").write_text(f"value = {10 + n}\n")
        _git(repo, "commit", "-qam", f"fix {n}")
    shas = _git(repo, "rev-list", "--reverse", "HEAD").split()
    head = shas[-1]
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", head)
    rows = [{"sha": shas[0], "parents": []}]
    rows += [{"sha": shas[i], "parents": [shas[i - 1]]} for i in range(1, len(shas))]
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "\n".join(json.dumps(r) for r in rows))
    reviews = []
    for i, sha in enumerate(shas[-4:]):
        when = (datetime.now(UTC) - timedelta(hours=5 - i)).isoformat().replace("+00:00", "Z")
        reviews.append(
            {
                "id": 1 + i,
                "login": CODEX,
                "commit_id": sha,
                "state": "COMMENTED",
                "submitted_at": when,
                "body": "",
                "top_level": [P2],
                "top_level_ids": [11 + i],
            }
        )
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "\n".join(json.dumps(r) for r in reviews))
    return head


@pytest.mark.parametrize("dispatched", [False, True])
def test_past_the_budget_a_reflection_skips_the_round_approval_ask(
    repo, home, monkeypatch, tmp_path, dispatched
):
    """Plan N7: an admitted reflection is empty and is the step a round past its
    budget should take, so it exits before the ask (and before a dispatched
    session's deny). A content commit in the same state is still Rule R's deny."""
    head = _past_the_budget(monkeypatch, repo)
    _stage_fix(repo)
    _mark(repo, home)
    denied = _run('git commit -m "fix"', repo, home, dispatched=dispatched)
    assert denied.returncode == 2 and "Owed: c14." in denied.stderr, denied.stderr
    _git(repo, "reset", "-q")
    msg = _write_reflection(tmp_path, _reflection(head, ("c14",), premise_round=True))
    res = _run(
        f"git commit --allow-empty --cleanup=verbatim -F {msg}", repo, home, dispatched=dispatched
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert '"permissionDecision"' not in res.stdout


def test_a_fifo_named_as_the_message_cannot_hang_the_hook(repo, home, monkeypatch, tmp_path):
    """A blocking read of a FIFO would run the hook into its timeout, and a killed
    hook lets the commit through. The read is non-blocking and refuses non-files."""
    _open_round(monkeypatch, repo)
    fifo = tmp_path / "message.fifo"
    os.mkfifo(fifo)
    _stage_fix(repo)
    res = _run(f"git commit -F {fifo}", repo, home)
    assert res.returncode == 2 and "owes a round reflection" in res.stderr
    _git(repo, "reset", "-q")
    res = _run(f"git commit --allow-empty -F {fifo}", repo, home)
    assert res.returncode == 2 and "is not a regular file" in res.stderr


# -- what never blocks ---------------------------------------------------------


def test_advisory_mode_allows_and_says_what_it_would_have_denied(repo, home, monkeypatch):
    _open_round(monkeypatch, repo)
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home, mode="advisory")
    assert res.returncode == 0, res.stdout + res.stderr
    context = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "advisory mode, NOT enforced" in context and "Owed: c11." in context


def test_no_round_open_is_untouched(repo, home, monkeypatch):
    _open_round(monkeypatch, repo)
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")  # nothing reviewed: no round
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def _budget(**over):
    budget = {
        "status": "ok",
        "pr": 99,
        "repo": "owner/repo",
        "count": 1,
        "gate_surface": False,
        "current_head": "a" * 40,
    }
    budget.update(over)
    return budget


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(_gate, "_ADVISORIES", [])
    return _gate._ADVISORIES


def _rule(budget, *, deadline=None):
    import time

    return _gate._reflection_rule(
        [],
        [],
        "git commit -m x",
        "/nonexistent",
        budget,
        hook_deadline=time.monotonic() + 9 if deadline is None else deadline,
    )


def test_an_unknown_budget_is_not_rule_rs_to_judge(quiet):
    assert _rule(None) == ("continue", None)
    assert _rule(_budget(status="unknown")) == ("continue", None)
    assert quiet == []


@pytest.mark.parametrize(
    "round_state, started, exempt",
    [
        ("open", "2026-10-01T00:00:00+00:00", True),  # opened before the cutover
        ("open", "2026-10-30T00:00:00+00:00", False),  # opened after it: always owes
        ("open", None, False),  # an unreadable start proves nothing
        ("open", "not a time", False),  # nor does an unparseable one
        ("complete", None, False),  # late keys only: owed from the cutover on
    ],
)
def test_an_exempt_pr_owes_nothing_for_a_round_opened_before_the_cutover(
    quiet, monkeypatch, round_state, started, exempt
):
    monkeypatch.setattr(_gate, "REFLECTION_EXEMPT_PRS", frozenset({99}))
    monkeypatch.setattr(rr, "REFLECTION_CUTOVER_ISO", "2026-10-22T00:00:00+00:00")
    state = {"owed": ["c1"], "round_started": started, "round_state": round_state}
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: state)
    verdict, message = _rule(_budget(repo="WingedGuardian/GENesis-AGI"))
    assert verdict == ("continue" if exempt else "deny"), message
    if not exempt:
        assert "Owed: c1." in message


@pytest.mark.parametrize(
    "owed,late_keys,want",
    [
        (["c1", "c2"], ["c2"], "deny"),
        (["c2"], ["c2"], "deny"),  # open debt was credited; late debt was not
        (["c1"], ["c2"], "continue"),  # late debt was already credited
        (["c1"], [], "continue"),
    ],
)
def test_snapshot_exemption_preserves_only_remaining_late_debt(
    quiet, monkeypatch, owed, late_keys, want,
):
    monkeypatch.setattr(_gate, "_reflection_gate_mode", lambda: "enforce")
    state = {
        "owed": owed, "open_keys": ["c1"], "late_keys": late_keys,
        "round_state": "open", "round_started": "2026-10-01T00:00:00+00:00",
    }
    original_state = json.loads(json.dumps(state))
    monkeypatch.setattr(rr, "REFLECTION_CUTOVER_ISO", "2026-10-22T00:00:00+00:00")
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: state)
    verdict, message = _rule(_budget(pr=3535, repo="WingedGuardian/GENesis-AGI"))
    assert verdict == want, message
    if want == "deny":
        assert "Owed: c2." in message
        assert "c1" not in message
    assert state == original_state  # exemption must not mutate reader evidence


@pytest.mark.parametrize(
    "repo,want",
    [
        ("WingedGuardian/GENesis-AGI", "continue"),
        ("wingedguardian/genesis-agi", "continue"),
        ("AnotherOwner/GENesis-AGI", "deny"),
        ("WingedGuardian/OtherRepository", "deny"),
    ],
)
def test_snapshot_exemption_is_bound_to_the_measured_repository(
    quiet, monkeypatch, repo, want,
):
    monkeypatch.setattr(_gate, "_reflection_gate_mode", lambda: "enforce")
    state = {
        "owed": ["c1"], "open_keys": ["c1"], "late_keys": [],
        "round_state": "open", "round_started": "2026-10-01T00:00:00+00:00",
    }
    monkeypatch.setattr(rr, "REFLECTION_CUTOVER_ISO", "2026-10-22T00:00:00+00:00")
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: state)
    monkeypatch.setattr(_gate, "_canonical_public_repo", lambda: "AnotherOwner/GENesis-AGI")
    verdict, message = _rule(_budget(pr=3535, repo=repo))
    assert verdict == want, message
    if want == "deny":
        assert "Owed: c1." in message


def test_snapshot_exemption_keeps_real_reader_late_debt(repo, quiet, monkeypatch):
    """The real git-backed reader combines old open debt with a new late key."""
    import time

    head = _head(repo)
    base = _git(repo, "rev-parse", "HEAD~1").strip()
    now = datetime.now(UTC)
    opened = (now - timedelta(hours=2)).isoformat()
    cutover = (now - timedelta(hours=1)).isoformat()
    late_at = (now - timedelta(minutes=30)).isoformat()
    budget = _budget(
        pr=3535,
        repo="WingedGuardian/GENesis-AGI",
        current_head=head,
        round_state="open",
        reflection_keys="ok",
        open_keys=["c11"],
        body="",
        rounds=[{
            "head": head,
            "opened_at": opened,
            "reviews": [{"submitted_at": opened, "finding_keys": ["c11"]}],
        }],
        commits=[{"sha": base, "parents": []}, {"sha": head, "parents": [base]}],
        findings=[{
            "head": base,
            "submitted_at": late_at,
            "late": True,
            "keys": ["c12"],
            "round_head": None,
        }],
    )
    monkeypatch.setattr(rr, "REFLECTION_CUTOVER_ISO", cutover)
    monkeypatch.setattr(_gate, "_reflection_gate_mode", lambda: "enforce")
    state = rr.owed_from_budget(
        str(repo), budget, budget["repo"], budget["pr"], now=now, cutover=cutover,
    )
    assert state["open_keys"] == ["c11"]
    assert state["late_keys"] == ["c12"]
    assert state["owed"] == ["c11", "c12"]
    verdict, message = _gate._reflection_rule(
        [], [], "git commit -m fix", str(repo), budget,
        hook_deadline=time.monotonic() + 9,
    )
    assert verdict == "deny", message
    assert "Owed: c12." in message
    assert "c11" not in message
    assert quiet == []


def _late_reflection_problem(repo, monkeypatch, tmp_path, *, owed, names, late_rounds):
    """Admission at round 2 with ``owed`` owed (c11 a late key whose round
    contexts, as review_late would place them, are ``late_rounds``) for a
    round-1-style reflection (Premise: LEAD) naming ``names``."""
    import time

    import review_late

    head = _head(repo)
    late = [{"key": "c11", "head": head, "at": datetime.now(UTC)}]
    monkeypatch.setattr(review_late, "late_findings", lambda *a, **k: late)
    monkeypatch.setattr(review_late, "fix_time_reader", lambda *a, **k: (lambda h: None))
    monkeypatch.setattr(review_late, "_ancestry", lambda budget: (set(), lambda sha: ()))
    monkeypatch.setattr(
        review_late,
        "_contexts",
        lambda *a, **k: [review_late.RoundContext(n, None, ()) for n in late_rounds],
    )
    msg = _write_reflection(tmp_path, _reflection(head, keys=tuple(names)))
    command = f"git commit --allow-empty --cleanup=verbatim -F {msg}"
    segs, _ = _gate.analyze_checked(command)
    state = {"owed": list(owed), "late_keys": ["c11"], "cutover": "2026-01-01T00:00:00+00:00"}
    budget = _budget(count=2, current_head=head)
    return _gate._reflection_problem(
        segs, command, str(repo), budget, rr, deadline=time.monotonic() + 30, state=state
    )


@pytest.mark.parametrize("late_round", [0, 1])
def test_a_late_key_is_admitted_under_its_own_round(repo, monkeypatch, tmp_path, late_round):
    """Two recorded rounds, only a late key from round 1 (or round 0) owed: a
    reflection valid for that round is admitted, as review_late would credit it,
    not refused for lacking round 2's premise checks."""
    assert _late_reflection_problem(
        repo, monkeypatch, tmp_path, owed=["c11"], names=["c11"], late_rounds=[late_round]
    ) is None


@pytest.mark.parametrize(
    "owed,names,late_rounds,why",
    [
        # An open round-2 key is judged at round 2, even with a round-1 late key owed.
        (["c11", "c12"], ["c12"], [1], "does not pass its grammar"),
        # A late key on both sides of a merge must pass every candidate round.
        (["c11"], ["c11"], [1, 2], "does not pass its grammar"),
        # A late key whose round cannot be placed can never be credited.
        (["c11"], ["c11"], [], "cannot be placed"),
        # Naming no owed key credits nothing.
        (["c11"], ["c99"], [1], "names none of the owed keys"),
    ],
)
def test_admission_follows_what_the_debt_reader_would_credit(
    repo, monkeypatch, tmp_path, owed, names, late_rounds, why
):
    problem = _late_reflection_problem(
        repo, monkeypatch, tmp_path, owed=owed, names=names, late_rounds=late_rounds
    )
    assert problem is not None and why in problem, problem


def test_a_late_round_one_key_is_admitted_through_the_real_reader(repo, monkeypatch, tmp_path):
    """No stubs: two rounds recorded, round 2 closed, a late finding on round 1
    owed. The real late reader places it at round 1, so a round-1 reflection is
    admitted, and the same text judged at round 2 would not be."""
    import time

    head = _head(repo)
    base = _git(repo, "rev-parse", "HEAD~1").strip()
    now = datetime.now(UTC)
    cutover = (now - timedelta(hours=4)).isoformat()

    def at(hours):
        return (now - timedelta(hours=hours)).isoformat()

    budget = _budget(
        pr=3535,
        repo="WingedGuardian/GENesis-AGI",
        count=2,
        current_head=head,
        round_state="complete",
        reflection_keys="ok",
        open_keys=[],
        body="",
        rounds=[
            {"head": base, "opened_at": at(3),
             "reviews": [{"submitted_at": at(3), "finding_keys": ["c10"]}]},
            {"head": head, "opened_at": at(2),
             "reviews": [{"submitted_at": at(2), "finding_keys": ["c12"]}]},
        ],
        commits=[{"sha": base, "parents": []}, {"sha": head, "parents": [base]}],
        findings=[{
            "head": base,
            "submitted_at": at(0.5),
            "late": True,
            "keys": ["c11"],
            "round_head": base,
        }],
    )
    state = rr.owed_from_budget(
        str(repo), budget, budget["repo"], budget["pr"], now=now, cutover=cutover,
    )
    assert state["owed"] == ["c11"], state
    text = _reflection(head, keys=("c11",))
    assert rr.parse(text, round_number=2).problems  # round 2 would refuse it
    msg = _write_reflection(tmp_path, text)
    command = f"git commit --allow-empty --cleanup=verbatim -F {msg}"
    segs, _ = _gate.analyze_checked(command)
    assert _gate._reflection_problem(
        segs, command, str(repo), budget, rr, deadline=time.monotonic() + 30, state=state
    ) is None


@pytest.mark.parametrize(

    "outcome",
    [rr.Refused("git unreadable"), RuntimeError("boom"), {"owed": None}],
)
def test_every_unknown_allows_with_an_advisory(quiet, monkeypatch, outcome):
    def owed(*a, **k):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(rr, "owed_from_budget", owed)
    assert _rule(_budget()) == ("continue", None)
    assert len(quiet) == 1 and "allowed" in quiet[0]


def test_no_time_left_allows_with_an_advisory(quiet, monkeypatch):
    import time

    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: pytest.fail("read"))
    assert _rule(_budget(), deadline=time.monotonic() + 0.5) == ("continue", None)
    assert "no time left" in quiet[0]


def test_the_owed_read_gets_a_bounded_slice_of_the_hook_budget(quiet, monkeypatch):
    import time

    seen = {}

    def owed(*a, deadline, **k):
        seen["slice"] = deadline - time.monotonic()
        return {"owed": []}

    monkeypatch.setattr(rr, "owed_from_budget", owed)
    assert _rule(_budget()) == ("continue", None)
    assert 0 < seen["slice"] <= _gate._REFLECTION_SLICE


def test_nothing_owed_continues_silently(quiet, monkeypatch):
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    assert _rule(_budget()) == ("continue", None) and quiet == []


# -- the kill switch -----------------------------------------------------------


@pytest.mark.parametrize(
    "text, want",
    [
        ("hooks:\n  reflection_gate: advisory\n", "advisory"),
        ("hooks:\n  reflection_gate: enforce\n", "enforce"),
        ("hooks:\n  asks:\n    secrets_env: off\n", "enforce"),
        ("hooks:\n  reflection_gate: off\n", "enforce"),
        ("hooks:\n  reflection_gate: on\n", "enforce"),  # truthy, still not advisory
        ("hooks:\n  reflection_gate: Advisory\n", "enforce"),  # exact spelling only
        ("hooks:\n  reflection_gate: advisory\n  reflection_gate: enforce\n", "enforce"),
        ("hooks:\n  reflection_gate: enforce\n  reflection_gate: advisory\n", "enforce"),
        ("base: &b\n  reflection_gate: advisory\nhooks:\n  <<: *b\n", "enforce"),
        ("hooks: [\n", "enforce"),
    ],
)
def test_the_kill_switch_reads_advisory_only_when_declared_exactly(
    tmp_path, monkeypatch, text, want
):
    cfg = tmp_path / ".genesis" / "config" / "genesis.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(text)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("_TEST_REFLECTION_GATE", raising=False)
    assert _gate._reflection_gate_mode() == want


def test_a_missing_config_enforces(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("_TEST_REFLECTION_GATE", raising=False)
    assert _gate._reflection_gate_mode() == "enforce"


# -- a round held open across a merge of main (#4133 item 6) -------------------


def _merge_main_after_the_review(
    monkeypatch, repo: Path, *, extra: bool = False
) -> tuple[str, str]:
    """The #3004 shape: round 1 reviewed at head H, then main merged in (M) and
    pushed. ``extra`` makes the merge carry content of its own, so it is a fix.
    Returns (H, M)."""
    head = _open_round(monkeypatch, repo)
    base = _git(repo, "rev-parse", "HEAD~1").strip()
    _git(repo, "checkout", "-q", "main")
    (repo / "other.py").write_text("other = 1\n")
    _git(repo, "add", "other.py")
    _git(repo, "commit", "-qm", "main moves on")
    upstream = _head(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", upstream)
    _git(repo, "checkout", "-q", "feature/reflection")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "main")
    if extra:
        (repo / "f.py").write_text("value = 99\n")
        _git(repo, "commit", "-q", "--amend", "--no-edit", "-a")
    merge = _head(repo)
    commits = [
        {"sha": base, "parents": []},
        {"sha": head, "parents": [base]},
        {"sha": upstream, "parents": [base]},
        {"sha": merge, "parents": [head, upstream]},
    ]
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", merge)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "\n".join(json.dumps(c) for c in commits))
    return head, merge


def test_a_merge_of_main_pushed_after_the_review_keeps_the_round_owed(repo, home, monkeypatch):
    """#3004's round 5: the head moved off the reviewed commit only by a merge of
    main, and the fix commit was admitted with nothing owed."""
    head, _ = _merge_main_after_the_review(monkeypatch, repo)
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "Owed: c11." in res.stderr
    assert head[:12] in res.stderr  # names the reviewed head that is held


def test_a_held_round_admits_a_reflection_naming_the_pr_head(repo, home, monkeypatch, tmp_path):
    """The held round's keys are owed like any late key: the reflection names
    the PR head (the merge), as on main, never the older reviewed head."""
    head, merge = _merge_main_after_the_review(monkeypatch, repo)
    wrong = _write_reflection(tmp_path, _reflection(head))
    res = _run(f"git commit --allow-empty --cleanup=verbatim -F {wrong}", repo, home)
    assert res.returncode == 2 and "not the PR head" in res.stderr, res.stdout + res.stderr
    msg = _write_reflection(tmp_path, _reflection(merge))
    res = _run(f"git commit --allow-empty --cleanup=verbatim -F {msg}", repo, home)
    assert res.returncode == 0, res.stdout + res.stderr


def test_after_the_reflection_the_held_fix_passes(repo, home, monkeypatch, tmp_path):
    _, merge = _merge_main_after_the_review(monkeypatch, repo)
    msg = _write_reflection(tmp_path, _reflection(merge))
    _git(repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(msg))
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "owes a round reflection" not in res.stdout + res.stderr
    # The reflection named the PR head, as the deny says: the round counts as
    # answered, so no "round has no reflection" advisory either.
    assert "no reflection" not in res.stdout + res.stderr


def test_a_reviewed_merge_still_owes_the_round_behind_it(repo, home, monkeypatch, tmp_path):
    """The merge of main draws findings of its own (c33), so round 2 opens at
    it. That does not answer round 1: c11 stays owed beside c33, and ONE
    reflection naming the merge credits both."""
    head, merge = _merge_main_after_the_review(monkeypatch, repo)
    old = datetime.now(UTC) - timedelta(hours=3)

    def review(n, sha, ids, minutes):
        return {
            "id": n,
            "login": CODEX,
            "commit_id": sha,
            "state": "COMMENTED",
            "submitted_at": (old + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
            "body": "",
            "top_level": [P2] * len(ids),
            "top_level_ids": ids,
        }

    reviews = [review(1, head, [11], 0), review(2, merge, [33], 30)]
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "\n".join(json.dumps(r) for r in reviews))
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "c11" in res.stderr and "c33" in res.stderr
    _git(repo, "reset", "-q")
    (home / "premise-check.txt").write_text(
        "Design-premise: SOUND\n"
        "Expected before checking: the bound is the right place\n"
        "P1 TRUE — the bound is checked in one place · read · 90% · falsified by a second reader\n"
        "P2 TRUE — the one helper covers both callers · read · 90% · falsified by a third caller\n"
    )
    msg = _write_reflection(tmp_path, _reflection(merge, keys=("c11", "c33"), premise_round=True))
    res = _run(f"git commit --allow-empty --cleanup=verbatim -F {msg}", repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    _git(repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(msg))
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "owes a round reflection" not in res.stdout + res.stderr


def test_a_merge_that_carries_content_completes_the_round_and_says_so(repo, home, monkeypatch):
    """A merge with content of its own is a fix: the round is complete as today,
    but the reviewed round behind it with no reflection is no longer silent."""
    head, _ = _merge_main_after_the_review(monkeypatch, repo, extra=True)
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "owes a round reflection" not in res.stderr
    context = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "no reflection" in context and head[:12] in context


def test_a_real_commit_after_the_review_completes_the_round_and_says_so(repo, home, monkeypatch):
    head = _open_round(monkeypatch, repo)
    base = _git(repo, "rev-parse", "HEAD~1").strip()
    (repo / "f.py").write_text("value = 5\n")
    _git(repo, "commit", "-qam", "a fix made before any reflection")
    fix = _head(repo)
    commits = [
        {"sha": base, "parents": []},
        {"sha": head, "parents": [base]},
        {"sha": fix, "parents": [head]},
    ]
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", fix)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "\n".join(json.dumps(c) for c in commits))
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    context = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "no reflection" in context and head[:12] in context


def test_a_reflected_round_behind_the_head_stays_silent(repo, home, monkeypatch, tmp_path):
    head = _open_round(monkeypatch, repo)
    msg = _write_reflection(tmp_path, _reflection(head))
    _git(repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(msg))
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD").strip()
    reflection = _head(repo)
    (repo / "f.py").write_text("value = 5\n")
    _git(repo, "commit", "-qam", "the fix")
    fix = _head(repo)
    commits = [
        {"sha": base, "parents": []},
        {"sha": head, "parents": [base]},
        {"sha": reflection, "parents": [head]},
        {"sha": fix, "parents": [reflection]},
    ]
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", fix)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "\n".join(json.dumps(c) for c in commits))
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "more"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "no reflection" not in res.stdout


def test_status_holds_the_round_open_too(repo, monkeypatch):
    """``status`` and the gate share ``owed_from_budget``, so the round held
    across a merge of main is owed there too; the head stays the PR head."""
    import review_budget

    head, merge = _merge_main_after_the_review(monkeypatch, repo)
    budget = review_budget.evaluate_pr("owner/repo", 99)
    assert budget["round_state"] == "complete"  # the producer is unchanged
    state = rr.owed_from_budget(
        str(repo),
        budget,
        "owner/repo",
        99,
        now=datetime.now(UTC),
        cutover=rr.REFLECTION_CUTOVER_ISO,  # the path status and the gate both take
    )
    assert state["owed"] == ["c11"]
    assert state["head"] == merge and state["held_round"] == head


def test_the_newest_of_two_rounds_is_held_open(repo, home, monkeypatch):
    """Two rounds behind a merge of main: the newer one is held open, owing its
    own key (c22), never the older round's."""
    first = _open_round(monkeypatch, repo)
    base = _git(repo, "rev-parse", "HEAD~1").strip()
    (repo / "f.py").write_text("value = 7\n")
    _git(repo, "commit", "-qam", "round one's fix")
    second = _head(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "other.py").write_text("other = 1\n")
    _git(repo, "add", "other.py")
    _git(repo, "commit", "-qm", "main moves on")
    upstream = _head(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", upstream)
    _git(repo, "checkout", "-q", "feature/reflection")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "main")
    merge = _head(repo)
    commits = [
        {"sha": base, "parents": []},
        {"sha": first, "parents": [base]},
        {"sha": second, "parents": [first]},
        {"sha": upstream, "parents": [base]},
        {"sha": merge, "parents": [second, upstream]},
    ]
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", merge)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "\n".join(json.dumps(c) for c in commits))
    older = (datetime.now(UTC) - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    reviews = [
        {
            "id": 1,
            "login": CODEX,
            "commit_id": first,
            "state": "COMMENTED",
            "submitted_at": older,
            "body": "",
            "top_level": [P2],
            "top_level_ids": [11],
        },
        {
            "id": 2,
            "login": CODEX,
            "commit_id": second,
            "state": "COMMENTED",
            "submitted_at": _reviewed(),
            "body": "",
            "top_level": [P2],
            "top_level_ids": [22],
        },
    ]
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "\n".join(json.dumps(r) for r in reviews))
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "Owed: c22." in res.stderr and second[:12] in res.stderr


def _point_budget_at(monkeypatch, repo: Path) -> str:
    """Re-point the budget's head and commit graph at the repo's current head,
    reading every commit on it (parents from git)."""
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD").strip()
    rows = [{"sha": base, "parents": []}]
    for sha in _git(repo, "rev-list", "--reverse", "--topo-order", "HEAD").split()[1:]:
        parents = _git(repo, "rev-list", "--parents", "-n1", sha).split()[1:]
        rows.append({"sha": sha, "parents": parents})
    head = _head(repo)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", head)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "\n".join(json.dumps(r) for r in rows))
    return head


def test_an_empty_commit_after_the_review_keeps_the_round_owed(repo, home, monkeypatch):
    """An empty commit fixes nothing (a CI re-trigger, or a pushed reflection
    that failed its full check): the round stays open, and the deny says why."""
    head = _open_round(monkeypatch, repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "retrigger CI")
    _point_budget_at(monkeypatch, repo)
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "Owed: c11." in res.stderr and head[:12] in res.stderr
    assert "only merges of main or empty" in res.stderr


def test_a_reflection_committed_below_the_merge_still_credits(repo, home, monkeypatch, tmp_path):
    """Reflect at the reviewed head, then merge main, then fix: the reflection
    sits below the merge and still answers the held round."""
    head = _open_round(monkeypatch, repo)
    msg = _write_reflection(tmp_path, _reflection(head))
    _git(repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(msg))
    _git(repo, "checkout", "-q", "main")
    (repo / "other.py").write_text("other = 1\n")
    _git(repo, "add", "other.py")
    _git(repo, "commit", "-qm", "main moves on")
    _git(repo, "update-ref", "refs/remotes/origin/main", _head(repo))
    _git(repo, "checkout", "-q", "feature/reflection")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "main")
    _point_budget_at(monkeypatch, repo)
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "owes a round reflection" not in res.stdout + res.stderr


def test_a_main_first_merge_is_no_hold(repo, home, monkeypatch):
    """A merge whose FIRST parent is main (merging the branch into a main
    checkout) is not a merge of main into the branch: no hold, as before."""
    head = _open_round(monkeypatch, repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "other.py").write_text("other = 1\n")
    _git(repo, "add", "other.py")
    _git(repo, "commit", "-qm", "main moves on")
    _git(repo, "update-ref", "refs/remotes/origin/main", _head(repo))
    _git(repo, "checkout", "-q", "-B", "feature/reflection", "main")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", head)
    assert _git(repo, "rev-list", "--parents", "-n1", "HEAD").split()[2] == head  # main first
    _point_budget_at(monkeypatch, repo)
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert "owes a round reflection" not in res.stderr, res.stderr


def test_the_advisory_ignores_a_round_from_before_the_cutover(repo, monkeypatch):
    """A round opened before the gate went live owes nothing, so a commit past
    it is no news: the advisory names only rounds from the cutover on."""
    import review_budget

    head = _open_round(monkeypatch, repo)
    (repo / "f.py").write_text("value = 5\n")
    _git(repo, "commit", "-qam", "a fix made before any reflection")
    _point_budget_at(monkeypatch, repo)
    budget = review_budget.evaluate_pr("owner/repo", 99)
    assert budget["round_state"] == "complete"
    live = rr.unreflected_round_behind(str(repo), budget, cutover=rr.REFLECTION_CUTOVER_ISO)
    later = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert live == head
    assert rr.unreflected_round_behind(str(repo), budget, cutover=later) is None


def test_a_refused_read_leaves_the_advisory_unknown(repo, monkeypatch, tmp_path):
    """A reflection naming a later head may answer the round, but when git
    cannot relate the two (out of time, or the head not fetched) the round is
    unknown, never "no reflection"."""
    import review_budget

    head = _open_round(monkeypatch, repo)
    (repo / "f.py").write_text("value = 5\n")
    _git(repo, "commit", "-qam", "a fix made before any reflection")
    msg = _write_reflection(tmp_path, _reflection(_head(repo)))
    _git(repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(msg))
    _point_budget_at(monkeypatch, repo)
    budget = review_budget.evaluate_pr("owner/repo", 99)
    assert budget["round_state"] == "complete"
    cut = rr.REFLECTION_CUTOVER_ISO
    # Control: git can relate them, a real fix lies between, so the advisory names the round.
    assert rr.unreflected_round_behind(str(repo), budget, cutover=cut) == head

    def refused(*_a, **_k):
        raise rr.Refused("ran out of time")

    monkeypatch.setattr(rr, "fix_rows", refused)
    assert rr.unreflected_round_behind(str(repo), budget, cutover=cut) is None


def test_an_older_round_reviewed_after_the_merge_stays_owed(repo, monkeypatch):
    """Rounds at A (c11) and B (c22), an empty commit between them, then a merge
    of main M; A's review lands after M. The held view holds B open, and A's key
    is still owed as a late key: the owe side's fix horizon is the real head M,
    not the held round's head (a merge of main is no fix of B, but it is one of
    A's head). Secondary review finding on this PR."""
    import review_budget

    first = _open_round(monkeypatch, repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "nothing to fix yet")
    second = _head(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "other.py").write_text("other = 1\n")
    _git(repo, "add", "other.py")
    _git(repo, "commit", "-qm", "main moves on")
    _git(repo, "update-ref", "refs/remotes/origin/main", _head(repo))
    _git(repo, "checkout", "-q", "feature/reflection")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "main")
    _point_budget_at(monkeypatch, repo)
    soon = datetime.now(UTC) + timedelta(minutes=1)
    reviews = [
        {
            "id": n,
            "login": CODEX,
            "commit_id": sha,
            "state": "COMMENTED",
            "submitted_at": (soon + timedelta(minutes=n)).isoformat().replace("+00:00", "Z"),
            "body": "",
            "top_level": [P2],
            "top_level_ids": [key],
        }
        for n, sha, key in ((1, first, 11), (2, second, 22))
    ]
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "\n".join(json.dumps(r) for r in reviews))
    budget = review_budget.evaluate_pr("owner/repo", 99)
    assert budget["round_state"] == "complete"
    state = rr.owed_from_budget(
        str(repo), budget, "owner/repo", 99, now=soon + timedelta(hours=1),
        cutover=rr.REFLECTION_CUTOVER_ISO,
    )
    assert state["held_round"] == second
    assert sorted(state["owed"]) == ["c11", "c22"]


def test_a_late_finding_after_the_held_round_is_answerable(repo, home, monkeypatch, tmp_path):
    """Round 1 of #4294 (Codex P2, Devin severe): H reviewed with c11, an empty
    commit E, a merge of main M, a clean review of M, then findings c22 on E
    (late: M was reviewed first). H is held and E's finding is late; ONE
    reflection naming the PR head credits both, and the fix then passes."""
    head = _open_round(monkeypatch, repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "nothing to fix yet")
    empty = _head(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "other.py").write_text("other = 1\n")
    _git(repo, "add", "other.py")
    _git(repo, "commit", "-qm", "main moves on")
    _git(repo, "update-ref", "refs/remotes/origin/main", _head(repo))
    _git(repo, "checkout", "-q", "feature/reflection")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "main")
    merge = _point_budget_at(monkeypatch, repo)
    old = datetime.now(UTC) - timedelta(hours=3)

    def review(n, sha, ids, minutes):
        when = (old + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
        top = [P2] * len(ids)
        return {
            "id": n,
            "login": CODEX,
            "commit_id": sha,
            "state": "COMMENTED",
            "submitted_at": when,
            "body": "",
            "top_level": top,
            "top_level_ids": ids,
        }

    reviews = [review(1, head, [11], 0), review(3, empty, [22], 60)]
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "\n".join(json.dumps(r) for r in reviews))
    clean = {  # a clean Codex review of M, before E's findings: they are late
        "login": CODEX,
        "type": "Bot",
        "body": f"Codex Review: Didn't find any major issues.\nReviewed commit: `{merge[:10]}`",
        "created_at": (old + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
    }
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", json.dumps(clean))
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "c11" in res.stderr and "c22" in res.stderr
    _git(repo, "reset", "-q")
    msg = _write_reflection(tmp_path, _reflection(merge, keys=("c11", "c22")))
    res = _run(f"git commit --allow-empty --cleanup=verbatim -F {msg}", repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    _git(repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(msg))
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "fix"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "owes a round reflection" not in res.stdout + res.stderr


def test_a_reflection_that_fails_its_grammar_does_not_silence_the_advisory(
    repo, home, monkeypatch, tmp_path
):
    """A header naming the round head is not an answer when the reflection
    fails its grammar: the advisory still names the unanswered round."""
    head = _open_round(monkeypatch, repo)
    broken = _reflection(head).replace("Escalate: no\n", "")
    msg = _write_reflection(tmp_path, broken)
    _git(repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(msg))
    (repo / "f.py").write_text("value = 5\n")
    _git(repo, "commit", "-qam", "the fix")
    _point_budget_at(monkeypatch, repo)
    _stage_fix(repo)
    _mark(repo, home)
    res = _run('git commit -m "more"', repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    context = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "no reflection" in context and head[:12] in context
