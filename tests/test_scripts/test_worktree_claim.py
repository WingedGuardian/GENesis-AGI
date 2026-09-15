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
def worktree(repo: Path, tmp_path: Path, monkeypatch) -> Path:
    """A linked worktree on a branch already merged into ``main``.

    Also declares this fixture's repository to be OURS. Without that,
    ``worktree_root_for`` would correctly refuse every worktree here as
    belonging to a different repository — which is the point of the ownership
    check and is asserted directly by the cross-repo tests below.
    """
    monkeypatch.setattr(wc, "_our_common_dir", lambda: (repo / ".git").resolve())
    _git(repo, "checkout", "--quiet", "-b", "feature/done")
    (repo / "f.txt").write_text("work\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "--quiet", "-m", "work")
    _git(repo, "checkout", "--quiet", "main")
    _git(repo, "merge", "--quiet", "--no-edit", "feature/done")
    path = tmp_path / "wt-done"
    _git(repo, "worktree", "add", "--quiet", str(path), "feature/done")
    return path


def P(rule: str, **extra) -> dict:
    """A well-formed ownership payload.

    Built from the module's OWN namespace constant rather than a literal, so a
    change to that constant cannot leave these tests quietly asserting a format
    nothing writes any more.
    """
    return {"ns": wc.PAYLOAD_NAMESPACE, "v": 1, "rule": rule, **extra}


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
    payload = P("claim", pid=4242, start=99, sid="abc")
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
    reason = wc.format_reason(P("claim", pid=7, start=1))
    assert reason.index("{") > 20
    assert reason.startswith("Claimed by a live Claude Code session (pid 7)")
    assert json.loads(reason[reason.index("{") :])["pid"] == 7


@pytest.mark.parametrize(
    "reason",
    [
        "do not touch",
        "migrating this by hand {not json}",
        '{"v": 999, "rule": "claim"}',
        '{"v": 1, "rule": "something-else", "pid": 4242}',
        '{"v": 1}',
        '["v", 1]',
        # The case that made the namespace necessary. `v` and `rule` are ordinary
        # words; an operator or another tool can write them by accident, and
        # without a namespace this parsed as OURS and became eligible for
        # auto-release -- silently breaking the one invariant this module rests
        # on. Found in review, not by the suite, which is why it is pinned here.
        'manual hold {"v": 1, "rule": "claim", "pid": 4242}',
        'do not touch {"v": 1, "rule": "claim", "pid": 4242, "start": 1}',
        # Right namespace, malformed body: a claim with no usable pid has no
        # release condition, and treating it as ours would release it instantly.
        '{"ns": "genesis.worktree-ownership", "v": 1, "rule": "claim"}',
        '{"ns": "genesis.worktree-ownership", "v": 1, "rule": "claim", "pid": "4242"}',
        '{"ns": "genesis.worktree-ownership", "v": 1, "rule": "claim", "pid": 1}',
        '{"ns": "genesis.worktree-ownership", "v": 1, "rule": "claim", "pid": 9, "start": "x"}',
        '{"ns": "genesis.other-thing", "v": 1, "rule": "claim", "pid": 4242}',
    ],
)
def test_a_reason_that_is_not_ours_is_foreign(repo: Path, worktree: Path, reason: str) -> None:
    """Anything we cannot fully validate is someone else's lock, not a repairable one.

    Covers every way a payload can be almost-ours -- wrong version, unknown rule,
    missing rule, valid JSON of the wrong TYPE, generic JSON with no namespace,
    a different namespace, and a correctly-namespaced claim whose body is
    unusable. Misreading any of these as ours is the single error that would
    auto-release work we do not own.
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
    assert wc.is_releasable(lock)[0] is False


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
    payload = P("claim", pid=proc.pid, start=start)
    _lock(repo, worktree, wc.format_reason(payload))
    lock = wc.read_lock(worktree)
    assert lock is not None

    monkeypatch.setattr(wc, "is_session_process", lambda pid: Path(f"/proc/{pid}").exists())
    try:
        kept, why = wc.is_releasable(lock)
        assert kept is False, why

        proc.kill()
        proc.wait(timeout=10)

        released, why = wc.is_releasable(lock)
        assert released is True, why
        assert "gone" in why
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# ─── the Archon seam ────────────────────────────────────────────────────────


# ─── git's own behaviour, which the whole design rests on ───────────────────


def test_a_lock_stops_git_worktree_remove_and_unlocking_lets_it_through(
    repo: Path, worktree: Path
) -> None:
    """MEASURED rather than read from git's usage string.

    Both halves matter. The refusal alone would also be produced by a worktree
    that could not be removed for some unrelated reason, so the unlocked removal
    is what shows the lock is the cause.
    """
    _lock(repo, worktree, wc.format_reason(P("claim", pid=7, start=1)))
    blocked = _git(repo, "worktree", "remove", str(worktree))
    assert blocked.returncode != 0
    assert "locked" in blocked.stderr.lower()

    _git(repo, "worktree", "unlock", str(worktree))
    allowed = _git(repo, "worktree", "remove", str(worktree))
    assert allowed.returncode == 0, allowed.stderr


def test_git_echoes_our_reason_when_it_refuses(repo: Path, worktree: Path) -> None:
    """Why the reason leads with a sentence: this text is what a blocked reader sees."""
    _lock(repo, worktree, wc.format_reason(P("claim", pid=7, start=1)))
    blocked = _git(repo, "worktree", "remove", str(worktree))
    assert blocked.returncode != 0
    assert "Claimed by a live Claude Code session" in blocked.stderr


def test_lock_and_unlock_round_trip(repo: Path, worktree: Path) -> None:
    assert wc.lock_worktree(worktree, P("claim", pid=4242, start=99)) is True
    assert wc.read_lock(worktree).rule == "claim"
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
    assert wc.lock_worktree(worktree, P("claim", pid=4242, start=99)) is True

    calls: list[tuple] = []
    real_git = wc._git

    def spy(root, *args):
        calls.append(args)
        return real_git(root, *args)

    monkeypatch.setattr(wc, "_git", spy)
    assert wc.lock_worktree(worktree, P("claim", pid=9999, start=1)) is False
    assert calls == [], f"a second lock attempt shelled out to git: {calls}"
    assert wc.read_lock(worktree).rule == "claim"


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


def test_the_overlay_precedence_matches_the_canonical_resolver(monkeypatch, tmp_path) -> None:
    """The user-config overlay wins, exactly as genesis._config_overlay does.

    This is not a preference. The settings API writes overrides to
    ``~/.genesis/config/<stem>.local.yaml`` on purpose, so user config never
    lands in a PR. A loader reading only the repo's ``config/`` directory makes
    ``settings_update`` report success, ``settings_get`` show the override, and
    the sweeper go on using the default -- a lever that looks live and is inert.
    Found in review; pinned here so the two resolvers cannot drift apart.
    """
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # No user overlay yet -> falls back to the repo-adjacent sibling.
    assert wc._overlay_path() == wc._config_path().with_suffix(".local.yaml")

    user_overlay = home / ".genesis" / "config" / "worktree_ownership.local.yaml"
    user_overlay.write_text("enabled: false\n")
    assert wc._overlay_path() == user_overlay

    monkeypatch.delenv("GENESIS_WORKTREE_OWNERSHIP", raising=False)
    assert wc.load_config()["enabled"] is False
    assert wc.effective_mode() == "off", "a user-set override must actually take effect"


def test_the_shipped_config_is_valid(monkeypatch) -> None:
    monkeypatch.delenv("GENESIS_WORKTREE_OWNERSHIP", raising=False)
    cfg = wc.load_config()
    assert cfg["mode"] in wc.MODES
    assert isinstance(cfg["enabled"], bool)


# ─── cross-repo ownership ───────────────────────────────────────────────────


@pytest.fixture
def sibling_worktree(tmp_path: Path) -> Path:
    """A linked worktree of a DIFFERENT repository, built the same way as ours.

    Deliberately identical in shape to the `worktree` fixture: same layout, same
    `.git` file, same branch geometry. The ONLY thing that distinguishes it is
    which repository it belongs to, so a test that passes here cannot be passing
    on some incidental difference.
    """
    root = tmp_path / "sibling"
    root.mkdir()
    _git(root, "init", "--quiet", "-b", "main")
    _git(root, "config", "user.email", "probe@example.invalid")
    _git(root, "config", "user.name", "Probe")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "--quiet", "-m", "seed")
    path = tmp_path / "sibling-wt"
    _git(root, "worktree", "add", "--quiet", "-b", "feature/theirs", str(path))
    return path


def test_a_worktree_of_another_repository_is_never_claimed(
    repo: Path, worktree: Path, sibling_worktree: Path
) -> None:
    """Ownership is decided by the git COMMON DIR, not by path shape.

    This is the whole reason the check exists. A session rooted in this
    repository can be handed a path inside an unrelated project's worktree --
    an external orchestrator places its worktrees outside this tree entirely,
    and so does anyone with a second checkout -- and nothing about the PATH
    distinguishes the two cases. Without the common-dir comparison the hook
    would write OUR lock into THEIR repository, where it pins their worktree
    against their own tooling and carries our namespace and our session's pid.

    Both halves are asserted from the same test, against two real repositories,
    because "ours is accepted" alone would also pass an implementation that
    accepts everything.
    """
    assert wc.worktree_root_for(worktree) == worktree.resolve()
    assert wc.worktree_root_for(sibling_worktree / "README.md") is None


def test_ownership_fails_closed_when_our_own_repository_cannot_be_resolved(
    worktree: Path, monkeypatch
) -> None:
    """An unanswerable question means we do NOT claim.

    The two failures are not symmetric. A false negative costs one missed
    advisory; a false positive writes a lock into a repository that is not ours.
    So an unresolvable common dir -- git absent, a timeout, a detached
    environment -- resolves toward refusing.
    """
    monkeypatch.setattr(wc, "_our_common_dir", lambda: None)
    assert wc.worktree_root_for(worktree) is None


def test_an_exported_git_dir_cannot_make_a_foreign_worktree_look_like_ours(
    sibling_worktree: Path, monkeypatch
) -> None:
    """Ambient git location overrides BEAT `-C`, and that defeats the whole check.

    `git rev-parse --local-env-vars` lists GIT_DIR and GIT_COMMON_DIR as
    repository-local: with either exported, every `rev-parse --git-common-dir`
    answers for THAT repository no matter which directory it runs in. Both sides
    of the ownership comparison then return the same foreign path and AGREE — and
    agreeing is precisely what makes a foreign worktree read as ours. The check
    would not merely fail; it would invert, accepting exactly what it exists to
    refuse, and a claim would be written into someone else's repository.

    This repo already knows the trap: `.claude/hooks/genesis-hook` scrubs the same
    three variables so an exported override cannot redirect hook discovery to an
    unrelated checkout. This is that trap one layer down.

    Note `_our_common_dir` is deliberately NOT patched here — its real resolution
    is half of what the override corrupts, so patching it would hide the bug.
    """
    sibling_git = str((sibling_worktree.parent / "sibling" / ".git").resolve())
    monkeypatch.setenv("GIT_DIR", sibling_git)
    monkeypatch.setenv("GIT_COMMON_DIR", sibling_git)
    monkeypatch.setattr(wc, "_COMMON_DIR_CACHE", wc._UNSET)

    assert wc.worktree_root_for(sibling_worktree / "README.md") is None, (
        "an exported GIT_DIR made another repository's worktree resolve as ours"
    )


def test_the_scrub_removes_only_the_location_overrides(monkeypatch) -> None:
    """The control: scrubbing must not blank the environment wholesale.

    git needs the rest of the environment — HOME for config discovery, PATH to
    be found at all — so an over-broad scrub would break the very calls it is
    meant to protect, and would do it silently because both functions fail closed.
    """
    monkeypatch.setenv("GIT_DIR", "/nowhere/.git")
    monkeypatch.setenv("GIT_COMMON_DIR", "/nowhere/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/nowhere")
    env = wc._git_env()
    for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"):
        assert var not in env, f"{var} survived the scrub"
    for var in ("PATH", "HOME"):
        if var in os.environ:
            assert env.get(var) == os.environ[var], f"{var} must be preserved"


def test_an_exported_git_dir_pointing_at_OUR_repo_cannot_adopt_a_foreign_worktree(
    sibling_worktree: Path, monkeypatch
) -> None:
    """The dangerous direction, which the sibling-pointing case does not reach.

    Found by mutation: removing the scrub from the CANDIDATE side alone left the
    sibling-pointing test green, because both sides then disagreed and the
    refusal happened for the wrong reason. That was a blind spot in the test, not
    a harmless mutation — point the override at OUR repository instead and the
    candidate side answers "ours" for a path inside someone else's worktree, so
    the comparison AGREES and the foreign worktree is adopted.

    This is the realistic shape too: a wrapper exporting GIT_DIR for the repo it
    is operating on is ordinary, and it is exactly then that a stray absolute
    path into another checkout gets claimed.
    """
    ours = wc._our_common_dir()
    if ours is None:
        pytest.skip("cannot resolve this repository's common dir")
    monkeypatch.setenv("GIT_DIR", str(ours))
    monkeypatch.setenv("GIT_COMMON_DIR", str(ours))
    monkeypatch.setattr(wc, "_COMMON_DIR_CACHE", wc._UNSET)

    assert wc.worktree_root_for(sibling_worktree / "README.md") is None, (
        "a GIT_DIR pointing at our own repo made another repository's worktree "
        "answer as ours, so it would have been claimed"
    )
