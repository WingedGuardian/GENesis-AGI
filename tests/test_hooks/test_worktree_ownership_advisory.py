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
def worktree(tmp_path: Path, monkeypatch) -> Path:
    """A linked worktree, in a repository this fixture declares to be OURS.

    The declaration is required rather than incidental: ``worktree_root_for``
    refuses a worktree belonging to a different repository, so without it every
    test here would exercise the refusal path instead of the claim path. The
    refusal itself is asserted in test_worktree_claim.py against two real repos.
    """
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
    monkeypatch.setattr(wc, "_our_common_dir", lambda: (root / ".git").resolve())
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

    # THE CHANNEL IS THE POINT. An earlier version printed to stderr and exited
    # 0, and was INERT: per docs/reference/cc-compatibility.md:1093-1099 only
    # SessionStart / UserPromptSubmit / UserPromptExpansion put hook stdout in
    # front of the model, and PreToolUse reaches it ONLY through this JSON
    # envelope. Asserting the prose alone would pass just as well against the
    # dead channel, so the SHAPE is asserted first and the prose second.
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert "additionalContext" not in doc, (
        "a TOP-LEVEL additionalContext is silently discarded by Claude Code; "
        "it must nest under hookSpecificOutput"
    )
    hso = doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    context = hso["additionalContext"]
    assert "claimed by session otherses" in context
    assert "4242" in context
    assert "Not blocked" in context


def test_our_own_claim_is_silent(worktree: Path, monkeypatch, capsys) -> None:
    """Warning a session about its own worktree is noise, and noise is what gets
    an advisory ignored."""
    _claim_for(worktree, 4242, 99)
    monkeypatch.setattr(wc, "pid_is_live_session", lambda pid, start=None: True)
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: 4242)

    assert hook._advise(payload(worktree / "target.py")) == 0
    assert capsys.readouterr().out == "", "an advisory that stays silent must emit NOTHING on stdout"


def test_a_stale_claim_is_silent(worktree: Path, monkeypatch, capsys) -> None:
    """The holder is gone; the sweep will release it. Warning about a dead
    session's claim would train the reader to ignore the real ones."""
    _claim_for(worktree, 4242, 99)
    monkeypatch.setattr(wc, "pid_is_live_session", lambda pid, start=None: False)
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: 777)

    assert hook._advise(payload(worktree / "target.py")) == 0
    assert capsys.readouterr().out == "", "an advisory that stays silent must emit NOTHING on stdout"


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
    assert capsys.readouterr().out == "", "an advisory that stays silent must emit NOTHING on stdout"


def test_an_unclaimed_worktree_does_not_warn(worktree: Path, capsys) -> None:
    assert hook._advise(payload(worktree / "target.py")) == 0
    assert capsys.readouterr().out == "", "an advisory that stays silent must emit NOTHING on stdout"


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
    assert capsys.readouterr().out == "", "mode=off must warn about nothing"


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


# ─── the channel, and the never-block contract ───────────────────────────────


