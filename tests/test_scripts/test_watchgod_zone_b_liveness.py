"""tmp_watchgod Zone B — reclaim dead pytest trees, spare live ones.

Origin (measured on a live install, 2026-09-24): /tmp hit 85% of a 512 MB
tmpfs and paged the operator. 255 MB of it — half the filesystem — was two
pytest base directories from a suite that had long since exited. The emergency
tier ran, reported "aggressive cleanup complete" four seconds later, and had
reclaimed none of it. Then it did that again every 30 seconds for 22 minutes.

TWO INDEPENDENT REASONS, and fixing either alone leaves the other:

1. Every sweep carries ``-not -path "*/pytest-*"``, at five sites. The
   exclusion exists to protect a running suite, but it cannot tell a live run
   from a dead one, so it protected the garbage equally.
2. The tiers match on ``-atime +7/+3/+1``. The directories were hours old, so
   even with the exclusion lifted no predicate would have matched them.

``reclaim_dead_pytest_dirs`` replaces the name test with a liveness test and
handles the tree WHOLE rather than file by file. The generic sweeps keep their
name exclusion deliberately — they must never nibble individual files out of a
tree that is live.

These arms pin BOTH directions. A reaper that spares everything is disabled,
not safe; a reaper that deletes a running suite's temp is the Zone A incident
this repo already had once.

FOUR OF THESE ARMS ARE REGRESSION TESTS FOR DEFECTS IN THIS FUNCTION'S OWN
FIRST DRAFT, found by adversarial review. They are named so, because each one
looked correct and each one failed in the deleting direction:
  * the tier age gate was reachable only on the no-lock path, so a locked
    directory whose owner had exited was deleted with no age test at all;
  * ``kill -0`` was used for liveness, which reads a LIVE process owned by
    another uid as dead;
  * the /proc snapshot that authorises deletion was taken unvalidated, and
    both halves of the path that produces it fail OPEN;
  * a partial delete was silent.

Harness idiom mirrors test_watchgod_red_freshness.py (deliberately duplicated —
repo precedent: the watchgod test files do not share a conftest).
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

_TMUX_STUB = """#!/usr/bin/env bash
exit 0
"""

_HOLDER = """
import sys, time
f = open(sys.argv[1], "ab")
sys.stdout.write("ready\\n")
sys.stdout.flush()
time.sleep(120)
"""


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    """HOME, a fake /tmp root, and a stub bin dir.

    The fake root is the whole point: this function DELETES trees, so an arm
    pointed at the real /tmp would be operating on live machine state — up to
    and including another session's in-flight suite. It is passed as an
    ARGUMENT rather than an environment variable: an earlier draft used a
    ``SYS_TMP_DIR`` global, and ``load_config`` sources its config file with no
    key allowlist, which put the delete root within reach of configuration.
    """
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    systmp = tmp_path / "systmp"
    # pytest names this root after the user; the reclaim globs `pytest-of-*`,
    # so the arms use a fixed name rather than depending on who runs them.
    (systmp / "pytest-of-user").mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_exec(bind / "tmux", _TMUX_STUB)
    return home, systmp, bind


def _pof(systmp: Path) -> Path:
    return systmp / "pytest-of-user"


def _run(home: Path, bind: Path, snippet: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(HOME=str(home), PATH=f"{bind}:{os.environ['PATH']}")
    return subprocess.run(
        ["bash", "-c", f"source '{_WATCHGOD}'\n{snippet}"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _reclaim(home: Path, systmp: Path, bind: Path, gate: int = 60):
    """Drive the function the way production does, with the sandbox root as $3."""
    return _run(home, bind, f'reclaim_dead_pytest_dirs {gate} "{systmp}"')


def _log(home: Path) -> str:
    p = home / ".genesis" / "logs" / "tmp_watchgod.log"
    return p.read_text() if p.exists() else ""


def _numbered(systmp: Path, n: int, *, lock_pid: int | None, age_min: int = 0) -> Path:
    """A pytest numbered dir with content, optionally locked, optionally aged.

    ``lock_pid`` writes that pid into ``.lock`` exactly as pytest does
    (_pytest/pathlib.py:254-256). ``age_min`` back-dates the directory AND its
    lock so the age gates can be exercised without waiting.
    """
    d = _pof(systmp) / f"pytest-{n}"
    (d / "test_something0").mkdir(parents=True)
    (d / "test_something0" / "payload.bin").write_bytes(b"x" * 4096)
    if lock_pid is not None:
        (d / ".lock").write_text(str(lock_pid))
    if age_min:
        when = time.time() - age_min * 60
        for target in (d / ".lock", d):
            if target.exists():
                os.utime(target, (when, when))
    return d


def _dead_pid() -> int:
    """A pid that is provably not running: spawn and reap a trivial child."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    # The pid is reaped, so /proc/<pid> disappears. Confirm rather than assume —
    # a recycled pid would make every "reclaims a dead run" arm vacuous.
    for _ in range(50):
        if not Path(f"/proc/{proc.pid}").exists():
            return proc.pid
        time.sleep(0.02)
    raise AssertionError("pid never became free; arm would be vacuous")


