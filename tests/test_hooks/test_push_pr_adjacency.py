"""The push guard's PR-adjacency hygiene: no silent public branch without a PR,
and no second branch name for work a PR already carries.

Two mechanisms, both measured on this repo before they were built:

  * 30 of 104 remote branches were closed/merged PRs' leftovers, and two of
    them were re-published under second names and grew DUPLICATE PRs for work
    already squash-merged (ancestry destroyed, so no ancestry test can see it).
  * A public branch with no open PR runs no CI and no leak-detector at all
    (ci.yml triggers on pull_request), which is the exact state the repo's
    publish rule exists to prevent.

The design constraint these tests pin is the FAIL DIRECTION, per finding:

  * `_open_pr_count_for_branch` distinguishes 0 (measured: no PR) from None
    (unanswerable). Only a measured 0 downgrades the silent re-push allow to an
    ask; None keeps the status quo, because this is a hygiene prompt on an
    already-approved branch, not a security boundary, and a network blip must
    not manufacture prompts.
  * `_prs_already_containing_head` returns [] on ANY failure: it only enriches
    an ask that is shown regardless, so degradation is the prompt as it was —
    never a block, never a silent pass.

Subprocess seams are patched via a recording fake rather than by running gh:
these tests must pass on a fresh clone with no network and no gh auth.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import private_module

gpg = private_module(
    "git_push_guard",
    Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py",
)


class FakeRun:
    """A subprocess.run stand-in scripted per argv prefix.

    Implements the real contract (returns CompletedProcess with returncode and
    stdout) so the code under test runs unmodified. Unscripted argv raises —
    a test that triggers an unexpected subprocess should fail loudly, not
    silently exercise the real gh.
    """

    def __init__(self) -> None:
        self.scripts: list[tuple[tuple[str, ...], int, str]] = []
        self.calls: list[list[str]] = []
        # Recorded so a test can assert the TIMEOUT the code passed. Without
        # this, reverting every `_gh_timeout(...)` to a flat `timeout=10` left
        # the whole suite green — the deadline-sharing claim was asserted only
        # by the diff that made it.
        self.kwargs: list[dict] = []

    def script(self, prefix: tuple[str, ...], returncode: int, stdout: str) -> None:
        self.scripts.append((prefix, returncode, stdout))

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.kwargs.append(dict(kwargs))
        for prefix, rc, out in self.scripts:
            if tuple(args[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(args, rc, out, "")
        raise AssertionError(f"unscripted subprocess: {args!r}")


# ─── _open_pr_count_for_branch: 0 is a measurement, None is an admission ─────


def _pr(number: int, base: str = "main", owner: str = "owner") -> dict:
    return {
        "number": number,
        "baseRefName": base,
        "isCrossRepository": owner != "owner",
        "headRepositoryOwner": {"login": owner},
    }


def _script_pr_list(
    fake: FakeRun, rows: list[dict], default: str = "main", owner: str = "owner"
) -> None:
    fake.script(("gh", "pr", "list"), 0, json.dumps(rows))
    fake.script(("gh", "repo", "view"), 0, f"{default}\n{owner}/repo\n")


def test_zero_open_prs_is_a_measured_zero(monkeypatch) -> None:
    fake = FakeRun()
    _script_pr_list(fake, [])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") == 0


def test_an_open_pr_is_counted(monkeypatch) -> None:
    fake = FakeRun()
    _script_pr_list(fake, [_pr(7)])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") == 1


def test_a_pr_onto_a_non_default_base_does_not_count(monkeypatch) -> None:
    """A stacked PR contributes NO CI, so it must not silence the prompt.

    ci.yml filters pull_request to the default branch, so a request onto a
    feature base runs no CI and no leak scan at all (issue #2035). Counting it
    would report covered for a branch in exactly the state this prompt exists
    to surface.
    """
    fake = FakeRun()
    _script_pr_list(fake, [_pr(7, base="feat/parent")])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") == 0


def test_another_owners_fork_pr_does_not_answer_for_ours(monkeypatch) -> None:
    """The head filter matches a bare NAME — `gh pr list --head` documents no
    owner-qualified form — so a DIFFERENT owner's fork carrying an
    identically-named branch would otherwise be counted as our coverage.

    Filtered on the head repo's OWNER, not on `isCrossRepository`: that flag is
    true for any head-repo != base-repo, which in a fork-based clone is every
    legitimate PR the contributor opens.
    """
    fake = FakeRun()
    _script_pr_list(fake, [_pr(7, owner="someone-else")])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") == 0


def test_our_own_fork_pr_still_counts(monkeypatch) -> None:
    """The control for the test above, and the bug it replaced.

    `isCrossRepository` would have excluded this row. In a fork-based clone,
    where gh resolves the base repo to the upstream, EVERY PR the contributor
    opens is cross-repository — so the count would read 0 forever, the prompt
    would fire on every re-push, and it would assert something false: a fork PR
    onto the default branch does run CI and the leak scan.
    """
    fake = FakeRun()
    _script_pr_list(fake, [_pr(7, owner="me")], owner="me")
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") == 1


def test_a_non_main_default_branch_is_honoured(monkeypatch) -> None:
    """Resolved live rather than assumed. Without this, comparing `baseRefName`
    to a hardcoded "main" passes every other test in the file."""
    fake = FakeRun()
    _script_pr_list(fake, [_pr(7, base="develop")], default="develop")
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") == 1


def test_an_empty_list_is_zero_even_when_identity_is_unresolvable(monkeypatch) -> None:
    """No PR has this head — that answer does not depend on who owns the base
    or what the default branch is called.

    Found by audit: resolving identity FIRST made a second gh call load-bearing
    for a result independent of it, and `_gh_timeout` floors at 1.0s once the
    budget drains, so that call is the likelier of the two to fail. The result
    was None — the silent allow — in precisely the state this prompt reports:
    public branch, no PR, no CI, no leak scan.
    """
    fake = FakeRun()
    fake.script(("gh", "pr", "list"), 0, "[]")
    fake.script(("gh", "repo", "view"), 1, "")
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") == 0


def test_the_probes_take_their_timeout_from_the_shared_deadline(monkeypatch) -> None:
    """Per-call caps cannot bound a SEQUENCE; the shared deadline can.

    Asserts the value actually passed to subprocess, because reverting every
    `_gh_timeout(...)` to a flat `timeout=10` otherwise leaves the suite green.
    """
    fake = FakeRun()
    _script_pr_list(fake, [_pr(7)])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    monkeypatch.setattr(gpg, "_merge_deadline", time.monotonic() + 2.0)
    gpg._open_pr_count_for_branch("feat/x")
    assert fake.kwargs, "no subprocess ran"
    for call_args, call_kwargs in zip(fake.calls, fake.kwargs, strict=True):
        assert "timeout" in call_kwargs, call_args
        # Under a 2s deadline every probe must be clamped well below its own
        # 10s cap — that clamping IS the shared budget.
        assert call_kwargs["timeout"] <= 2.0 + 0.5, (call_args, call_kwargs)


def test_an_unresolvable_default_branch_is_none_not_zero(monkeypatch) -> None:
    """Guessing a default here would turn a failed lookup into a confident
    count, the same collapse the None/0 split exists to prevent."""
    fake = FakeRun()
    # A NON-empty list: rows exist but cannot be attributed to a base or an
    # owner, so the question is genuinely unanswerable. (An empty list needs no
    # identity at all — see the test above.)
    fake.script(("gh", "pr", "list"), 0, json.dumps([_pr(7)]))
    fake.script(("gh", "repo", "view"), 1, "")
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") is None


@pytest.mark.parametrize(
    "rc,out",
    [(1, ""), (0, "not json"), (127, "")],
    ids=["gh-error", "bad-json", "gh-missing"],
)
def test_an_unanswerable_lookup_is_none_never_zero(monkeypatch, rc, out) -> None:
    """The whole reason the return type exists. A None that collapsed to 0
    would convert every network blip into a prompt; a 0 that collapsed to None
    would let the unchecked-public-branch state stay silent forever."""
    fake = FakeRun()
    fake.script(("gh", "pr", "list"), rc, out)
    # Script the identity call too. Without it the `bad-json` case was BLIND:
    # the unscripted-argv AssertionError was what produced the None, so
    # replacing `json.loads(...)` with `[]` left the test green and the
    # malformed-payload path was never the thing under test.
    fake.script(("gh", "repo", "view"), 0, "main\nowner/repo\n")
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") is None


def test_a_timeout_is_none(monkeypatch) -> None:
    def boom(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    monkeypatch.setattr(gpg.subprocess, "run", boom)
    assert gpg._open_pr_count_for_branch("feat/x") is None


# ─── _prs_already_containing_head: enrichment only, [] on any failure ────────


def _script_happy_path(fake: FakeRun, pulls_json: str) -> None:
    fake.script(("git", "rev-parse", "HEAD"), 0, "a" * 40 + "\n")
    fake.script(("gh", "repo", "view"), 0, "owner/repo\n")
    fake.script(("gh", "api"), 0, pulls_json)


def test_a_commit_already_in_a_pr_is_named(monkeypatch) -> None:
    fake = FakeRun()
    _script_happy_path(fake, json.dumps([{"number": 1586, "state": "closed"}]))
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._prs_already_containing_head() == [(1586, "closed")]


def test_a_fresh_commit_yields_empty(monkeypatch) -> None:
    fake = FakeRun()
    _script_happy_path(fake, "[]")
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._prs_already_containing_head() == []


@pytest.mark.parametrize("fail_at", ["rev-parse", "repo-view", "api"])
def test_every_failure_shape_degrades_to_empty(monkeypatch, fail_at) -> None:
    """[] on failure is what keeps this enrichment-only: the ask it feeds is
    shown regardless, so the degraded state is the prompt as it was."""
    fake = FakeRun()
    fake.script(
        ("git", "rev-parse", "HEAD"),
        1 if fail_at == "rev-parse" else 0,
        "" if fail_at == "rev-parse" else "a" * 40 + "\n",
    )
    fake.script(
        ("gh", "repo", "view"),
        1 if fail_at == "repo-view" else 0,
        "" if fail_at == "repo-view" else "owner/repo\n",
    )
    fake.script(("gh", "api"), 1 if fail_at == "api" else 0, "[]")
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._prs_already_containing_head() == []


def test_the_listing_is_bounded(monkeypatch) -> None:
    """A commit in many PRs (a long-lived base) must not flood the prompt."""
    fake = FakeRun()
    _script_happy_path(
        fake,
        json.dumps([{"number": i, "state": "open"} for i in range(50)]),
    )
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert len(gpg._prs_already_containing_head()) == 5


# ─── the wiring: what the user actually sees ─────────────────────────────────
#
# The decision site is deep inside the push handler, so these drive the REAL
# entry point with a synthetic payload, the same way the other guard behaviour
# suites do — a wiring test against the helpers alone would pass with the
# helpers never called (the blind-test shape this repo keeps re-learning).


def _run_guard_on_push(monkeypatch, tmp_path, fake: FakeRun, capsys, command="git push"):
    """Drive main() with a push payload from a real repo on `feat/x`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "--quiet", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["checkout", "--quiet", "-b", "feat/x"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], capture_output=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-qm", "seed"],
        capture_output=True,
        timeout=30,
    )

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(repo),
    }
    monkeypatch.setattr(gpg.sys, "stdin", __import__("io").StringIO(json.dumps(payload)))
    rc = gpg.main()
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_a_republished_branch_with_no_open_pr_asks_instead_of_sliding(
    monkeypatch, tmp_path, capsys
) -> None:
    """The re-push relaxation is earned by first-push approval; a PR-less
    public branch does not get to keep it silently."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 0
    doc = json.loads(out)
    decision = doc["hookSpecificOutput"]["permissionDecision"]
    reason = doc["hookSpecificOutput"]["permissionDecisionReason"]
    assert decision == "ask", (rc, out, err)
    assert "NO OPEN PR" in reason and "leak scan" in reason


def test_a_republished_branch_with_an_open_pr_stays_silent(monkeypatch, tmp_path, capsys) -> None:
    """The control: the hygiene ask must not tax the healthy state, or the
    first-push-only relaxation is silently repealed."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 1)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 0
    if out.strip():
        doc = json.loads(out)
        assert doc["hookSpecificOutput"]["permissionDecision"] != "ask", out


