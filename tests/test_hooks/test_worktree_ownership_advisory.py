"""Tests for the worktree ownership hook (claim on first edit, advise on collision).

Two properties matter more than any individual case, and both are asserted
against a REAL subprocess rather than an imported function, because they are
properties of the hook as Claude Code invokes it:

  * it NEVER exits non-zero -- this is advisory by design, and a hook that
    exits 2 denies the tool call. The house rule is that advisory is the
    default and escalating to a block needs a specific, measured reason; this
    has none, so the exit code is part of the contract, not an implementation
    detail.
  * it never crashes a session. Malformed payloads, missing files, an absent
    worktree, an unreadable lock: all exit 0.

The claim path is exercised through the module, since making a real hook believe
it belongs to an arbitrary session process would mean spawning one -- which CI
cannot do. `session_pid_from_ancestry` is therefore patched where the test is
about WHAT IS WRITTEN rather than about how the pid is found; the finding of the
pid has its own coverage in test_worktree_claim.py against the live process tree.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _ROOT / "scripts" / "hooks" / "worktree_ownership_advisory.py"

sys.path.insert(0, str(_ROOT / "scripts" / "hooks"))
_spec = importlib.util.spec_from_file_location("worktree_ownership_advisory", _HOOK)
hook = importlib.util.module_from_spec(_spec)
sys.modules["worktree_ownership_advisory"] = hook
_spec.loader.exec_module(hook)

wc = hook.wc


def P(rule: str, **extra) -> dict:
    """A well-formed ownership payload, built from the module's own namespace.

    Hand-written literals here went stale the moment the payload gained its
    namespace, and two of these tests then passed while asserting a format
    nothing writes. Deriving it removes that failure mode.
    """
    return {"ns": wc.PAYLOAD_NAMESPACE, "v": 1, "rule": rule, **extra}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", "-b", "main")
    _git(root, "config", "user.email", "probe@example.invalid")
    _git(root, "config", "user.name", "Probe")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "--quiet", "-m", "seed")
    wt = tmp_path / "wt"
    _git(root, "worktree", "add", "--quiet", "-b", "feature/x", str(wt))
    (wt / "target.py").write_text("x = 1\n")
    return wt


def payload(path: Path, *, sid: str = "test-session-id") -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "session_id": sid,
        "tool_input": {"file_path": str(path)},
    }


def run_hook(flag: str, data: dict) -> subprocess.CompletedProcess:
    """Invoke the hook the way Claude Code does: JSON on stdin, flag in argv."""
    return subprocess.run(
        [sys.executable, str(_HOOK), flag],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        timeout=120,
    )


# ─── the contract: never block, never crash ─────────────────────────────────


@pytest.mark.parametrize("flag", ["--advise", "--claim"])
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"tool_input": {}},
        {"tool_input": {"file_path": ""}},
        {"tool_input": {"file_path": "/nonexistent/nowhere/file.py"}},
        {"tool_input": "not-a-dict"},
        {"tool_input": {"file_path": 12345}},
    ],
    ids=["empty", "no-path", "blank-path", "missing-path", "bad-input", "non-string"],
)
def test_a_malformed_payload_never_blocks_the_tool_call(flag, data) -> None:
    """Exit 0 is the contract, not a detail: a non-zero exit DENIES the edit."""
    result = run_hook(flag, data)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("flag", ["--advise", "--claim"])
def test_no_flag_and_unknown_flag_do_nothing(flag) -> None:
    result = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps({"tool_input": {"file_path": "/tmp/x"}}),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_an_edit_in_the_main_checkout_is_ignored(tmp_path: Path) -> None:
    """The main tree is never claimed. It is not a linked worktree, so this falls
    out of the geometry rather than needing a name check."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    f = repo / "a.py"
    f.write_text("x\n")
    result = run_hook("--claim", payload(f))
    assert result.returncode == 0
    assert not (repo / ".git" / "locked").exists()


# ─── claiming ───────────────────────────────────────────────────────────────


