"""The push guard's PR-adjacency hygiene: no silent public branch without a PR.

Two mechanisms, both measured on this repo before they were built:

  * A public branch with no open PR runs no CI and no leak-detector at all
    (ci.yml triggers on pull_request), which is the exact state the repo's
    publish rule exists to prevent.

The design constraint these tests pin is the FAIL DIRECTION, per finding:

  * `_open_pr_count_for_branch` distinguishes 0 (measured: no PR) from None
    (unanswerable). Only a measured 0 downgrades the silent re-push allow to an
    ask; None keeps the status quo, because this is a hygiene prompt on an
    already-approved branch, not a security boundary, and a network blip must
    not manufacture prompts.
  * The count refuses to answer rather than guess: a FULL result window is a
    truncated read, and a push destination that is not the repository gh
    resolved is a different question entirely. Both return None.

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
    fake: FakeRun,
    rows: list[dict],
    default: str = "main",
    owner: str = "owner",
    host: str = "github.com",
) -> None:
    fake.script(("gh", "pr", "list"), 0, json.dumps(rows))
    fake.script(
        ("gh", "repo", "view"), 0,
        f"{default}\n{owner}/repo\nhttps://{host}/{owner}/repo\n",
    )


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
    fake.script(("gh", "repo", "view"), 0, "main\nowner/repo\nhttps://github.com/owner/repo\n")
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") is None


def test_a_timeout_is_none(monkeypatch) -> None:
    def boom(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    monkeypatch.setattr(gpg.subprocess, "run", boom)
    assert gpg._open_pr_count_for_branch("feat/x") is None




def _run_guard_on_push(
    monkeypatch, tmp_path, fake: FakeRun, capsys, command="git push", git_config=()
):
    """Drive main() with a push payload from a real repo on `feat/x`.

    ``git_config`` is extra ``git config`` argument lists applied after init,
    for tests that need real remotes rather than a stubbed URL set."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "--quiet", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["checkout", "--quiet", "-b", "feat/x"],
        *[["config", *kv] for kv in git_config],
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


# ─── _no_pr_block_applies: the block is scoped, and fails toward NOT blocking ──


PUBLIC_URL = "https://github.com/owner/repo"


def test_the_block_applies_on_the_configured_public_repo(monkeypatch) -> None:
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/repo")
    assert gpg._no_pr_block_applies({PUBLIC_URL}) is True
    # Every spelling of the same remote is the same destination.
    assert gpg._no_pr_block_applies({"git@github.com:owner/repo.git"}) is True


@pytest.mark.parametrize(
    ("urls", "why"),
    [
        ({"https://github.com/owner/genesis-backups"}, "the private backups repo"),
        ({"https://github.com/owner/GENesis-Voice"}, "the voice repo"),
        ({"https://github.com/someone-else/repo"}, "someone else's fork"),
        ({"https://github.com/owner/repo-other"}, "a same-owner sibling repo"),
        ({"https://ghe.example.com/owner/repo"}, "the same name on another host"),
        ({PUBLIC_URL, "https://github.com/owner/private-fork"}, "a fan-out that is not ALL public"),
    ],
)
def test_the_block_does_not_apply_off_the_public_repo(monkeypatch, urls, why: str) -> None:
    """The hook is registered on `Bash` with no cwd scoping, so it fires in
    whatever repo a session has wandered into. "This branch has no open PR" is
    an ordinary state everywhere except the one repo whose `ci.yml` and leak
    detector trigger on `pull_request` — blocking elsewhere refuses routine work
    for a reason that does not exist there."""
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/repo")
    assert gpg._no_pr_block_applies(urls) is False, why


def test_the_scope_is_the_push_destination_not_the_checkout(monkeypatch) -> None:
    """A checkout whose gh repo IS the public repo, pushing to a private fork
    (`branch.<name>.pushRemote` / `remote.pushDefault`), must not be blocked:
    it is the destination that decides whether CI ever sees the branch. The
    decision must not consult gh at all — patch it to explode."""
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/repo")

    def _boom(*a, **k):
        raise AssertionError("the scope decision must not call gh")

    monkeypatch.setattr(gpg, "_base_repo_identity", _boom)
    assert gpg._no_pr_block_applies({"https://github.com/owner/private-fork"}) is False
    assert gpg._no_pr_block_applies({PUBLIC_URL}) is True