def test_the_advisory_reaches_the_model_through_the_only_channel_that_works(
    tmp_path: Path,
) -> None:
    """Delivery, not emission — the distinction that made the first version inert.

    Driven as a REAL subprocess, the way Claude Code runs it, with a claim held
    by a GENUINELY live session process. A monkeypatched liveness check cannot
    reach a subprocess, and faking it would recreate the original error: testing
    the thing I control instead of the thing I claimed.

    The earlier version printed to stderr and exited 0, and I "verified" it by
    reading that stderr back — which proves the hook produced bytes and nothing
    about whether anyone receives them. Per
    docs/reference/cc-compatibility.md:1093-1099 PreToolUse reaches the model
    ONLY through this envelope, and it is the same one two sibling hooks in this
    repo are observed delivering with.

    Runs against a REAL worktree of THIS repository rather than a temporary one,
    because the subprocess resolves ownership for itself: a hook handed a path in
    some other repository correctly refuses to claim it, and a parent-process
    monkeypatch cannot reach across the process boundary to say otherwise. Using
    the genuine geometry is also the more faithful test — it is the only one here
    that exercises the ownership check and the delivery channel together.

    SKIPS where no foreign session exists (CI), rather than faking one. A test
    that cannot run says so; it does not pretend.
    """
    import os as _os

    def _is_session(pid: str) -> bool:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\x00")
        except OSError:
            return False
        return bool(argv and argv[0]) and _os.path.basename(
            argv[0].decode(errors="replace")
        ) == "claude"

    mine = wc.session_pid_from_ancestry()
    foreign = [
        int(e)
        for e in _os.listdir("/proc")
        if e.isdigit() and _is_session(e) and int(e) != mine
    ]
    if not foreign:
        pytest.skip("no foreign Claude Code session on this box to hold the claim")

    holder = foreign[0]
    start = wc.proc_starttime(holder)
    reason = wc.format_reason(P("claim", pid=holder, start=start, sid="otherses"))

    # A genuine linked worktree of this repository, detached so no branch is
    # created, removed in `finally` so a failing assertion cannot leave a
    # registration behind.
    real = tmp_path / "advisory-delivery-probe"
    added = _git(_ROOT, "worktree", "add", "--quiet", "--detach", str(real))
    assert added.returncode == 0, added.stderr
    try:
        target = real / "target.py"
        target.write_text("x = 1\n")
        locked = _git(_ROOT, "worktree", "lock", "--reason", reason, str(real))
        assert locked.returncode == 0, locked.stderr

        result = run_hook("--advise", payload(target))
        assert result.returncode == 0

        doc = json.loads(result.stdout)
        assert set(doc) == {"hookSpecificOutput"}, (
            "nothing may sit beside the envelope: a top-level additionalContext is "
            "silently discarded, which is the same failure as writing to stderr"
        )
        hso = doc["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert "claimed by session otherses" in hso["additionalContext"]
        assert str(holder) in hso["additionalContext"]
        assert result.stderr == "", "stderr is the dead channel; nothing should go there"
    finally:
        _git(_ROOT, "worktree", "unlock", str(real))
        _git(_ROOT, "worktree", "remove", "--force", str(real))
        _git(_ROOT, "worktree", "prune")


def test_a_crash_inside_the_advisory_does_not_deny_the_edit(
    worktree: Path, monkeypatch
) -> None:
    """The never-block contract, against the failure mode most likely to break it.

    `run_guard` converts an unhandled exception into exit 2, and on PreToolUse
    exit 2 DENIES the tool call — so wiring the fail-closed runner here would let
    a bug in this advisory block an edit it has no business blocking. Its own
    docstring says "never for advisory or convenience guards". This asserts the
    fail-OPEN wrapper actually holds by making the hook raise.
    """
    import subprocess as _sp

    # The real entry point, fed a payload that cannot parse, asserting the
    # process still exits 0 — a non-zero exit here is a DENIED edit.
    result = _sp.run(
        [sys.executable, str(_HOOK), "--advise"],
        input="{not json at all",
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "an advisory must never exit non-zero: on PreToolUse that denies the edit"
    )


# ─── the releaser, without which the claimer is a leak ───────────────────────


def test_session_end_releases_only_this_sessions_claims(
    worktree: Path, tmp_path: Path, monkeypatch
) -> None:
    """A claimer without a releaser is a leak, and this one was measured leaking.

    The redesign that removed the daily sweep removed the only thing that
    released claims, so every first edit took a lock nothing ever dropped — the
    reaper skips a locked worktree permanently. On this install a claim sat for
    three days after its session exited and had to be released by hand.

    Three locks, one pass, and the discrimination is the whole test: ours goes,
    another session's stays, a foreign one is never touched. A release that drops
    everything would pass a test that only checked the first.
    """
    repo = worktree.parent / "repo"

    mine_wt = worktree
    other_wt = tmp_path / "wt-other"
    foreign_wt = tmp_path / "wt-foreign"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/other", str(other_wt))
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/foreign", str(foreign_wt))

    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: 4242)
    monkeypatch.setattr(wc, "proc_starttime", lambda pid: 99)

    wc.lock_worktree(mine_wt, P("claim", pid=4242, start=99, sid="mine"))
    wc.lock_worktree(other_wt, P("claim", pid=9999, start=1, sid="theirs"))
    _git(repo, "worktree", "lock", "--reason", "a human said do not touch", str(foreign_wt))

    # The release enumerates from the repo the hook lives in, so point it here.
    real_run = hook.subprocess.run

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "worktree", "list"]:
            kw = {**kw, "cwd": str(repo)}
        return real_run(cmd, **kw)

    monkeypatch.setattr(hook.subprocess, "run", fake_run)

    assert hook._release({}) == 0

    assert wc.read_lock(mine_wt) is None, "this session's own claim must be released"
    assert wc.read_lock(other_wt) is not None, "another session's claim must survive"
    assert wc.read_lock(foreign_wt) is not None, "a foreign lock must never be touched"


def test_session_end_with_no_resolvable_session_releases_nothing(
    worktree: Path, monkeypatch
) -> None:
    """The control. With no pid there is no basis for deciding ownership, so the
    safe answer is to touch nothing — not to release everything.

    THE ENUMERATION IS REDIRECTED HERE TOO, and that is not incidental. Without
    it `_release` lists the REAL repository's worktrees, never visits this
    temporary one, and the lock survives no matter what the code does — the test
    passes for the wrong reason. Caught by mutation: replacing the `is None`
    guard with a default pid left this green until the redirect was added.
    """
    repo = worktree.parent / "repo"
    real_run = hook.subprocess.run

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "worktree", "list"]:
            kw = {**kw, "cwd": str(repo)}
        return real_run(cmd, **kw)

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: None)
    monkeypatch.setattr(wc, "proc_starttime", lambda pid: 99)

    wc.lock_worktree(worktree, P("claim", pid=4242, start=99, sid="mine"))
    assert wc.read_lock(worktree) is not None, "precondition: the claim is in place"

    assert hook._release({}) == 0
    assert wc.read_lock(worktree) is not None