def test_an_unanswerable_pr_lookup_keeps_the_silent_allow(monkeypatch, tmp_path, capsys) -> None:
    """None is the status quo, by design: a hygiene prompt must not be
    manufactured by a network blip on an already-approved branch."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: None)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 0
    if out.strip():
        doc = json.loads(out)
        assert doc["hookSpecificOutput"]["permissionDecision"] != "ask", out


def test_a_first_push_of_work_already_in_a_pr_names_that_pr(monkeypatch, tmp_path, capsys) -> None:
    """The duplicate-name detector, at the moment it can still help."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: False)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_prs_already_containing_head", lambda *a, **k: [(1586, "closed")])
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 0
    doc = json.loads(out)
    reason = doc["hookSpecificOutput"]["permissionDecisionReason"]
    assert doc["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "#1586" in reason and "duplicate" in reason


def test_a_first_push_of_fresh_work_asks_plainly(monkeypatch, tmp_path, capsys) -> None:
    """The control for the enrichment: fresh work gets the ordinary prompt,
    with no note about PRs it is not in."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: False)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_prs_already_containing_head", lambda *a, **k: [])
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 0
    doc = json.loads(out)
    reason = doc["hookSpecificOutput"]["permissionDecisionReason"]
    assert doc["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "duplicate" not in reason


def test_a_containment_timeout_degrades_to_empty(monkeypatch) -> None:
    """The exception path, distinctly from the rc-failure path above.

    Found by mutation: `except: raise` survived the rc-failure tests, because a
    scripted non-zero return never enters the except block at all. A timeout
    does — and it is also the realistic shape, since these lookups run inside a
    hook with a wall-clock budget.
    """

    def boom(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    monkeypatch.setattr(gpg.subprocess, "run", boom)
    assert gpg._prs_already_containing_head() == []


def test_a_commit_in_the_same_command_skips_the_duplicate_probe(
    monkeypatch, tmp_path, capsys
) -> None:
    """`git commit -m x && git push` is the ordinary first-publication shape.

    The hook runs BEFORE any of the command executes, so HEAD at this moment is
    the PARENT of the tip that will actually be pushed. A note keyed on that
    HEAD would name a PR for the wrong commit, inside an enrichment whose whole
    purpose is preventing a mistaken identity — so the probe is skipped rather
    than answered wrongly.

    Drives the BARE push form on purpose: `git push -u origin HEAD` resolves the
    branch to the literal "HEAD" and never reaches this block at all, so a test
    written against that form passes without observing anything.
    """
    fake = FakeRun()
    calls = []
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: False)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(
        gpg,
        "_prs_already_containing_head",
        lambda *a, **k: (calls.append(1), [(1586, "closed")])[1],
    )
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(
        monkeypatch, tmp_path, fake, capsys,
        command="git commit -m wip && git push",
    )
    assert rc == 0
    doc = json.loads(out)
    reason = doc["hookSpecificOutput"]["permissionDecisionReason"]
    assert doc["hookSpecificOutput"]["permissionDecision"] == "ask"
    # Both halves: the probe was not run, and nothing about it reached the user.
    assert calls == [], "the duplicate probe ran against a pre-commit HEAD"
    assert "#1586" not in reason and "duplicate" not in reason, reason


def test_the_probe_sees_an_armed_deadline_on_the_push_path(
    monkeypatch, tmp_path, capsys
) -> None:
    """Per-call caps cannot bound a SEQUENCE of probes.

    The lookups on this path run one after another, so three 10s caps permit
    30s. A PreToolUse hook that overruns its registration is SIGKILLed, and a
    killed hook fails OPEN — the tool runs and the gate stack disengages. The
    shared deadline makes each probe fail FAST into its own fail-safe once the
    budget is spent.

    Asserted AT PROBE TIME, not after main() returns: a later merge-gate site
    arms the same global regardless, so an after-the-fact read is green whether
    or not this path ever armed anything.
    """
    fake = FakeRun()
    seen = {}

    def _count(*a, **k):
        seen["deadline"] = gpg._merge_deadline
        return 1  # keep the silent allow; the arming is what is under test

    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", _count)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)
    monkeypatch.setattr(gpg, "_merge_deadline", None)

    before = time.monotonic()
    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 0
    assert "deadline" in seen, "the PR-count probe never ran"
    assert seen["deadline"] is not None, "probe ran with the deadline unarmed"
    # Bounded in both directions: armed by THIS invocation, within the budget.
    assert before <= seen["deadline"] <= time.monotonic() + gpg._MERGE_GATE_BUDGET_S


def test_a_pr_close_in_the_same_command_cancels_the_silent_allow(
    monkeypatch, tmp_path, capsys
) -> None:
    """`gh pr close <n> && git push` reads a state the command is about to void.

    The hook runs before any segment executes, so the count sees the PR still
    open and keeps the re-push allow — then the command closes it and pushes
    into exactly the PR-less, CI-less state the ask exists to report. A count
    cannot see a close that has not happened, so the command's SHAPE has to.
    """
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    # A still-open PR: the ONLY reason to ask here is the close in the command.
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 1)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(
        monkeypatch, tmp_path, fake, capsys,
        command="gh pr close 123 && git push",
    )
    assert rc == 0
    doc = json.loads(out)
    reason = doc["hookSpecificOutput"]["permissionDecisionReason"]
    assert doc["hookSpecificOutput"]["permissionDecision"] == "ask", out
    assert "CLOSES a pull request" in reason, reason


def test_an_ordinary_repush_with_an_open_pr_is_still_silent(
    monkeypatch, tmp_path, capsys
) -> None:
    """The control for the test above: without a close in the command, the
    healthy re-push keeps its silence. Otherwise the new branch would repeal
    the first-push-only relaxation for everyone."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 1)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 0
    if out.strip():
        doc = json.loads(out)
        assert doc["hookSpecificOutput"]["permissionDecision"] != "ask", out


def test_an_explicit_head_push_still_gets_the_duplicate_note(
    monkeypatch, tmp_path, capsys
) -> None:
    """`git push -u origin HEAD` resolves the branch to the literal "HEAD" and
    took the plain-prompt path, so the duplicate detector never ran on the most
    common first-publication form. The enrichment was coupled to the auto-ALLOW
    predicate, which refuses any refspec it cannot vouch for — the right posture
    for an authorization, the wrong one for a note on a prompt shown anyway.
    """
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: False)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_prs_already_containing_head", lambda *a, **k: [(1586, "closed")])
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(
        monkeypatch, tmp_path, fake, capsys,
        command="git push -u origin HEAD",
    )
    assert rc == 0
    doc = json.loads(out)
    reason = doc["hookSpecificOutput"]["permissionDecisionReason"]
    assert doc["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "#1586" in reason and "duplicate" in reason, reason


def test_a_head_to_new_name_push_gets_the_duplicate_note(
    monkeypatch, tmp_path, capsys
) -> None:
    """`HEAD:<new-name>` IS the second-name publication this detector exists to
    catch, stated in one command."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: False)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_prs_already_containing_head", lambda *a, **k: [(1586, "closed")])
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(
        monkeypatch, tmp_path, fake, capsys,
        command="git push origin HEAD:feat/second-name",
    )
    assert rc == 0
    doc = json.loads(out)
    reason = doc["hookSpecificOutput"]["permissionDecisionReason"]
    assert "#1586" in reason, reason


def test_a_push_of_some_other_branch_gets_no_duplicate_note(
    monkeypatch, tmp_path, capsys
) -> None:
    """The control for the widening: the note keys on HEAD, so a push whose
    SOURCE is not HEAD must not carry it — that would name a PR for a commit
    the command is not publishing."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: False)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_prs_already_containing_head", lambda *a, **k: [(1586, "closed")])
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(
        monkeypatch, tmp_path, fake, capsys,
        command="git push origin other-branch:other-branch",
    )
    assert rc == 0
    doc = json.loads(out)
    reason = doc["hookSpecificOutput"]["permissionDecisionReason"]
    assert "#1586" not in reason, reason


def test_a_commit_after_the_push_does_not_silence_the_note(
    monkeypatch, tmp_path, capsys
) -> None:
    """Only a commit BEFORE the push can change the tip it publishes.

    `git push && git commit -m after` was silencing a note that was correct,
    because the scan looked at every segment rather than the preceding ones.
    """
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: False)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_prs_already_containing_head", lambda *a, **k: [(1586, "closed")])
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(
        monkeypatch, tmp_path, fake, capsys,
        command="git push && git commit -m after",
    )
    assert rc == 0
    doc = json.loads(out)
    reason = doc["hookSpecificOutput"]["permissionDecisionReason"]
    assert "#1586" in reason, reason
