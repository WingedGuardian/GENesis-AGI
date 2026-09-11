"""Tests for the worktree ownership lever (scripts/hooks/worktree_claim.py).

The design rests on two claims about things this repo does not own -- that a
``git worktree lock`` stops the reaper, and that it stops ``git worktree
remove``. Both are exercised here against REAL git repositories rather than
mocks, because a mock of git would only prove that the mock agrees with the
belief being tested.

Every liveness test uses a CONTROL THAT MOVES. Asserting "a locked worktree was
skipped" on its own passes just as happily against a reaper that reaps nothing,
so each case runs the same worktree through the same command twice and differs
only by the lock.

The one thing deliberately NOT tested by spawning a real Claude Code session is
``session_pid_from_ancestry``. It is exercised against the live process tree
instead (the test process is itself a descendant of something), plus synthetic
``/proc`` readings, because spawning a session inside CI is neither available
nor reproducible.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "hooks" / "worktree_claim.py"
_spec = importlib.util.spec_from_file_location("worktree_claim", _SCRIPT)
wc = importlib.util.module_from_spec(_spec)
sys.modules["worktree_claim"] = wc
_spec.loader.exec_module(wc)


# ─── helpers ────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository with one commit on ``main``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", "-b", "main")
    _git(root, "config", "user.email", "probe@example.invalid")
    _git(root, "config", "user.name", "Probe")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "--quiet", "-m", "seed")
    return root


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    """A linked worktree on a branch already merged into ``main``."""
    _git(repo, "checkout", "--quiet", "-b", "feature/done")
    (repo / "f.txt").write_text("work\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "--quiet", "-m", "work")
    _git(repo, "checkout", "--quiet", "main")
    _git(repo, "merge", "--quiet", "--no-edit", "feature/done")
    path = tmp_path / "wt-done"
    _git(repo, "worktree", "add", "--quiet", str(path), "feature/done")
    return path


def _lock(repo: Path, path: Path, reason: str) -> None:
    result = _git(repo, "worktree", "lock", "--reason", reason, str(path))
    assert result.returncode == 0, result.stderr


# ─── geometry ───────────────────────────────────────────────────────────────


def test_worktree_root_is_found_for_a_path_inside_a_linked_worktree(worktree: Path) -> None:
    nested = worktree / "a" / "b"
    nested.mkdir(parents=True)
    assert wc.worktree_root_for(nested) == worktree.resolve()


def test_the_main_checkout_is_not_a_worktree(repo: Path) -> None:
    """The main tree is never claimed or reaped, and falls out of the geometry.

    A linked worktree's ``.git`` is a FILE holding a gitdir pointer; the main
    checkout's is a directory. So this is a property of git's own layout rather
    than a name check that a rename could defeat.
    """
    assert wc.worktree_root_for(repo) is None
    assert wc.worktree_root_for(repo / "README.md") is None


def test_gitdir_resolves_to_the_admin_directory_holding_the_lock(worktree: Path) -> None:
    gitdir = wc.gitdir_for(worktree)
    assert gitdir is not None
    assert gitdir.name == worktree.name
    assert gitdir.parent.name == "worktrees"


# ─── lock payload parsing ───────────────────────────────────────────────────


def test_a_lock_we_wrote_round_trips(repo: Path, worktree: Path) -> None:
    payload = {"v": 1, "rule": "claim", "pid": 4242, "start": 99, "sid": "abc"}
    _lock(repo, worktree, wc.format_reason(payload))
    lock = wc.read_lock(worktree)
    assert lock is not None
    assert lock.foreign is False
    assert lock.payload == payload
    assert lock.rule == "claim"


def test_the_reason_leads_with_a_sentence_before_the_json() -> None:
    """git echoes this verbatim when it refuses a removal, so a human reads it.

    The assertion is on ORDER, not on the presence of both halves: a reason that
    put the JSON first would still contain a sentence, and would still be
    unreadable at the moment it is shown.
    """
    reason = wc.format_reason({"v": 1, "rule": "claim", "pid": 7, "start": 1})
    assert reason.index("{") > 20
    assert reason.startswith("Claimed by a live Claude Code session (pid 7)")
    assert json.loads(reason[reason.index("{") :])["pid"] == 7


@pytest.mark.parametrize(
    "reason",
    [
        "do not touch",
        "migrating this by hand {not json}",
        '{"v": 999, "rule": "claim"}',
        '{"v": 1, "rule": "something-else"}',
        '{"v": 1}',
        '["v", 1]',
    ],
)
def test_a_reason_that_is_not_ours_is_foreign(repo: Path, worktree: Path, reason: str) -> None:
    """Anything we cannot fully validate is someone else's lock, not a repairable one.

    Covers the three ways a payload can be almost-ours -- wrong version, unknown
    rule, missing rule -- plus valid JSON of the wrong TYPE. Misreading one of
    these as ours is the single error that would auto-release work we do not own.
    """
    _lock(repo, worktree, reason)
    lock = wc.read_lock(worktree)
    assert lock is not None
    assert lock.foreign is True
    assert lock.payload is None


def test_an_unlocked_worktree_reads_as_no_lock(worktree: Path) -> None:
    assert wc.read_lock(worktree) is None


def test_claude_code_agent_locks_are_named_but_still_foreign(repo: Path, worktree: Path) -> None:
    """Claude Code's own worktree-isolated subagents lock what they create.

    Observed on a live install: ``claude agent agent-<id> (pid N start M)`` --
    the same pid+starttime identity this module uses. Recognising it improves the
    report and nothing else: it stays foreign, so nothing here ever releases it.
    """
    _lock(repo, worktree, "claude agent agent-a1ea091e2a88 (pid 425484 start 1362340)")
    lock = wc.read_lock(worktree)
    assert lock is not None
    assert lock.foreign is True
    assert wc.describe_foreign(lock.raw) == "claude agent"
    assert wc.is_releasable(lock, worktree)[0] is False


# ─── process identity ───────────────────────────────────────────────────────


def test_the_launcher_shell_is_not_mistaken_for_the_session(tmp_path: Path) -> None:
    """argv[0]'s basename decides, not a substring of the whole command line.

    A session is launched as ``bash -c cd <repo> && claude ...``, so the WRAPPER's
    cmdline also contains "claude". Recording the wrapper's pid would produce a
    claim that still reads as live after the session inside it exited. Uses a real
    process because the distinction is a property of /proc, not of a string.
    """
    # `sleep 30; :` rather than a bare `sleep 30`: bash EXECS a lone final simple
    # command, replacing itself, and the wrapper's cmdline would then be plain
    # `sleep 30` -- no longer the shape under test. The precondition below is
    # what caught that, so it stays.
    proc = subprocess.Popen(["bash", "-c", "sleep 30; :", "claude-session-wrapper"])
    try:
        time.sleep(0.2)
        raw = Path(f"/proc/{proc.pid}/cmdline").read_bytes().replace(b"\x00", b" ")
        assert b"claude" in raw, "precondition: the wrapper's cmdline mentions claude"
        assert wc.is_session_process(proc.pid) is False
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_starttime_and_ppid_are_readable_for_a_live_process() -> None:
    assert wc.proc_starttime(os.getpid()) is not None
    assert wc.proc_ppid(os.getpid()) == os.getppid()


def test_a_dead_pid_is_not_a_live_session() -> None:
    proc = subprocess.Popen(["sleep", "30"])
    pid, start = proc.pid, wc.proc_starttime(proc.pid)
    proc.kill()
    proc.wait(timeout=10)
    assert wc.pid_is_live_session(pid, start) is False
    assert wc.pid_is_live_session(None, None) is False
    assert wc.pid_is_live_session(1, None) is False


def test_a_recycled_pid_reads_as_dead_when_the_starttime_disagrees(monkeypatch) -> None:
    """The pid alone cannot establish identity, which is why start time is stored.

    Simulated by recording a live process under a start time it does not have --
    equivalent to the pid having been recycled, and not reproducible by waiting
    for the kernel to actually recycle one.

    THE EXE CHECK IS PATCHED OPEN ON PURPOSE. ``pid_is_live_session`` tests
    ``is_session_process`` BEFORE it compares start times, and a stand-in process
    is not named `claude` -- so without this the function returns False on the
    exe-name branch and the start-time comparison is never reached. An earlier
    version of this test did exactly that: it passed, while a mutation deleting
    the entire start-time check ALSO passed. Verified by re-running that mutation
    against this version, which now fails.
    """
    monkeypatch.setattr(wc, "is_session_process", lambda pid: True)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        real = wc.proc_starttime(proc.pid)
        assert real is not None
        assert wc.pid_is_live_session(proc.pid, real) is True, "control: the true start matches"
        assert wc.pid_is_live_session(proc.pid, real + 1) is False
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_ancestry_returns_none_rather_than_guessing_when_no_session_is_above() -> None:
    """init is not a session, so a walk that starts there must fail, not improvise.

    Every caller treats None as "no claim can be made". Returning some other pid
    would silently attach the claim to an unrelated process.
    """
    assert wc.session_pid_from_ancestry(start_pid=1) is None
    assert wc.session_pid_from_ancestry(start_pid=os.getpid(), max_hops=0) is None


def test_a_claim_without_a_resolvable_session_is_refused(monkeypatch) -> None:
    """No pid means no release condition, and a lock with no release condition
    is indistinguishable from a leak. Refusing to write one is the mechanism that
    keeps this from degenerating into a blanket lock on every worktree."""
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: None)
    assert wc.build_payload(wc.RULE_CLAIM) is None
    assert wc.build_payload("not-a-rule") is None


def test_a_dirty_payload_needs_no_process(monkeypatch) -> None:
    monkeypatch.setattr(wc, "session_pid_from_ancestry", lambda *a, **k: None)
    payload = wc.build_payload(wc.RULE_DIRTY)
    assert payload == {"v": 1, "rule": "dirty"}


# ─── release rules ──────────────────────────────────────────────────────────


def test_a_claim_is_kept_while_its_session_lives_and_released_once_it_is_gone(
    repo: Path, worktree: Path, monkeypatch
) -> None:
    """The load-bearing pair. Same lock, same worktree; only liveness moves.

    A real session process cannot be spawned here, so a `sleep` stands in and the
    exe-name half of the identity check is patched to accept it. Only the
    LIVENESS transition is under test. The exe-name half is proven separately, by
    test_the_launcher_shell_is_not_mistaken_for_the_session, against a real
    process -- patching it here would otherwise leave it covered nowhere.
    """
    proc = subprocess.Popen(["sleep", "30"])
    start = wc.proc_starttime(proc.pid)
    payload = {"v": 1, "rule": "claim", "pid": proc.pid, "start": start}
    _lock(repo, worktree, wc.format_reason(payload))
    lock = wc.read_lock(worktree)
    assert lock is not None

    monkeypatch.setattr(wc, "is_session_process", lambda pid: Path(f"/proc/{pid}").exists())
    try:
        kept, why = wc.is_releasable(lock, worktree)
        assert kept is False, why

        proc.kill()
        proc.wait(timeout=10)

        released, why = wc.is_releasable(lock, worktree)
        assert released is True, why
        assert "gone" in why
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_a_live_claim_still_releases_once_the_worktree_goes_idle(
    repo: Path, worktree: Path, monkeypatch
) -> None:
    """Without this, ~9 long-running sessions pin their worktrees for their whole
    life and the reaper never runs again. The idle window is the reaper's own
    STALE_DAYS, not a second threshold invented here."""
    monkeypatch.setattr(wc, "pid_is_live_session", lambda *a, **k: True)
    payload = {"v": 1, "rule": "claim", "pid": 4242, "start": 1}
    _lock(repo, worktree, wc.format_reason(payload))
    lock = wc.read_lock(worktree)
    assert lock is not None

    fresh, _ = wc.is_releasable(lock, worktree, idle_seconds=3600, stale_seconds=14 * 86400)
    assert fresh is False
    stale, why = wc.is_releasable(lock, worktree, idle_seconds=15 * 86400, stale_seconds=14 * 86400)
    assert stale is True
    assert "idle" in why


def test_a_dirty_lock_tracks_whether_tracked_changes_remain(repo: Path, worktree: Path) -> None:
    """Both directions, because only the pair shows the predicate is reading the
    worktree rather than returning a constant."""
    (worktree / "f.txt").write_text("modified\n")
    payload = {"v": 1, "rule": "dirty"}
    _lock(repo, worktree, wc.format_reason(payload))
    lock = wc.read_lock(worktree)
    assert lock is not None
    assert wc.is_releasable(lock, worktree)[0] is False

    _git(worktree, "checkout", "--", "f.txt")
    assert wc.is_releasable(lock, worktree)[0] is True


def test_untracked_files_do_not_count_as_work(worktree: Path) -> None:
    """If they did, build output and editor droppings would keep every worktree
    permanently locked, which is the blanket lock this design rejected."""
    assert wc.has_tracked_changes(worktree) is False
    (worktree / "scratch.log").write_text("noise\n")
    (worktree / "node_modules").mkdir()
    assert wc.has_tracked_changes(worktree) is False


def test_an_unreadable_worktree_reports_dirty(tmp_path: Path) -> None:
    """Fails CLOSED: a git failure keeps the lock rather than dropping protection."""
    assert wc.has_tracked_changes(tmp_path / "does-not-exist") is True


# ─── the Archon seam ────────────────────────────────────────────────────────


def test_archon_paths_are_empty_when_archon_is_absent(tmp_path: Path) -> None:
    assert wc.archon_active_paths(tmp_path / "nothing-here.db") == []


def test_archon_paths_are_empty_when_the_database_is_corrupt(tmp_path: Path) -> None:
    """Absent and broken are different branches and are tested separately: an
    install can lose Archon, and it can also have a half-written database."""
    broken = tmp_path / "archon.db"
    broken.write_bytes(b"this is not a sqlite file" * 40)
    assert wc.archon_active_paths(broken) == []


def test_archon_paths_are_empty_when_the_table_is_missing(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "archon.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
    assert wc.archon_active_paths(db) == []


def test_there_is_no_archon_rule_and_one_cannot_be_written(repo: Path, worktree: Path) -> None:
    """`archon` is not a rule, and a lock claiming to be one is FOREIGN.

    This is the regression test for a deadlock that a green unit suite did not
    catch and only an end-to-end run exposed. An `archon` rule DID exist: it held
    a worktree while Archon reported the environment active, and released when it
    stopped. Measured against a live install, that cycle never closes --
    Archon's `complete` runs `git worktree remove`, the lock refuses it, so the
    environment never leaves `status='active'`, so the release condition can
    never fire. `archon complete` reported "0 completed, 1 failed" and only a
    manual unlock recovered it.

    It was redundant as well as deadlocking: a clean Archon worktree has nothing
    for the reaper to destroy, and a dirty one is covered by the `dirty` rule --
    which the next test exercises on an Archon-shaped path.
    """
    assert "archon" not in wc.RULES
    assert wc.build_payload("archon") is None

    _lock(repo, worktree, 'held by something {"v": 1, "rule": "archon"}')
    lock = wc.read_lock(worktree)
    assert lock is not None
    assert lock.foreign is True, "an unknown rule must read as someone else's lock"
    assert wc.is_releasable(lock, worktree)[0] is False


def test_an_archon_worktree_is_protected_by_the_ordinary_dirty_rule(
    repo: Path, worktree: Path
) -> None:
    """What replaces the removed rule: nothing special, which is the point.

    Archon registers its worktrees against THIS repository, so they arrive in the
    same enumeration as every other worktree and are judged by the same rules.
    """
    (worktree / "f.txt").write_text("work Archon has not committed yet\n")
    assert wc.has_tracked_changes(worktree) is True

    payload = wc.build_payload(wc.RULE_DIRTY)
    assert wc.lock_worktree(worktree, payload) is True
    lock = wc.read_lock(worktree)
    assert wc.is_releasable(lock, worktree)[0] is False

    _git(worktree, "checkout", "--", "f.txt")
    assert wc.is_releasable(lock, worktree)[0] is True


# ─── git's own behaviour, which the whole design rests on ───────────────────


def test_a_lock_stops_git_worktree_remove_and_unlocking_lets_it_through(
    repo: Path, worktree: Path
) -> None:
    """MEASURED rather than read from git's usage string.

    Both halves matter. The refusal alone would also be produced by a worktree
    that could not be removed for some unrelated reason, so the unlocked removal
    is what shows the lock is the cause.
    """
    _lock(repo, worktree, wc.format_reason({"v": 1, "rule": "dirty"}))
    blocked = _git(repo, "worktree", "remove", str(worktree))
    assert blocked.returncode != 0
    assert "locked" in blocked.stderr.lower()

    _git(repo, "worktree", "unlock", str(worktree))
    allowed = _git(repo, "worktree", "remove", str(worktree))
    assert allowed.returncode == 0, allowed.stderr


def test_git_echoes_our_reason_when_it_refuses(repo: Path, worktree: Path) -> None:
    """Why the reason leads with a sentence: this text is what a blocked reader sees."""
    _lock(repo, worktree, wc.format_reason({"v": 1, "rule": "claim", "pid": 7, "start": 1}))
    blocked = _git(repo, "worktree", "remove", str(worktree))
    assert blocked.returncode != 0
    assert "Claimed by a live Claude Code session" in blocked.stderr


def test_lock_and_unlock_round_trip(repo: Path, worktree: Path) -> None:
    assert wc.lock_worktree(worktree, {"v": 1, "rule": "dirty"}) is True
    assert wc.read_lock(worktree).rule == "dirty"
    assert wc.unlock_worktree(worktree) is True
    assert wc.read_lock(worktree) is None
    assert wc.unlock_worktree(worktree) is False


def test_an_existing_lock_is_not_even_offered_to_git(
    repo: Path, worktree: Path, monkeypatch
) -> None:
    """A second lock attempt must not reach git at all, and the spy IS the test.

    Asserting only "returns False, lock unchanged" is VACUOUS here: git itself
    refuses to lock an already-locked worktree, so deleting this guard entirely
    leaves that assertion passing. Measured -- a mutation removing the guard kept
    the whole suite green until this spy existed.

    Which also settles what the guard is FOR, and it is not what the old name
    claimed. git is the enforcement. This guard is what makes the answer
    deterministic without spending a subprocess, and keeps the decision in this
    module rather than in git's exit codes.
    """
    assert wc.lock_worktree(worktree, {"v": 1, "rule": "dirty"}) is True

    calls: list[tuple] = []
    real_git = wc._git

    def spy(root, *args):
        calls.append(args)
        return real_git(root, *args)

    monkeypatch.setattr(wc, "_git", spy)
    assert wc.lock_worktree(worktree, {"v": 1, "rule": "claim", "pid": 1, "start": 1}) is False
    assert calls == [], f"a second lock attempt shelled out to git: {calls}"
    assert wc.read_lock(worktree).rule == "dirty"


def test_a_foreign_lock_is_never_unlocked_by_us(repo: Path, worktree: Path) -> None:
    _lock(repo, worktree, "do not touch")
    assert wc.unlock_worktree(worktree) is False
    assert wc.read_lock(worktree) is not None


# ─── config ─────────────────────────────────────────────────────────────────


def test_the_env_kill_switch_forces_off(monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_WORKTREE_OWNERSHIP", "1")
    assert wc.effective_mode() == "off"


def test_disabling_the_master_switch_is_equivalent_to_off(monkeypatch) -> None:
    monkeypatch.delenv("GENESIS_WORKTREE_OWNERSHIP", raising=False)
    monkeypatch.setattr(wc, "load_config", lambda: {"enabled": False, "mode": "advisory"})
    assert wc.effective_mode() == "off"


@pytest.mark.parametrize("bad", ["block", "", None, 3, "ADVISORY"])
def test_an_invalid_mode_degrades_to_advisory_not_off(monkeypatch, bad) -> None:
    """Degrades toward keeping protection. Every surface here is non-blocking --
    a lock the reaper already honours, and a hook that writes to stderr and exits
    0 -- so failing toward `off` would drop protection to avoid no risk at all."""
    monkeypatch.delenv("GENESIS_WORKTREE_OWNERSHIP", raising=False)
    monkeypatch.setattr(wc, "load_config", lambda: {"enabled": True, "mode": bad})
    assert wc.effective_mode() == "advisory"


def test_an_unquoted_yaml_off_is_honoured(monkeypatch) -> None:
    """`mode: off` unquoted parses as a YAML 1.1 boolean, not the string 'off'.

    Without this branch a hand-edited config would read as an invalid mode and
    degrade to advisory -- silently doing the opposite of what was written.
    """
    monkeypatch.delenv("GENESIS_WORKTREE_OWNERSHIP", raising=False)
    monkeypatch.setattr(wc, "load_config", lambda: {"enabled": True, "mode": False})
    assert wc.effective_mode() == "off"


def test_the_shipped_config_is_valid(monkeypatch) -> None:
    monkeypatch.delenv("GENESIS_WORKTREE_OWNERSHIP", raising=False)
    cfg = wc.load_config()
    assert cfg["mode"] in wc.MODES
    assert isinstance(cfg["enabled"], bool)