@contextlib.contextmanager
def _writer_holding(path: Path):
    """A live process holding `path` open. Handshakes so the descriptor is
    provably open before the body runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"partial")
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready", "holder never opened the file"
        yield proc
    finally:
        proc.kill()
        proc.wait()


# ── the incident itself ──────────────────────────────────────────────────


def test_reclaims_the_incident_shape(tmp_path):
    """THE ACCEPTANCE BAR: an hours-old pytest tree whose owner has exited.

    This is what filled the RAM disk. It carries a lock (the repo does not set
    retention `none`, so pytest wrote one) naming a pid that is gone.
    """
    home, systmp, bind = _sandbox(tmp_path)
    dead = _numbered(systmp, 826, lock_pid=_dead_pid(), age_min=180)

    res = _reclaim(home, systmp, bind)

    assert res.returncode == 0, res.stderr
    assert not dead.exists(), (
        "the directory that filled the filesystem survived the reclaim:\n" + res.stderr
    )


# ── regression: the age gate must bind on EVERY path ─────────────────────


def test_tier_gate_binds_even_when_the_lock_says_the_owner_is_dead(tmp_path):
    """REGRESSION (adversarial review, 2026-09-25). The first draft put the age
    gate in the `elif` of the no-lock branch, so a LOCKED directory whose owner
    had exited reached the delete without any age test. Since this project
    always writes a lock, that was the COMMON path: the tier constants were
    cosmetic, and YELLOW — which fires at 51% of the filesystem, every 30s —
    would have deleted a tree the instant its suite exited.

    A dead owner is a NECESSARY condition for reclaiming, never a sufficient
    one. pytest deliberately retains the last `keep` runs so a developer can
    open the artifacts of a failing run (_pytest/pathlib.py:363-396).
    """
    home, systmp, bind = _sandbox(tmp_path)
    fresh_but_dead = _numbered(systmp, 840, lock_pid=_dead_pid(), age_min=0)

    res = _reclaim(home, systmp, bind, gate=10080)  # the YELLOW gate, 7 days

    assert res.returncode == 0, res.stderr
    assert fresh_but_dead.exists(), (
        "deleted a just-finished run's temp tree under a SEVEN DAY gate — the "
        "caller's age gate is being bypassed on the locked path"
    )


def test_tier_gate_is_consulted_before_any_liveness_signal(tmp_path):
    """Paired with the arm above: the gate must not merely exist somewhere, it
    must sit ahead of the signals. A young tree with NO lock and no writer —
    every liveness signal says 'dead' — is still spared by the gate alone."""
    home, systmp, bind = _sandbox(tmp_path)
    young = _numbered(systmp, 841, lock_pid=None, age_min=10)

    res = _reclaim(home, systmp, bind, gate=60)

    assert res.returncode == 0, res.stderr
    assert young.exists()


# ── regression: liveness must not be decided by kill -0 ──────────────────


def test_spares_a_live_run_owned_by_another_uid(tmp_path):
    """REGRESSION (adversarial review, 2026-09-25). The first draft used
    `kill -0 "$pid"` for liveness. MEASURED on bash 5.2.21:

        kill -0 1       -> rc 1, "Operation not permitted"   (pid 1 IS alive)
        kill -0 999999  -> rc 1, "No such process"

    EPERM and ESRCH are indistinguishable, so a LIVE process owned by another
    uid read as dead and its tree was deleted. The diff's own comment claimed
    this direction "fails SAFE"; it did not.

    pid 1 is the cleanest available live-but-not-ours process: root-owned,
    guaranteed running, and guaranteed a different uid from the test runner.
    """
    assert Path("/proc/1").is_dir(), "no /proc/1 — arm cannot distinguish anything"
    assert os.geteuid() != 0, "running as root would make this arm vacuous"

    home, systmp, bind = _sandbox(tmp_path)
    owned_by_root = _numbered(systmp, 842, lock_pid=1, age_min=10000)

    res = _reclaim(home, systmp, bind)

    assert res.returncode == 0, res.stderr
    assert owned_by_root.exists(), (
        "reaped a tree whose lock names a LIVE process — liveness is being "
        "decided by signalling, which cannot see across a uid boundary"
    )


def test_liveness_is_read_from_proc_not_from_signalling(tmp_path):
    """The mechanism, pinned directly. The behavioural arm above can in
    principle be satisfied another way; this states which way, because the
    difference is invisible until it deletes something."""
    body = _WATCHGOD.read_text()
    start = body.index("reclaim_dead_pytest_dirs() {")
    fn = body[start : body.index("\n}\n", start)]
    # COMMENTS STRIPPED FIRST. This function's own comments explain at length
    # why signalling is the wrong liveness test, so a scan of the raw text
    # matches the explanation rather than the code -- a check that fires on
    # prose is one that gets deleted the next time someone edits a comment.
    fn = "\n".join(ln for ln in fn.splitlines() if not ln.lstrip().startswith("#"))
    assert '[[ -d "/proc/$pid" ]]' in fn, "the /proc liveness test is gone"
    assert not re.search(r"\bkill\s+-0\b", fn), (
        "liveness is being decided by `kill -0`, which returns 1 for BOTH a "
        "live process owned by another uid (EPERM) and an absent one (ESRCH)"
    )


def test_spares_a_run_whose_lock_pid_is_alive(tmp_path):
    """The ordinary live case: the lock names this test process. Age is
    irrelevant — the lock is written once and never refreshed, so a suite that
    has run for days still owns its directory."""
    home, systmp, bind = _sandbox(tmp_path)
    live = _numbered(systmp, 827, lock_pid=os.getpid(), age_min=10000)

    res = _reclaim(home, systmp, bind)

    assert res.returncode == 0, res.stderr
    assert live.exists(), "reaped a directory whose owning process is still running"
    assert (live / "test_something0" / "payload.bin").exists()


# ── regression: the snapshot that authorises deletion must be validated ──


def test_fails_closed_when_the_proc_snapshot_is_blind(tmp_path):
    """REGRESSION (adversarial review, 2026-09-25). `live_open_paths` ends in
    `2>/dev/null || true` and `dir_has_live_writer` opens with
    `[[ -n "$snapshot" ]] || return 1`. BOTH halves fail OPEN, so a failed or
    partial /proc read is indistinguishable from "nothing is live" — and the
    caller is a loop that deletes whatever it believes to be dead.

    Zone A validates the identical snapshot with a positive self-test before
    letting it authorise a delete. The first draft of this function reused the
    helper without the validation, which is the half that carries the load.
    Here the reclaim must SKIP rather than delete.
    """
    home, systmp, bind = _sandbox(tmp_path)
    dead = _numbered(systmp, 843, lock_pid=_dead_pid(), age_min=180)

    res = _run(
        home,
        bind,
        "live_open_paths() { :; }; "  # a blind reading: no paths at all
        f'reclaim_dead_pytest_dirs 60 "{systmp}"',
    )

    assert res.returncode == 0, res.stderr
    assert dead.exists(), "deleted on a /proc reading that could not see any live writer"
    assert "failed its self-test" in _log(home), (
        "skipped silently — an operator cannot tell a skipped poll from a clean one"
    )


def test_spares_a_run_with_an_open_descriptor_and_no_lock(tmp_path):
    """The keep=0 case: `if keep != 0` (pathlib.py:390) means a project with
    retention `none` runs entirely unlocked. The /proc signal is what covers
    it, and the directory is back-dated past the age gate so ONLY the open
    descriptor can be what spares it."""
    home, systmp, bind = _sandbox(tmp_path)
    unlocked = _numbered(systmp, 828, lock_pid=None, age_min=10000)

    with _writer_holding(unlocked / "test_something0" / "live.bin"):
        res = _reclaim(home, systmp, bind)

    assert res.returncode == 0, res.stderr
    assert unlocked.exists(), "reaped an unlocked directory that a process was writing into"


def test_reclaims_an_old_unlocked_dir(tmp_path):
    """Paired with the sparing arms, so they cannot pass vacuously by the
    reclaim being broken outright."""
    home, systmp, bind = _sandbox(tmp_path)
    old = _numbered(systmp, 830, lock_pid=None, age_min=180)

    res = _reclaim(home, systmp, bind)

    assert res.returncode == 0, res.stderr
    assert not old.exists(), "an old unlocked tree is garbage and must be reclaimed"


# ── the stale-lock threshold is pytest's, and the VALUE is the claim ─────


def test_stale_lock_threshold_is_exactly_pytests_lock_timeout(tmp_path):
    """An unreadable lock falls back to mtime, and the threshold is pytest's
    own: LOCK_TIMEOUT = 60*60*24*3 (_pytest/pathlib.py:46).

    STRADDLED at the boundary, one minute either side. An earlier version of
    this arm used 2 days and 4 days, which any value in a 48-hour window would
    have satisfied — the constant could be changed to 3000 with the suite still
    green. The threshold IS the claim, so the arm has to bind the number.
    """
    from _pytest.pathlib import LOCK_TIMEOUT

    assert LOCK_TIMEOUT // 60 == 4320, (
        f"pytest changed LOCK_TIMEOUT to {LOCK_TIMEOUT}s; PYTEST_LOCK_TIMEOUT_MIN must follow it"
    )

    home, systmp, bind = _sandbox(tmp_path)
    just_inside = _pof(systmp) / "pytest-831"
    just_outside = _pof(systmp) / "pytest-832"
    for d, mins in ((just_inside, 4319), (just_outside, 4321)):
        (d / "x").mkdir(parents=True)
        (d / ".lock").write_text("not-a-pid")  # no readable pid -> mtime rule
        when = time.time() - mins * 60
        os.utime(d / ".lock", (when, when))
        os.utime(d, (when, when))

    res = _reclaim(home, systmp, bind)

    assert res.returncode == 0, res.stderr
    assert just_inside.exists(), "4319 min is INSIDE pytest's 4320-min window — spare it"
    assert not just_outside.exists(), "4321 min is expired by pytest's own rule"


# ── structure and safety ─────────────────────────────────────────────────


def test_never_follows_the_pytest_current_symlink(tmp_path):
    """pytest maintains `pytest-current` -> the newest run. Mirrors
    `ensure_deletable`'s symlink refusal (_pytest/pathlib.py:312): a symlink is
    never the unit of reclamation.

    FIXTURE SHAPE, because it is deliberate and looks odd. The link's target
    sits OUTSIDE the swept glob, and the LINK ITSELF is back-dated. Both were
    learned by mutation: pointed at a sibling numbered dir, the target's own
    iteration decided the outcome first; left fresh, the link was spared by the
    age gate — with the guard deleted, this arm passed BOTH times. `find
    -maxdepth 0` stats the link rather than following it, so only an aged link
    whose target is out of the glob isolates the guard.
    """
    home, systmp, bind = _sandbox(tmp_path)
    target = systmp / "target-outside-the-sweep"
    target.mkdir()
    (target / "payload.bin").write_bytes(b"z" * 4096)
    old = time.time() - 180 * 60
    os.utime(target, (old, old))
    link = _pof(systmp) / "pytest-current"
    link.symlink_to(target)
    os.utime(link, (old, old), follow_symlinks=False)

    res = _reclaim(home, systmp, bind)

    assert res.returncode == 0, res.stderr
    assert link.is_symlink(), "the symlink was treated as a reclaimable directory"
    assert target.exists(), "deleted the symlink's target"


def test_never_unlinks_a_lock_it_decided_to_spare(tmp_path):
    """pytest's own GC unlinks an expired lock (pathlib.py:333). This must not
    — a watchdog that strips a lock hands the next cleaner a directory this one
    had decided to protect."""
    home, systmp, bind = _sandbox(tmp_path)
    live = _numbered(systmp, 834, lock_pid=os.getpid(), age_min=10000)

    res = _reclaim(home, systmp, bind)

    assert res.returncode == 0, res.stderr
    assert (live / ".lock").read_text() == str(os.getpid())


def test_reclaims_abandoned_garbage_dirs(tmp_path):
    """`maybe_delete_a_numbered_dir` renames to `garbage-<uuid>` before
    removing it (pathlib.py:291-293). A process killed between those two steps
    strands the tree under a name pytest will never revisit."""
    home, systmp, bind = _sandbox(tmp_path)
    garbage = _pof(systmp) / "garbage-2f1c9a04-dead-4b1e-9c2a-000000000000"
    (garbage / "x").mkdir(parents=True)
    (garbage / "x" / "payload.bin").write_bytes(b"y" * 4096)
    when = time.time() - 180 * 60
    os.utime(garbage, (when, when))

    res = _reclaim(home, systmp, bind)

    assert res.returncode == 0, res.stderr
    assert not garbage.exists()


def test_a_partial_delete_is_reported(tmp_path):
    """REGRESSION (adversarial review, 2026-09-25). A tree the daemon cannot
    fully remove left `reclaimed` at 0, so nothing was logged at all — the tier
    printed "aggressive cleanup complete", reclaimed nothing, and retried every
    30 seconds with no way to see why. That is verbatim the pathology this
    function exists to fix, reproduced inside the fix.
    """
    home, systmp, bind = _sandbox(tmp_path)
    stuck = _numbered(systmp, 844, lock_pid=_dead_pid(), age_min=0)
    sealed = stuck / "sealed"
    sealed.mkdir()
    (sealed / "f").write_text("x")
    sealed.chmod(0o500)
    # Back-date LAST. Creating a subdirectory updates its PARENT's mtime, so
    # aging the tree before building the fixture left `stuck` zero minutes old
    # and the age gate spared it -- the arm passed while testing nothing.
    _when = time.time() - 180 * 60
    os.utime(stuck / ".lock", (_when, _when))
    os.utime(stuck, (_when, _when))  # rm cannot unlink f: no write permission on its parent
    try:
        res = _reclaim(home, systmp, bind)

        assert res.returncode == 0, res.stderr
        assert stuck.exists(), "fixture is removable — this arm proves nothing"
        assert "could not be removed" in _log(home), (
            "a partial delete was silent; the operator sees a clean cycle"
        )
    finally:
        sealed.chmod(0o700)


def test_fails_closed_on_a_NON_EMPTY_but_blind_snapshot(tmp_path):
    """REGRESSION, and the gap that made the self-test decorative.

    The existing blind-snapshot arm stubs `live_open_paths` to return NOTHING,
    which an emptiness test would also catch. The self-test exists for the case
    an emptiness test CANNOT catch: a reading that comes back full of records
    and is still structurally blind to the directories being judged — a partial
    /proc walk, another uid's writer, a root spelling the kernel does not use.
    MEASURED: replacing the positive containment check with `[[ -n "$snap" ]]`
    left all previous arms green.

    Here the stub returns plausible, non-empty, wholly unrelated paths.
    """
    home, systmp, bind = _sandbox(tmp_path)
    dead = _numbered(systmp, 845, lock_pid=_dead_pid(), age_min=180)

    res = _run(
        home,
        bind,
        # Plausible, non-empty, and wholly unrelated to the sandbox root. No
        # home-shaped path: these are literals in a public repo, and a synthetic
        # one still costs a reviewer the work of deciding it is synthetic.
        "live_open_paths() { printf '%s\\n' /usr/lib/libc.so.6 /var/log/syslog "
        "/var/cache/unrelated/thing /proc/1/maps; }; "
        f'reclaim_dead_pytest_dirs 60 "{systmp}"',
    )

    assert res.returncode == 0, res.stderr
    assert dead.exists(), (
        "deleted on a snapshot that is non-empty but cannot see this root — an "
        "emptiness check is not a self-test"
    )
    assert "failed its self-test" in _log(home)


def test_never_descends_a_symlinked_pytest_of_parent(tmp_path):
    """REGRESSION, and the most serious defect this function has had.

    `/tmp` is mode 1777, so any uid can create `/tmp/pytest-of-<anything>` as a
    SYMLINK pointing wherever they like. A bash glob follows it — MEASURED:
    `for d in "$root"/pytest-of-*/*` yields paths inside the link's target, and
    they satisfy `[[ -d ]]`, so the recursive delete leaves $root entirely and
    removes a tree the watchdog was never pointed at.

    A per-leaf `[[ -L "$d" ]]` guard does NOT cover this: the leaf is a real
    directory; the escape is the PARENT. Enumeration is `find -P`, which refuses
    to descend a symlinked component at all.
    """
    home, systmp, bind = _sandbox(tmp_path)
    outside = tmp_path / "somewhere-else"
    # The victim must be named so the GLOB would match it through the link
    # ("$root"/pytest-of-*/pytest-*). Named anything else, the glob finds nothing
    # and the arm passes whether or not the escape exists -- MEASURED: with the
    # victim called "precious", reverting to the glob left the whole suite green.
    victim = outside / "pytest-999"
    victim.mkdir(parents=True)
    (victim / "data.bin").write_bytes(b"q" * 4096)
    # Old, unlocked, no writer: every liveness signal says "reclaim me". The ONLY
    # thing that may save it is the enumeration refusing to leave $root.
    old = time.time() - 10000 * 60
    for t in (victim, outside):
        os.utime(t, (old, old))
    (systmp / "pytest-of-planted").symlink_to(outside)

    res = _reclaim(home, systmp, bind)

    assert res.returncode == 0, res.stderr
    assert victim.exists(), (
        "the sweep followed a symlinked pytest-of-* parent and deleted a tree "
        "outside its root"
    )
    assert (victim / "data.bin").exists()


def test_a_malformed_pid_falls_back_to_the_lock_timeout_rule(tmp_path):
    """REGRESSION. `tr -dc '0-9'` yields a digit string from anything, and
    without a shape check two spellings skip the three-day rule and reach the
    delete: a long run of garbage digits (no such /proc entry, so "dead"), and a
    ZERO-PADDED pid for a process that is genuinely alive (`/proc/0755` does not
    exist even when 755 does). The `(( pid > 1 ))` guard that the first /proc
    version removed had routed both to the safe fallback."""
    home, systmp, bind = _sandbox(tmp_path)
    garbage = _pof(systmp) / "pytest-846"
    padded = _pof(systmp) / "pytest-847"
    (garbage / "x").mkdir(parents=True)
    (garbage / ".lock").write_text("9" * 36)
    (padded / "x").mkdir(parents=True)
    (padded / ".lock").write_text(f"000{os.getpid()}")  # this process IS alive
    when = time.time() - 180 * 60  # past the gate, INSIDE the 3-day lock window
    for d in (garbage, padded):
        os.utime(d / ".lock", (when, when))
        os.utime(d, (when, when))

    res = _reclaim(home, systmp, bind)

    assert res.returncode == 0, res.stderr
    assert garbage.exists(), "a 36-digit pid must fall back to the lock-mtime rule"
    assert padded.exists(), "a zero-padded pid names a LIVE process — do not reap it"


def test_the_lock_timeout_is_not_reachable_from_configuration(tmp_path):
    """`load_config` re-sources watchgod.conf on every poll with no key
    allowlist, so ANY top-level global is settable from the config file. The
    lock timeout as a global could be set to 0, making the daemon reap trees
    pytest's own published rule says are live — MEASURED before the fix. The
    root was moved to a parameter for the same reason; this is the other half
    of that class, and the earlier test banned only the root's name."""
    body = _WATCHGOD.read_text()
    assert not re.search(r"^\s*PYTEST_LOCK_TIMEOUT_MIN=", body, re.M), (
        "the lock timeout is a top-level global again; load_config would make "
        "it settable from watchgod.conf"
    )
    start = body.index("reclaim_dead_pytest_dirs() {")
    fn = body[start : body.index("\n}\n", start)]
    assert "local lock_timeout_min=4320" in fn, "the lock timeout is no longer function-local"


