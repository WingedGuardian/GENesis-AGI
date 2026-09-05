"""A dispatched session must not be able to abandon reviewed work unilaterally.

MEASURED on the guard as it stood 2026-09-03, with GENESIS_CC_SESSION=1 — i.e.
exactly what a Genesis-dispatched session gets:

    gh pr merge <N> --squash --admin   ->  BLOCK
    gh pr close <N>                    ->  ALLOW      <- the hole
    gh pr comment <N> --body ...       ->  ALLOW

The two worst cases were not symmetric. Merging without user approval is already
structurally impossible; ABANDONING the work was free.

WHY IT MATTERS MORE NOW. The review-round terminal (FINAL_ROUND_CAP) hard-blocks
the commit at seven external rounds and no self-issued sigil clears it. That is
deliberate — the disposition at that point is a user decision, and a reviewing
session cannot make it (it cannot even rebuild: a rebuild is a different builder
session). But it leaves an autonomous session under a make-progress mandate with
exactly ONE unilateral action that resolves the situation: close the PR. The
terminal removed the escape and left the fire exit open.

SCOPE. Closing a PR is a USER decision either way (the session is not the
user; presence is not consent — the #1579 lesson, where a FOREGROUND session
closed a reviewed PR without asking):
  * a FOREGROUND session must ASK — a native approve/deny dialog it cannot
    self-satisfy, the same footing as the push / pr-open gates;
  * a DISPATCHED session is HARD-DENIED (no human to ask) and pointed at
    outreach_send_and_wait;
  * the `gh api ... -X PATCH .../pulls/N ... state=closed` REST form rides the
    same arm (the documented walk-past of a subcommand-only gate);
  * `gh issue close` is NOT covered. Closing an issue is not abandoning reviewed
    work, and an autonomous filer legitimately closes its own issues. Widening
    to it would be a policy change, not a safety fix;
  * `gh pr comment` / `gh pr edit` stay allowed. A session must still be able to
    explain itself and label its work — the point is to stop it DECIDING, not to
    stop it speaking.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUARD = _REPO_ROOT / "scripts" / "hooks" / "git_push_guard.py"

# Same isolation contract as test_guard_ansic_fail_closed: an ALLOWLIST, so an
# ambient variable cannot change a verdict. GENESIS_CC_SESSION in particular is
# never inherited — forwarding it once made a whole suite test its CALLER's mode.
_NEUTRAL_ENV = frozenset({"PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONHASHSEED"})
_SANDBOX_HOME_TD = tempfile.TemporaryDirectory(prefix="pr-close-gate-home-")
_SANDBOX_HOME = _SANDBOX_HOME_TD.name


def _child_env(
    cwd: str, dispatched: str | None, extra_env: dict[str, str] | None = None
) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _NEUTRAL_ENV}
    env["HOME"] = _SANDBOX_HOME
    env["GENESIS_HOME"] = str(Path(_SANDBOX_HOME) / ".genesis")
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CEILING_DIRECTORIES"] = str(Path(cwd).parent)
    if dispatched is not None:
        env["GENESIS_CC_SESSION"] = dispatched
    if extra_env:
        env.update(extra_env)
    return env


def _run(
    cmd: str,
    cwd: str,
    *,
    dispatched: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "cwd": cwd,
            "session_id": "test",
        }
    )
    return subprocess.run(
        [sys.executable, str(_GUARD)],
        input=payload,
        cwd=cwd,
        env=_child_env(cwd, dispatched, extra_env),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _verdict(r: subprocess.CompletedProcess) -> str:
    if '"ask"' in (r.stdout or ""):
        return "ask"
    return "block" if r.returncode == 2 else "allow"


@pytest.fixture
def repo(tmp_path: Path) -> str:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=r, check=True)
    (r / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=r, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "feature/x"], cwd=r, check=True)
    return str(r)


# ── the hole this closes ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr close 1680",
        "gh pr close 1680 --comment 'no longer needed'",
        "gh pr close https://github.com/o/r/pull/1680",
        "gh  pr   close  1680",
        "gh pr close 1680 --delete-branch",
    ],
)
def test_dispatched_session_cannot_close_a_pr(repo, cmd):
    """Abandoning reviewed work is a user decision, and there is no user here."""
    r = _run(cmd, repo, dispatched="1")
    assert _verdict(r) == "block", f"{cmd!r} -> {_verdict(r)}: {r.stdout}{r.stderr}"


def test_the_refusal_names_a_route_that_actually_works(repo):
    """A refusal with no route is a wall, and a wall teaches the wrong lesson.

    The session must be told it can ASK — otherwise the only remaining move is
    to do nothing, and an autonomous session that silently stalls on a blocked
    PR is barely better than one that closes it.
    """
    r = _run("gh pr close 1680", repo, dispatched="1")
    combined = (r.stdout or "") + (r.stderr or "")
    assert "outreach_send_and_wait" in combined, combined


# ── the controls: this must not become a wall for everything else ─────────


def test_foreground_session_close_asks(repo):
    """THE #1579 fix. A foreground close is not free — the session is not the
    user, and presence is not consent. It fires a native ask the session cannot
    self-satisfy. (Mutation control: delete the ask and this reverts to `allow`.)
    """
    r = _run("gh pr close 1680", repo, dispatched=None)
    assert _verdict(r) == "ask", f"{_verdict(r)}: {r.stdout}{r.stderr}"
    reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "your decision" in reason.lower(), reason
    assert "#1579" in reason, reason  # the exception is legibly the user's


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr comment 1680 --body 'here is why this is stuck'",
        "gh pr edit 1680 --add-label needs-decision",
        "gh pr view 1680",
        "gh pr list",
        "gh issue close 42",
        "gh pr reopen 1680",
    ],
)
def test_dispatched_session_keeps_every_non_disposition_action(repo, cmd):
    """Stop it DECIDING, not speaking.

    `gh issue close` is in this list deliberately — closing an issue is not
    abandoning reviewed work, and an autonomous filer legitimately closes its
    own. Covering it would be a policy change rather than a safety fix.
    """
    r = _run(cmd, repo, dispatched="1")
    assert _verdict(r) == "allow", f"{cmd!r} -> {_verdict(r)}: {r.stdout}{r.stderr}"


def test_close_buried_in_a_compound_still_blocks(repo):
    """One segment deciding to abandon is the whole command deciding to abandon."""
    r = _run("git status && gh pr close 1680", repo, dispatched="1")
    assert _verdict(r) == "block", f"{_verdict(r)}: {r.stdout}{r.stderr}"


def test_the_word_close_in_prose_does_not_block(repo):
    """A commit message that MENTIONS closing a PR is not closing a PR.

    The guard's own history is full of substring matches that refused ordinary
    text; this is the shape that would do it here.
    """
    r = _run(
        "git commit -m 'document why we close a pr at the review terminal'",
        repo,
        dispatched="1",
    )
    assert _verdict(r) != "block", f"{r.stdout}{r.stderr}"


# ── the gh api PATCH walk-past ────────────────────────────────────────────


def test_foreground_gh_api_patch_close_asks(repo):
    r = _run(
        "gh api repos/o/r/pulls/1680 -X PATCH -f state=closed", repo, dispatched=None
    )
    assert _verdict(r) == "ask", f"{_verdict(r)}: {r.stdout}{r.stderr}"


def test_dispatched_gh_api_patch_close_blocks(repo):
    r = _run(
        "gh api repos/o/r/pulls/1680 -X PATCH -f state=closed", repo, dispatched="1"
    )
    assert _verdict(r) == "block", f"{_verdict(r)}: {r.stdout}{r.stderr}"


def test_gh_api_patch_that_only_edits_title_is_not_a_close(repo):
    """A PATCH with no state=closed and no opaque body is not a close, so the
    close arm must not fire on it (control against over-blocking PR edits)."""
    r = _run(
        "gh api repos/o/r/pulls/1680 -X PATCH -f title=renamed", repo, dispatched="1"
    )
    assert _verdict(r) == "allow", f"{_verdict(r)}: {r.stdout}{r.stderr}"


# ── the rebuild-commitment, scoped to terminal-reached closes ─────────────

_SEVEN_ROUNDS = "\n".join(
    f'{{"login": "chatgpt-codex-connector[bot]", "commit_id": "c{i}"}}'
    for i in range(7)
)


def test_terminal_reached_close_blocks_without_a_commitment(repo, tmp_path):
    """A PR at the review terminal cannot be closed until a rebuild-commitment
    is on file — closing failed work is 'back to the drawing board', not the
    end. (Mutation control: drop the terminal check and this becomes `ask`.)"""
    r = _run(
        "gh pr close 1579",
        repo,
        dispatched=None,
        extra_env={
            "_TEST_GH_CODEX_REVIEWS": _SEVEN_ROUNDS,
            "_TEST_CLOSE_COMMITMENT_DIR": str(tmp_path / "commitments"),
        },
    )
    assert _verdict(r) == "block", f"{_verdict(r)}: {r.stdout}{r.stderr}"
    assert "rebuild" in (r.stderr or "").lower(), r.stderr


def test_terminal_reached_close_asks_with_a_valid_commitment(repo, tmp_path):
    d = tmp_path / "commitments"
    d.mkdir()
    # repo is unresolved for a bare `gh pr close 1579`, so the file is 1579.txt.
    (d / "1579.txt").write_text(
        "Rebuild commitment for PR #1579: the slot-door heal failed seven review "
        "rounds on the decision-time-vs-action-time staleness class. Rebuild from "
        "the recorded failure classes rather than re-patching the closed branch. "
        "Follow-up: f1f2546dadb34e1ca62ab6395368fba7\n"
    )
    r = _run(
        "gh pr close 1579",
        repo,
        dispatched=None,
        extra_env={
            "_TEST_GH_CODEX_REVIEWS": _SEVEN_ROUNDS,
            "_TEST_CLOSE_COMMITMENT_DIR": str(d),
        },
    )
    assert _verdict(r) == "ask", f"{_verdict(r)}: {r.stdout}{r.stderr}"
    reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "rebuild" in reason.lower(), reason  # the commitment is quoted back


def test_below_terminal_close_asks_without_needing_a_commitment(repo, tmp_path):
    """A routine close (superseded, duplicate, dropped experiment) needs only the
    approval ask — no rebuild artifact. Three rounds is below the terminal."""
    three = "\n".join(
        f'{{"login": "chatgpt-codex-connector[bot]", "commit_id": "c{i}"}}'
        for i in range(3)
    )
    r = _run(
        "gh pr close 1579",
        repo,
        dispatched=None,
        extra_env={
            "_TEST_GH_CODEX_REVIEWS": three,
            "_TEST_CLOSE_COMMITMENT_DIR": str(tmp_path / "empty"),
        },
    )
    assert _verdict(r) == "ask", f"{_verdict(r)}: {r.stdout}{r.stderr}"


# ── compound-collapse must include close ──────────────────────────────────


def test_close_sharing_a_gate_with_a_push_blocks(repo):
    """A foreground `gh pr close && git push` would collapse to ONE ask; approving
    it would run both. The compound must be refused so each is gated separately."""
    r = _run("gh pr close 1680 && git push", repo, dispatched=None)
    assert _verdict(r) == "block", f"{_verdict(r)}: {r.stdout}{r.stderr}"


def test_two_closes_in_one_command_block(repo):
    r = _run("gh pr close 1 && gh pr close 2", repo, dispatched=None)
    assert _verdict(r) == "block", f"{_verdict(r)}: {r.stdout}{r.stderr}"


# ── the known-positive control: the exact #1579 incident invocation ───────


def test_known_positive_the_exact_1579_invocation(repo):
    """The command that started all of this — `gh pr close 1579` — must be
    ASKED in a foreground session and BLOCKED in a dispatched one. If either
    reverts to `allow`, the gate the incident demanded is not in force."""
    fg = _run("gh pr close 1579", repo, dispatched=None)
    assert _verdict(fg) == "ask", f"foreground {_verdict(fg)}: {fg.stdout}{fg.stderr}"
    bg = _run("gh pr close 1579", repo, dispatched="1")
    assert _verdict(bg) == "block", f"dispatched {_verdict(bg)}: {bg.stdout}{bg.stderr}"


def test_untokenizable_gh_close_is_not_a_silent_allow(repo):
    r"""An ANSI-C-hidden `gh pr close` must not slip. The escaped-quote `$'...'`
    makes the whole command untokenizable, so the parser drops the close segment
    — and the blind-spot net catches it via the gh+close mention: a dispatched
    session is denied, a foreground one is asked, never a silent allow."""
    cmd = "echo $'a\\'b)c' && gh pr close 1680"
    bg = _run(cmd, repo, dispatched="1")
    assert _verdict(bg) == "block", f"dispatched {_verdict(bg)}: {bg.stdout}{bg.stderr}"
    fg = _run(cmd, repo, dispatched=None)
    assert _verdict(fg) == "ask", f"foreground {_verdict(fg)}: {fg.stdout}{fg.stderr}"


# ── the WIDER gh api close surface (review Finding 1) ─────────────────────


def test_dispatched_gh_api_issues_patch_close_blocks(repo):
    """A PR IS an issue in GitHub's model: PATCHing the /issues/N endpoint with
    state=closed closes the underlying PR. It must be gated like the /pulls form,
    or it is a silent walk-past for a dispatched session."""
    r = _run(
        "gh api repos/o/r/issues/1680 -X PATCH -f state=closed", repo, dispatched="1"
    )
    assert _verdict(r) == "block", f"{_verdict(r)}: {r.stdout}{r.stderr}"


def test_foreground_gh_api_graphql_close_asks(repo):
    """The GraphQL closePullRequest mutation has no /pulls path and is POST, not
    PATCH — it must still be caught by operation name, not slip as a silent allow."""
    r = _run(
        "gh api graphql -f query='mutation{ closePullRequest(input:{pullRequestId:\"x\"}){ clientMutationId } }'",
        repo,
        dispatched=None,
    )
    assert _verdict(r) == "ask", f"{_verdict(r)}: {r.stdout}{r.stderr}"


@pytest.mark.parametrize(
    "cmd",
    [
        "gh api repos/o/r/pulls/1 -X PATCH -F state=closed",       # typed field
        "gh api repos/o/r/pulls/1 -X PATCH -f state=CLOSED",       # case-insensitive
        "gh api repos/o/r/pulls/1 --method PATCH -f state=closed",  # long method flag
        "gh api repos/o/r/pulls/1 -XPATCH -f state=closed",        # glued -X
        "gh api repos/o/r/pulls/1 -X PATCH --input body.json",     # opaque body
    ],
)
def test_gh_api_patch_close_forms_are_all_gated(repo, cmd):
    """Field-form and method-form variants of the PATCH close all ask (fg)."""
    r = _run(cmd, repo, dispatched=None)
    assert _verdict(r) == "ask", f"{cmd!r} -> {_verdict(r)}: {r.stdout}{r.stderr}"