def test_an_undeterminable_canonical_repo_does_not_block(monkeypatch) -> None:
    """THE fail-direction test, and the one that is the OPPOSITE of the sibling
    gate. `_scheduled_gate_applies` enforces when the repo is unknown, because
    skipping would be an evasion path on a MERGE a human is standing over. This
    refuses a PUSH, in every session, for a hygiene property — so an unreadable
    config must not wedge ordinary work everywhere with no way through."""
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "")
    assert gpg._no_pr_block_applies({PUBLIC_URL}) is False


def test_unresolvable_push_urls_do_not_block(monkeypatch) -> None:
    """Same direction, other input: no push URL could be resolved."""
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/repo")
    assert gpg._no_pr_block_applies(set()) is False


@pytest.fixture
def on_the_public_repo(monkeypatch):
    """Configure the PUBLIC repo through the real seam. The scoping comparison
    runs for real; a test that wants the block must also push to PUBLIC_URL."""
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/repo")


def test_a_republished_branch_with_no_open_pr_is_BLOCKED(
    monkeypatch, tmp_path, capsys, on_the_public_repo
) -> None:
    """The re-push relaxation is earned by first-push approval; a PR-less
    public branch does not get to keep it.

    This asserted an ASK until 2026-09-25. PR-less public branches kept
    accumulating under the ask — 2 of 60 on 2026-09-25, both after the previous
    cleanup — so on the public repo it is now a block. A dispatched session never
    reaches this arm: `_is_dispatched()` denies every non-force push earlier.
    """
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: {PUBLIC_URL})
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 2, (rc, out, err)
    assert "NO OPEN PR" in err and "leak detector" in err
    assert "gh pr create" in err, "the block must name the way out, not just refuse"
    assert not out.strip(), (
        "a block writes to stderr and returns 2; emitting hook JSON as well would "
        "hand Claude Code a permission decision that contradicts the exit code"
    )


@pytest.mark.parametrize(
    "command",
    [
        "git push https://github.com/owner/repo HEAD:refs/heads/feat/x",
        "git push https://github.com/owner/repo.git feat/x",
    ],
)
def test_a_raw_url_republish_with_no_open_pr_is_BLOCKED(
    monkeypatch, tmp_path, capsys, on_the_public_repo, command: str
) -> None:
    """A raw URL is not a configured remote, so `git remote get-url` cannot
    resolve it. The destination must still be read from the URL itself, or the
    open-PR lookup sees no target, answers None, and the public no-PR block
    never runs for exactly the spelling that skips the remote."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    # Realistic: a raw URL names no remote, so the remote lookup is empty.
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(
        gpg,
        "_open_pr_count_for_branch",
        lambda *a, push_urls=None, **k: 0 if push_urls else None,
    )
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys, command=command)
    assert rc == 2, (rc, out, err)
    assert "NO OPEN PR" in err


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


def test_an_unanswerable_pr_lookup_keeps_the_silent_allow(
    monkeypatch, tmp_path, capsys, on_the_public_repo
) -> None:
    """None is the status quo, by design: a hygiene prompt must not be
    manufactured by a network blip on an already-approved branch."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: {PUBLIC_URL})
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: None)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 0
    if out.strip():
        doc = json.loads(out)
        assert doc["hookSpecificOutput"]["permissionDecision"] != "ask", out


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


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --head feat/x --title t --body b && git push",
        "git push && gh pr create --title t --body b",
        "git push && gh pr create --dry-run --title t --body b",
        "git push && gh pr create --head=feat/x --title t --body b",
    ],
)
def test_a_same_command_pr_create_does_NOT_exempt(
    monkeypatch, tmp_path, capsys, on_the_public_repo, command: str
) -> None:
    """There is no create exemption, and these rows are why.

    A create in the same command does supply the PR the block demands — that
    premise is true — but WHETHER it will is not decidable from argv. Two rounds
    of narrowing proved it: `bool(create_segs)` exempted a create for a different
    branch, and binding it to head/base/repo then drew four more findings — a
    glued flag, a `--dry-run` that creates nothing, a create that FAILS and leaves
    the `&&` push to run anyway, and a configured merge base. Each fix created the
    surface for the next.

    So the compound blocks. The rows below include the exact spellings that defeated
    the previous design, as the regression record: if someone reintroduces an
    exemption, these are the cases it has to survive, and the simplest way to
    survive them is not to have one.

    The cost is close to nothing, which the sibling test measures directly: a FIRST
    push is not a republish, so the ordinary push-then-create flow never reaches
    this arm.
    """
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: {PUBLIC_URL})
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys, command=command)
    assert rc == 2, (command, rc, out, err)
    assert "NO OPEN PR" in err, (command, err)