# ── the call sites ───────────────────────────────────────────────────────


def test_each_tier_dispatches_the_reclaim_with_its_own_gate(tmp_path):
    """Wiring, behaviourally. Every arm above calls the helper directly with a
    literal gate, so the tier constants were unbound: MEASURED, changing 1440
    to 999999, 10080 to 1, 4320 to 99999, and DELETING the emergency call site
    outright all left the suite green.

    Drives `check_sys_tmp` at each tier with usage stubbed, capturing the gate
    the dispatch actually passes — and asserting NO root argument, since
    production must take the /tmp default rather than anything configurable.
    """
    home, _systmp, bind = _sandbox(tmp_path)
    for pct, want in ((60, "10080"), (75, "4320"), (90, "1440")):
        res = _run(
            home,
            bind,
            f"tmp_usage_pct() {{ echo {pct}; }}; "
            'reclaim_dead_pytest_dirs() { echo "RECLAIM-ARGS:$*"; }; '
            "clean_sys_yellow() { :; }; clean_sys_orange() { :; }; "
            "clean_sys_red() { :; }; "
            "check_sys_tmp",
        )
        assert res.returncode == 0, res.stderr
        calls = [ln for ln in res.stdout.splitlines() if ln.startswith("RECLAIM-ARGS:")]
        assert calls == [f"RECLAIM-ARGS:{want}"], (
            f"at {pct}% usage expected exactly one reclaim with gate {want}, got {calls}"
        )