def test_the_first_edit_into_an_unclaimed_worktree_claims_it(worktree: Path, monkeypatch) -> None:
    """Claim on first EDIT, not only on creation.

    ~200 worktrees already exist and sessions mostly ADOPT one rather than create
    it, so a claim taken only at `git worktree add` would be absent in the common
    case -- and an advisory that can never fire is worse than none, because it
    reads as coverage.
    """
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: 4242)
    monkeypatch.setattr(wc, "proc_starttime", lambda pid: 99)

    assert wc.read_lock(worktree) is None
    hook._claim(payload(worktree / "target.py", sid="abc123"))

    lock = wc.read_lock(worktree)
    assert lock is not None
    assert lock.foreign is False
    assert lock.payload == P("claim", pid=4242, start=99, sid="abc123")


def test_an_already_locked_worktree_skips_the_ancestry_walk_entirely(
    worktree: Path, monkeypatch
) -> None:
    """The existing lock survives, AND no work is done to discover that.

    Asserting only "the lock is unchanged" is VACUOUS here, and measurement says
    so: deleting this hook's early return left the whole suite green, because
    ``lock_worktree`` refuses a second lock on its own. The early return is not
    the protection -- it is what keeps an already-answered question off a hook
    path, by skipping the /proc ancestry walk that ``build_payload`` performs on
    every Edit and Write. So the walk is what the spy counts.
    """
    _git(worktree, "worktree", "lock", "--reason", "do not touch", str(worktree))

    walks: list[int] = []
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: (walks.append(1), 4242)[1])
    monkeypatch.setattr(wc, "proc_starttime", lambda pid: 99)

    hook._claim(payload(worktree / "target.py"))

    assert walks == [], "an already-locked worktree must not cost an ancestry walk"
    lock = wc.read_lock(worktree)
    assert lock.foreign is True
    assert lock.raw == "do not touch"


def test_no_resolvable_session_writes_no_lock(worktree: Path, monkeypatch) -> None:
    """A claim with no pid has no release condition, and a lock nobody can decide
    to remove pins the worktree against the reaper forever. Writing nothing is
    correct; writing a conditionless lock is the failure."""
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: None)
    hook._claim(payload(worktree / "target.py"))
    assert wc.read_lock(worktree) is None


def test_a_rejected_session_id_still_produces_a_usable_claim(worktree: Path, monkeypatch) -> None:
    """The sid is cosmetic; the pid is what releases the lock.

    A session id is echoed into a reason a human reads, so a malformed one is
    refused rather than escaped -- but refusing it must not cost the claim, which
    is still fully decidable from pid and start time.
    """
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: 4242)
    monkeypatch.setattr(wc, "proc_starttime", lambda pid: 99)
    hook._claim(payload(worktree / "target.py", sid="../../etc/passwd"))
    lock = wc.read_lock(worktree)
    assert lock is not None
    assert "sid" not in lock.payload
    assert lock.payload["pid"] == 4242


# ─── advising ───────────────────────────────────────────────────────────────


def _claim_for(worktree: Path, pid: int, start: int, sid: str = "otherses") -> None:
    reason = wc.format_reason(P("claim", pid=pid, start=start, sid=sid))
    _git(worktree, "worktree", "lock", "--reason", reason, str(worktree))


def test_editing_a_worktree_a_live_session_holds_warns_without_blocking(
    worktree: Path, monkeypatch, capsys
) -> None:
    _claim_for(worktree, 4242, 99)
    monkeypatch.setattr(wc, "pid_is_live_session", lambda pid, start=None: True)
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: 777)

    assert hook._advise(payload(worktree / "target.py")) == 0
    err = capsys.readouterr().err
    assert "claimed by session otherses" in err
    assert "4242" in err
    assert "Not blocked" in err


def test_our_own_claim_is_silent(worktree: Path, monkeypatch, capsys) -> None:
    """Warning a session about its own worktree is noise, and noise is what gets
    an advisory ignored."""
    _claim_for(worktree, 4242, 99)
    monkeypatch.setattr(wc, "pid_is_live_session", lambda pid, start=None: True)
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: 4242)

    assert hook._advise(payload(worktree / "target.py")) == 0
    assert capsys.readouterr().err == ""