def test_the_ordinary_push_then_create_flow_is_UNAFFECTED(
    monkeypatch, tmp_path, capsys, on_the_public_repo
) -> None:
    """The cost measurement for the test above, and the reason dropping the
    exemption is cheap rather than a regression.

    The arm only runs when `push_allow_reason` is set, i.e. the branch is already
    on the remote. A FIRST push is not a republish, so the prescribed flow —
    publish a new branch, then open its PR — never reaches the block at all,
    whatever the create looks like.
    """
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: False)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: {PUBLIC_URL})
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(
        monkeypatch, tmp_path, fake, capsys,
        command="git push -u origin HEAD && gh pr create --title t --body b",
    )
    assert rc != 2, (rc, out, err)
    assert "NO OPEN PR" not in err, err


def test_the_block_does_not_fire_off_the_public_repo_end_to_end(
    monkeypatch, tmp_path, capsys
) -> None:
    """The scoping decision driven through main(), not just the predicate.

    Same command and same measured zero as the blocking test; only the push
    destination differs. Without this, a unit test of `_no_pr_block_applies` could pass
    while nothing wired it into the decision."""
    fake = FakeRun()
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/repo")
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(
        gpg, "_remote_push_urls",
        lambda *a, **k: {"https://github.com/owner/genesis-backups"},
    )
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 0, (rc, out, err)
    assert "BLOCKED" not in err, err
    # Off the public repo the pre-existing ASK is kept, unchanged — the cut
    # changes behaviour on the public repo only.
    doc = json.loads(out)
    assert doc["hookSpecificOutput"]["permissionDecision"] == "ask", out
    assert "NO OPEN PR" in doc["hookSpecificOutput"]["permissionDecisionReason"], out


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


# ─── what replaced the enrichment ───────────────────────────────────────────


@pytest.mark.parametrize("cmd", ["git push -n", "git push --dry-run", "git push -un"])
def test_a_dry_run_keeps_its_silence(
    monkeypatch, tmp_path, capsys, cmd, on_the_public_repo
) -> None:
    """A dry run publishes NOTHING, so it cannot create the unchecked-branch
    state this prompt reports.

    Both spellings are accepted by the plain-current-branch predicate, so they
    reach the check; `-n` also travels inside a short bundle. Prompting here is
    pure friction on an inspection command.
    """
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: {PUBLIC_URL})
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys, command=cmd)
    assert rc == 0
    if out.strip():
        doc = json.loads(out)
        assert doc["hookSpecificOutput"]["permissionDecision"] != "ask", out