# ─── round-4 findings ────────────────────────────────────────────────────────


def test_release_runs_even_when_ownership_is_switched_off(
    worktree: Path, monkeypatch
) -> None:
    """Turning the feature off must not strand the claims it already took.

    A locked worktree is skipped unconditionally by the reaper, so a claim left
    behind by a disabled feature is permanent: the thing that created it is off
    and can no longer clean up after it. This also contradicted the shipped
    config, which states that `off` does not release existing claims BECAUSE a
    claim releases when its process exits regardless of the setting. That was a
    promise the code did not keep.

    Driven through `main` rather than `_release`, because the defect was purely
    the ORDER of the mode gate and the dispatch — `_release` itself was correct
    and a direct call would pass against the broken version.
    """
    mine = 424242
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda: mine)
    monkeypatch.setattr(wc, "pid_is_live_session", lambda *a, **k: True)
    monkeypatch.setattr(wc, "effective_mode", lambda: "off")
    monkeypatch.setattr(sys, "argv", ["worktree_ownership_advisory.py", "--release"])
    monkeypatch.setattr(hook, "read_payload", lambda: {"hook_event_name": "SessionEnd"})

    repo_root = worktree.parent / "repo"
    _git(repo_root, "worktree", "lock", "--reason",
         wc.format_reason(P("claim", pid=mine, start=wc.proc_starttime(mine) or 1, sid="s")),
         str(worktree))
    assert wc.read_lock(worktree) is not None, "precondition: the claim is in place"

    # Point the enumeration at THIS repo, not the real one — without this the
    # release lists the actual repository and never visits the fixture, which is
    # exactly how an earlier control test in this file passed while blind.
    real_run = hook.subprocess.run

    def fake_run(cmd, *a, **k):
        if cmd[:3] == ["git", "worktree", "list"]:
            k = {**k, "cwd": str(repo_root)}
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(hook.subprocess, "run", fake_run)

    assert hook.main() == 0
    assert wc.read_lock(worktree) is None, (
        "the claim survived a SessionEnd taken while ownership was off"
    )


def test_claiming_is_still_suppressed_when_switched_off(
    worktree: Path, monkeypatch
) -> None:
    """The control. Release is the ONLY thing exempt from the mode gate.

    Without this, moving the dispatch above the gate could have exempted
    everything, which would make the kill switch do nothing.
    """
    monkeypatch.setattr(wc, "effective_mode", lambda: "off")
    monkeypatch.setattr(sys, "argv", ["worktree_ownership_advisory.py", "--claim"])
    monkeypatch.setattr(
        hook, "read_payload", lambda: payload(worktree / "target.py")
    )
    assert hook.main() == 0
    assert wc.read_lock(worktree) is None, "a claim was taken while ownership was off"


def test_a_worktree_path_containing_a_newline_is_still_released(
    tmp_path: Path, monkeypatch
) -> None:
    """A newline is legal in a Unix path, and porcelain puts it INSIDE the value.

    Splitting the enumeration on lines turns such a path into a truncated,
    nonexistent root, so the real worktree is never visited and its claim
    outlives the session — the leak `_release` exists to close, reintroduced by
    the parser. `-z` makes the records NUL-terminated instead.

    Built with a REAL worktree whose name contains a newline rather than a
    synthetic porcelain string, because the bug is in how git's actual output is
    parsed; a hand-written fixture would encode my own belief about that output.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", "-b", "main")
    _git(root, "config", "user.email", "probe@example.invalid")
    _git(root, "config", "user.name", "Probe")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "--quiet", "-m", "seed")

    weird = tmp_path / "wt\nwith-newline"
    added = _git(root, "worktree", "add", "--quiet", "-b", "feature/nl", str(weird))
    if added.returncode != 0:
        pytest.skip(f"this filesystem rejects a newline in a path: {added.stderr.strip()}")

    mine = 515151
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda: mine)
    monkeypatch.setattr(wc, "pid_is_live_session", lambda *a, **k: True)
    monkeypatch.setattr(wc, "_our_common_dir", lambda: (root / ".git").resolve())
    _git(root, "worktree", "lock", "--reason",
         wc.format_reason(P("claim", pid=mine, start=1, sid="s")), str(weird))
    assert wc.read_lock(weird) is not None, "precondition: the claim is in place"

    real_run = hook.subprocess.run

    def fake_run(cmd, *a, **k):
        if cmd[:3] == ["git", "worktree", "list"]:
            k = {**k, "cwd": str(root)}
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(hook.subprocess, "run", fake_run)

    assert hook._release({}) == 0
    assert wc.read_lock(weird) is None, (
        "a worktree whose path contains a newline was never visited, so its "
        "claim outlived the session"
    )