def test_a_stale_claim_is_silent(worktree: Path, monkeypatch, capsys) -> None:
    """The holder is gone; the sweep will release it. Warning about a dead
    session's claim would train the reader to ignore the real ones."""
    _claim_for(worktree, 4242, 99)
    monkeypatch.setattr(wc, "pid_is_live_session", lambda pid, start=None: False)
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: 777)

    assert hook._advise(payload(worktree / "target.py")) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "reason",
    ["do not touch", "claude agent agent-abc (pid 1 start 2)", '{"v": 1, "rule": "dirty"}'],
)
def test_a_non_claim_lock_does_not_warn(worktree: Path, monkeypatch, capsys, reason) -> None:
    """Only a `claim` names a session to collide with. A foreign lock, or our own
    `dirty` lock, says nothing about who is working here."""
    _git(worktree, "worktree", "lock", "--reason", reason, str(worktree))
    monkeypatch.setattr(wc, "pid_is_live_session", lambda pid, start=None: True)
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: 777)

    assert hook._advise(payload(worktree / "target.py")) == 0
    assert capsys.readouterr().err == ""


def test_an_unclaimed_worktree_does_not_warn(worktree: Path, capsys) -> None:
    assert hook._advise(payload(worktree / "target.py")) == 0
    assert capsys.readouterr().err == ""


def test_a_notebook_edit_resolves_its_own_path_field(worktree: Path, monkeypatch) -> None:
    """NotebookEdit names the file `notebook_path`, not `file_path`. Without this
    the hook silently does nothing for every notebook edit."""
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: 4242)
    monkeypatch.setattr(wc, "proc_starttime", lambda pid: 99)
    nb = worktree / "analysis.ipynb"
    nb.write_text("{}")
    hook._claim(
        {"tool_name": "NotebookEdit", "session_id": "s", "tool_input": {"notebook_path": str(nb)}}
    )
    assert wc.read_lock(worktree) is not None


# ─── the off switch ─────────────────────────────────────────────────────────


def test_mode_off_claims_nothing_and_warns_about_nothing(
    worktree: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("GENESIS_WORKTREE_OWNERSHIP", "1")
    assert wc.effective_mode() == "off"

    monkeypatch.setattr(sys, "argv", [str(_HOOK), "--claim"])
    monkeypatch.setattr(hook, "read_payload", lambda: payload(worktree / "target.py"))
    assert hook.main() == 0
    assert wc.read_lock(worktree) is None, "mode=off must take no lock"

    _claim_for(worktree, 4242, 99)
    monkeypatch.setattr(wc, "pid_is_live_session", lambda pid, start=None: True)
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: 777)
    monkeypatch.setattr(sys, "argv", [str(_HOOK), "--advise"])
    assert hook.main() == 0
    assert capsys.readouterr().err == "", "mode=off must warn about nothing"


def test_the_hook_is_registered_for_both_modes() -> None:
    """A hook nothing invokes is inert, and the settings file is the only thing
    that invokes it -- so the wiring is asserted here rather than assumed."""
    settings = json.loads((_ROOT / ".claude" / "settings.json").read_text())
    pre = [
        h["command"]
        for e in settings["hooks"]["PreToolUse"]
        for h in e["hooks"]
        if "worktree_ownership_advisory" in h.get("command", "")
    ]
    post = [
        h["command"]
        for e in settings["hooks"]["PostToolUse"]
        for h in e["hooks"]
        if "worktree_ownership_advisory" in h.get("command", "")
    ]
    assert len(pre) == 1 and pre[0].endswith("--advise")
    assert len(post) == 1 and post[0].endswith("--claim")

    matchers = {
        e.get("matcher")
        for event in ("PreToolUse", "PostToolUse")
        for e in settings["hooks"][event]
        if any("worktree_ownership_advisory" in h.get("command", "") for h in e["hooks"])
    }
    assert matchers == {"Edit|Write|MultiEdit|NotebookEdit"}