def test_a_real_push_is_still_stopped(monkeypatch, tmp_path, capsys, on_the_public_repo) -> None:
    """The control for the dry-run skip: without it the block must still fire,
    or the exclusion has silently disabled the whole check."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: {PUBLIC_URL})
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 2, (rc, out, err)
    assert "NO OPEN PR" in err


def test_a_full_result_window_is_unanswerable_not_absent(monkeypatch) -> None:
    """A response that FILLS the window is a truncated read.

    The qualifying request can sit past the cap, and the base/owner filters
    would then sum to a confident 0 — the value that downgrades the allow to an
    ask. This is the repo's own truncated-listing rule applied to a gate input.
    """
    fake = FakeRun()
    rows = [_pr(n, base="other") for n in range(gpg._PR_LIST_WINDOW)]
    _script_pr_list(fake, rows)
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") is None


def test_a_short_window_is_still_counted(monkeypatch) -> None:
    """The control: one row short of the cap is a complete read and must count
    normally, or the guard above has disabled counting altogether."""
    fake = FakeRun()
    rows = [_pr(n, base="other") for n in range(gpg._PR_LIST_WINDOW - 1)]
    _script_pr_list(fake, rows)
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x") == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo",
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
    ],
)
def test_a_destination_that_is_the_resolved_repo_is_counted(monkeypatch, url) -> None:
    """Every spelling of the same remote must agree, or the ssh form alone
    would make the count unanswerable forever."""
    fake = FakeRun()
    _script_pr_list(fake, [_pr(7)])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x", push_urls={url}) == 1


def test_a_destination_other_than_the_resolved_repo_is_unanswerable(monkeypatch) -> None:
    """gh answers about the repo IT resolves; the count is about the repo this
    push goes to.

    In a fork workflow (`gh repo set-default upstream`) those differ, and
    trusting the mismatch would report "no open PR" forever for a contributor
    whose requests all live upstream — a permanent false prompt asserting
    something untrue.
    """
    fake = FakeRun()
    _script_pr_list(fake, [_pr(7)])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch(
        "feat/x", push_urls={"https://github.com/someone-else/repo.git"}
    ) is None


def test_a_push_fanning_out_to_two_repos_is_unanswerable(monkeypatch) -> None:
    """ALL rather than ANY: one count cannot answer for two destinations."""
    fake = FakeRun()
    _script_pr_list(fake, [_pr(7)])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch(
        "feat/x",
        push_urls={
            "https://github.com/owner/repo.git",
            "https://github.com/someone-else/repo.git",
        },
    ) is None


def test_a_remote_on_another_host_does_not_match(monkeypatch) -> None:
    """HOST is part of a repository's identity.

    Comparing only the `owner/repo` tail makes an enterprise instance, a mirror,
    or a look-alike host compare EQUAL to the repository gh answered about — and
    the count would then be trusted for a destination it never described. Found
    in review after the tail-only matcher shipped.
    """
    fake = FakeRun()
    _script_pr_list(fake, [_pr(7)])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch(
        "feat/x", push_urls={"https://not-github.example/owner/repo.git"}
    ) is None


def test_an_empty_list_from_the_wrong_repo_is_not_a_measured_zero(monkeypatch) -> None:
    """The destination must gate EVERY answer, the empty one included.

    `gh pr list` asks the repo gh resolves. If the push goes somewhere else, an
    empty result describes a different repository — it is not evidence that THIS
    branch has no request, and returning 0 would turn that non-evidence into the
    value that downgrades a silent allow to an ask.
    """
    fake = FakeRun()
    _script_pr_list(fake, [])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch(
        "feat/x", push_urls={"https://github.com/someone-else/repo.git"}
    ) is None


def test_an_empty_list_from_the_right_repo_is_still_zero(monkeypatch) -> None:
    """The control: verifying the destination must not disable the measurement."""
    fake = FakeRun()
    _script_pr_list(fake, [])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch(
        "feat/x", push_urls={"https://github.com/owner/repo.git"}
    ) == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo/",
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "https://github.com:443/owner/repo",
    ],
)
def test_every_spelling_of_one_remote_compares_equal(monkeypatch, url) -> None:
    """One remote written five ways is one destination.

    If any spelling failed to match, the count would be unanswerable forever for
    anyone whose remote used it — a silent, permanent loss of the check.
    """
    fake = FakeRun()
    _script_pr_list(fake, [_pr(7)])
    monkeypatch.setattr(gpg.subprocess, "run", fake)
    assert gpg._open_pr_count_for_branch("feat/x", push_urls={url}) == 1


def test_an_undeclared_public_repo_keeps_the_ask(monkeypatch, tmp_path, capsys) -> None:
    """No declared public repo: the block cannot scope, so it must not fire —
    but the push must not go SILENT either. It keeps main's ask."""
    fake = FakeRun()
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "")
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: {PUBLIC_URL})
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(monkeypatch, tmp_path, fake, capsys)
    assert rc == 0, (rc, out, err)
    doc = json.loads(out)
    assert doc["hookSpecificOutput"]["permissionDecision"] == "ask", out


@pytest.mark.parametrize(
    ("git_config", "blocked"),
    [
        # Control: the branch pushes to origin, which IS the public repo.
        ((("remote.origin.url", PUBLIC_URL),), True),
        # pushRemote sends it to a private fork: the checkout is public, the
        # destination is not, and the destination decides.
        (
            (
                ("remote.origin.url", PUBLIC_URL),
                ("remote.fork.url", "https://github.com/owner/private-fork"),
                ("branch.feat/x.pushRemote", "fork"),
            ),
            False,
        ),
        # remote.pushDefault does the same for every branch.
        (
            (
                ("remote.origin.url", PUBLIC_URL),
                ("remote.fork.url", "https://github.com/owner/private-fork"),
                ("remote.pushDefault", "fork"),
            ),
            False,
        ),
    ],
)
def test_the_scope_follows_the_real_push_destination(
    monkeypatch, tmp_path, capsys, on_the_public_repo, git_config, blocked
) -> None:
    """Real remotes, real `_remote_push_urls`: nothing between git config and the
    scope decision is stubbed except the PR count. Scoping on origin's URLs
    instead of the destination's turns the fork rows into blocks."""
    fake = FakeRun()
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)

    rc, out, err = _run_guard_on_push(
        monkeypatch, tmp_path, fake, capsys, git_config=git_config
    )
    if blocked:
        assert rc == 2 and "NO OPEN PR" in err, (rc, out, err)
    else:
        assert rc == 0 and "BLOCKED" not in err, (rc, out, err)