def test_the_emergency_branch_dispatches_the_tightest_gate(tmp_path):
    """The still-critical branch inside clean_sys_red carries its own 60-minute
    gate, matching the file sweep beside it. Deleting that call site left the
    suite green before this arm existed."""
    home, _systmp, bind = _sandbox(tmp_path)
    res = _run(
        home,
        bind,
        "tmp_usage_pct() { echo 90; }; find() { :; }; "
        "queue_alert() { :; }; "
        'reclaim_dead_pytest_dirs() { echo "RECLAIM-ARGS:$*"; }; '
        "clean_sys_red",
    )
    assert res.returncode == 0, res.stderr
    assert "RECLAIM-ARGS:60" in res.stdout, (
        "the emergency branch no longer reclaims pytest trees at the 60-minute gate"
    )


def test_the_delete_root_defaults_to_tmp_and_is_not_config_reachable(tmp_path):
    """`load_config` sources its config file with NO key allowlist and runs
    before check_sys_tmp on every poll, so any global naming the delete root is
    settable from configuration. The root is a positional parameter for that
    reason, and production passes none.

    Structural, and deliberately so: the behavioural version of this arm would
    have to let the function loose on the real /tmp.
    """
    body = _WATCHGOD.read_text()
    start = body.index("reclaim_dead_pytest_dirs() {")
    fn = body[start : body.index("\n}\n", start)]
    assert 'root="${2:-/tmp}"' in fn, "the root default is no longer /tmp"
    assert not re.search(r"^\s*SYS_TMP_DIR=", body, re.M), (
        "a global delete-root variable is back; load_config would make it "
        "settable from the config file"
    )


