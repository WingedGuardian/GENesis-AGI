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

    def script(self, prefix: tuple[str, ...], returncode: int, stdout: str) -> None:
        self.scripts.append((prefix, returncode, stdout))

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        for prefix, rc, out in self.scripts:
            if tuple(args[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(args, rc, out, "")
        raise AssertionError(f"unscripted subprocess: {args!r}")


# ─── _open_pr_count_for_branch: 0 is a measurement, None is an admission ─────


def test_zero_open_prs_is_a_measured_zero(monkeypatch) -> None:
    fake = FakeRun()
    fake.script(("gh", "pr", "list"), 0, "[]")
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") == 0


def test_an_open_pr_is_counted(monkeypatch) -> None:
    fake = FakeRun()
    fake.script(("gh", "pr", "list"), 0, json.dumps([{"number": 7}]))
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") == 1


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


def _run_guard_on_push(monkeypatch, tmp_path, fake: FakeRun, capsys):
    """Drive main() with a `git push` payload from a real repo on `feat/x`."""
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
        "tool_input": {"command": "git push"},
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