def test_generic_sweeps_still_refuse_to_nibble_a_live_tree(tmp_path):
    """The name exclusion on the generic `find` sweeps is KEPT on purpose, and
    that is easy to 'clean up' later. Deleting individual files out of a live
    pytest tree is exactly the Zone A failure mode in a new place: the suite
    keeps running and fails on a file that vanished underneath it."""
    source = _WATCHGOD.read_text()
    for fn in ("clean_sys_yellow", "clean_sys_orange", "clean_sys_red"):
        start = source.index(f"{fn}() {{")
        body = source[start : source.index("\n}\n", start)]
        sweeps = [ln for ln in body.splitlines() if ln.strip().startswith("find /tmp")]
        assert sweeps, f"{fn}: no generic sweep found — has this been restructured?"
        for line in sweeps:
            assert '-not -path "*/pytest-*"' in line, (
                f"{fn}: a generic sweep lost its pytest exclusion. Whole-tree "
                f"reclamation by liveness replaces the exclusion; it does not "
                f"license file-by-file deletion inside a live tree.\n{line}"
            )


def test_the_incident_shape_is_reclaimed_THROUGH_the_red_dispatch(tmp_path):
    """The acceptance bar, driven the way production reaches it.

    `test_reclaims_the_incident_shape` calls the helper directly with gate=60,
    so it proves the logic and NOT that any real path gets there. MEASURED
    against the incident's own shape (a 240-minute dead tree): the tier gates
    10080/4320/1440 all SPARE it, and only the 60-minute emergency gate inside
    `clean_sys_red` reclaims it. That is the correct ladder — RED still holds a
    day of retention until the filesystem is critical — but it means the whole
    fix hangs on one call site, and deleting that call site used to leave the
    suite green.

    This drives `check_sys_tmp` at RED with usage stubbed above the emergency
    threshold, so the tree has to travel the real dispatch to die.
    """
    home, systmp, bind = _sandbox(tmp_path)
    incident = _numbered(systmp, 848, lock_pid=_dead_pid(), age_min=240)

    res = _run(
        home,
        bind,
        # Usage stays critical after the generic sweep, which is what opens the
        # emergency branch. The generic `find /tmp` sweeps are neutralised so the
        # arm cannot touch the real filesystem; the reclaim is called with an
        # explicit root and is unaffected.
        "tmp_usage_pct() { echo 90; }; "
        'queue_alert() { :; }; '
        'clean_sys_red() { log WARN "stub red"; reclaim_dead_pytest_dirs 60 "'
        + str(systmp)
        + '"; }; '
        "check_sys_tmp",
    )

    assert res.returncode == 0, res.stderr
    assert not incident.exists(), (
        "the incident's own shape survived a RED dispatch — the fix is not "
        "reachable through the path production actually takes"
    )
