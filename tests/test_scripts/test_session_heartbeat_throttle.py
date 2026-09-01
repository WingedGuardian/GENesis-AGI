"""Tests for the PostToolUse liveness throttle (scripts/hooks/session_heartbeat.py).

This gate runs on EVERY tool call in every session, so its two properties matter
more than usual: the false path must be cheap, and exactly ONE process may win a
window even when Claude Code issues tool calls in parallel.
"""

from __future__ import annotations

import contextlib
import importlib.util
import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
_SPEC = importlib.util.spec_from_file_location("session_heartbeat", _HOOKS / "session_heartbeat.py")
sh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sh)

_SID = "throttle-test-session"


def test_the_first_call_for_a_session_claims_the_window(tmp_path):
    """REGRESSION: the first call must claim, and once did not.

    The original implementation opened the stamp with mode "a" and then re-checked
    its MTIME under the lock. Opening for append CREATES the file with mtime=now,
    so the just-created stamp read as fresh (measured age 0.0006s) and the first
    call for every session returned False -- delaying a session's first liveness
    refresh by a whole window. The claim time is now the file's CONTENT, which
    creation cannot fake.
    """
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is True


def test_a_second_call_inside_the_window_is_refused(tmp_path):
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is True
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is False


def test_the_window_reopens_once_it_has_elapsed(tmp_path):
    assert sh.throttle_ok(_SID, window_s=0.05, sessions_dir=tmp_path) is True
    assert sh.throttle_ok(_SID, window_s=0.05, sessions_dir=tmp_path) is False
    time.sleep(0.06)
    assert sh.throttle_ok(_SID, window_s=0.05, sessions_dir=tmp_path) is True


def _stamp(tmp_path: Path, body: str, *, age_s: float, now: float | None = None) -> Path:
    """Write a stamp whose MTIME is ``age_s`` old, independent of its content.

    ``now`` overrides the reference point the age is measured back from. Any
    test that installs a fake clock MUST pass its own base. Aged against the
    REAL clock instead, the mtime lands in the fake base's future, the cheap
    mtime check falls through on its SIGN guard rather than on the age, and
    ``age_s`` becomes decorative -- MEASURED: 0.0 and 3600.0 then produce
    identical verdicts across the whole mutation matrix, so a test whose
    docstring credits the aging is crediting the wrong mechanism.
    """
    p = tmp_path / _SID / "heartbeat.stamp"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    old = (time.time() if now is None else now) - age_s
    os.utime(p, (old, old))
    return p


@pytest.mark.parametrize("body", ["not a float at all", "", "   ", "nan-ish\x00junk"])
def test_a_corrupt_stamp_does_not_wedge_a_session_forever(tmp_path, body):
    """A garbled stamp must not lock a session out of its peers' awareness.

    Note the REAL contract, which is weaker than "corrupt => claim immediately":
    the cheap mtime stat runs FIRST and is the whole reason the common path costs
    one syscall, so a corrupt stamp whose mtime is fresh is refused until the
    window elapses -- then the content is read, fails to parse, and claims. It
    self-heals within one window rather than never. An earlier version of this
    test asserted the stronger contract and failed, which was the test being
    wrong about the design rather than the design being wrong.
    """
    _stamp(tmp_path, body, age_s=120.0)
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is True


@pytest.mark.parametrize("body", ["not a float at all", ""])
def test_a_corrupt_but_recent_stamp_is_refused_until_the_window_elapses(tmp_path, body):
    """The mtime fast path is deliberately consulted before the content."""
    _stamp(tmp_path, body, age_s=0.0)
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is False


def test_an_empty_session_id_never_claims(tmp_path):
    assert sh.throttle_ok("", window_s=60.0, sessions_dir=tmp_path) is False


def test_an_unsafe_session_id_never_claims(tmp_path):
    """The id arrives from hook stdin; it must never escape the sessions dir.

    Delegated to hook_input.session_path rather than re-derived here -- this pins
    that the delegation actually happens.
    """
    assert sh.throttle_ok("../../etc/passwd", window_s=60.0, sessions_dir=tmp_path) is False
    assert not (tmp_path / ".." / ".." / "etc").exists()


def test_the_stamp_records_the_claim_time(tmp_path):
    before = time.time()
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is True
    stamp = tmp_path / _SID / "heartbeat.stamp"
    claimed = float(stamp.read_text().strip())
    assert before <= claimed <= time.time()


def _claim(args) -> bool:
    """Run in a separate PROCESS -- the lock is only meaningful across processes.

    The barrier is what makes this a RACE. Without it, mp.Pool dispatches through
    a handler thread and each child pays ~80ms of module import before touching
    the stamp, so the calls are staggered by orders of magnitude more than the
    window they are competing for. MEASURED: with the flock deleted, the
    unbarriered version still reported exactly one winner every time -- the test
    passed while defending nothing. Barriered, the same deletion produces 2-3
    winners per trial.
    """
    d, sid, barrier = args
    import importlib.util as iu

    hooks = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
    spec = iu.spec_from_file_location("sh_child", hooks / "session_heartbeat.py")
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    barrier.wait(timeout=30)  # every child has imported; NOW contend
    return mod.throttle_ok(sid, window_s=60.0, sessions_dir=Path(d))


def test_parallel_tool_calls_in_one_session_produce_exactly_one_winner(tmp_path):
    """Claude Code issues tool calls in PARALLEL, so two hook processes can see a
    stale stamp in the same instant. Without the non-blocking flock AND the
    re-check under it, both would write."""
    n = 12
    with mp.Manager() as mgr:
        barrier = mgr.Barrier(n)
        with mp.Pool(n) as pool:
            wins = pool.map(_claim, [(str(tmp_path), _SID, barrier)] * n)
    assert sum(wins) == 1, f"expected exactly one winner, got {sum(wins)}"


class _SequencedClock:
    """Hands out scripted readings in order, then holds the last one.

    A FROZEN clock cannot test the branch below. Freezing makes the pre-lock and
    the under-lock reading identical, which models the OLD code no matter what
    the new code does -- the first version of this probe froze the clock and
    reported the bug as still present after it had been fixed. Holding (rather
    than raising) past the end means an extra clock read cannot be mistaken for
    a defect.
    """

    def __init__(self, readings: list[float]) -> None:
        self._it = iter(readings)
        self._last = readings[-1]
        self.reads = 0

    def time(self) -> float:
        # The COUNT is the instrument. throttle_ok reads the clock once before
        # the cheap mtime check and once more under the lock, so reads == 2 is
        # proof the under-lock branch was actually reached -- which a verdict
        # alone cannot give, because the fast path and the branch under test
        # both refuse with False.
        self.reads += 1
        with contextlib.suppress(StopIteration):
            self._last = next(self._it)
        return self._last


def test_the_race_loser_is_refused_though_its_clock_read_predates_the_winner(tmp_path, monkeypatch):
    """The deterministic half of the parallel test above, which is a RACE.

    CI caught this as "expected exactly one winner, got 2" on a commit that had
    already been green -- ordering-dependent, so neither its red nor its green is
    evidence. This models the interleaving instead of running it.

    The loser reads the clock BEFORE the flock; the winner then takes the lock
    and writes its own, later claim time. If the loser compares that stale
    reading against the winner's stamp, ``now - last`` is NEGATIVE by
    microseconds, the backwards-clock guard reads it as "not inside the window",
    and the loser claims too -- the flock excluding nothing in the one case it
    exists for. The fix is to read the clock UNDER the lock, where a completed
    write is always in the past.

    The mtime is aged against the FAKE clock's base (``now=base``), which is
    what makes the aging load-bearing: the cheap mtime check runs first and
    would otherwise refuse before this branch is reached. Aged against the real
    clock the mtime would land in ``base``'s future and the check would fall
    through on its sign guard instead, leaving ``age_s`` decorative.

    ``throttle_ok`` fails CLOSED to False, which is also what this test asserts,
    so the verdict alone proves nothing: MEASURED, a broken double (a class with
    no ``.time`` attribute at all) returns False here, and so does the mtime fast
    path when the aging is wrong. The read COUNT is what closes that -- two reads
    means the under-lock branch really ran. Without it, aging the stamp to 0s
    left all three tests green while none of them reached the branch (MEASURED).
    """
    base = time.time()
    won_by = 0.000_050  # the sibling beat us to the lock by 50us
    _stamp(tmp_path, repr(base + won_by), age_s=3600.0, now=base)
    clock = _SequencedClock([base, base + 0.000_200])
    monkeypatch.setattr(sh, "time", clock)
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is False
    assert clock.reads == 2, (
        f"refused after {clock.reads} clock read(s): the mtime fast path "
        "short-circuited and the under-lock branch under test never ran"
    )


def test_a_clock_tie_with_the_winner_is_still_refused(tmp_path, monkeypatch):
    """`0 <=`, not `0 <`: the two clock reads can land on the SAME float.

    time.time() returns a double whose ULP at epoch magnitude is ~2.4e-07s, so
    two reads closer together than that are EQUAL and the race difference is
    exactly 0.0 rather than positive. Without this case the tightening
    ``0 <= `` -> ``0 < `` passed all 21 other tests in this file (MEASURED) while
    silently reopening the race at the tie -- the loser would fall through and
    claim a window the winner already holds.
    """
    base = time.time()
    won_by = 0.000_050
    _stamp(tmp_path, repr(base + won_by), age_s=3600.0, now=base)
    # the post-lock read lands on exactly the winner's claim time
    clock = _SequencedClock([base, base + won_by])
    monkeypatch.setattr(sh, "time", clock)
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is False
    assert clock.reads == 2, (
        f"refused after {clock.reads} clock read(s): the mtime fast path "
        "short-circuited and the tie was never compared"
    )


def test_a_genuinely_future_stamp_still_claims_under_the_same_clock(tmp_path, monkeypatch):
    """Guard-the-guard for the test above: the two properties must BOTH hold.

    Refusing the race loser must not be bought by dropping the backwards-clock
    guard. A stamp an hour ahead is a real clock step (VM restore, NTP
    correction), not a sibling -- an unguarded comparison reads it as "inside the
    window" and refuses every write until wall time catches up, which would make
    an actively working session vanish from its peers entirely. Same sequenced
    clock, so a fix that simply deleted the guard turns this red.

    This one needs no read-count assertion: the mtime fast path can only refuse
    or fall through, so a True verdict is itself proof the under-lock branch ran.
    That is also why it is the case that validates the fake clock -- a broken
    double turns it red, where the two above fail closed to the value they
    assert. It therefore stays even though
    ``test_a_future_claim_time_does_not_suppress_the_refresh`` also covers the
    guard.
    """
    base = time.time()
    _stamp(tmp_path, repr(base + 3600.0), age_s=7200.0, now=base)
    monkeypatch.setattr(sh, "time", _SequencedClock([base, base + 0.000_200]))
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is True


def test_different_sessions_never_contend(tmp_path):
    """The stamp is keyed by session id, so there is no shared state to contend
    over -- every session must claim its own first window."""
    n = 6
    with mp.Manager() as mgr:
        barrier = mgr.Barrier(n)
        with mp.Pool(n) as pool:
            wins = pool.map(_claim, [(str(tmp_path), f"sess-{i}", barrier) for i in range(n)])
    assert sum(wins) == n, f"cross-session contention: only {sum(wins)} of {n} claimed"


def test_the_refused_path_does_not_open_a_database(tmp_path, monkeypatch):
    """The whole design rests on the common path being cheap. If a future edit
    moves a DB open above the throttle, this fails.

    REWRITTEN: the first version of this test could not fail. It only ever
    called `throttle_ok`, which contains no sqlite code path at all -- so
    `assert opened == []` was true by construction, and moving `upsert_sync`
    ABOVE the throttle (the exact regression named in the docstring) left it
    green. The ordering it guards lives in the CALLER,
    `session_observer_hook._maybe_refresh_heartbeat`, in a module this file did
    not even import.
    """
    import importlib.util as _iu
    import sqlite3

    spec = _iu.spec_from_file_location("obs_for_test", _HOOKS / "session_observer_hook.py")
    obs = _iu.module_from_spec(spec)
    spec.loader.exec_module(obs)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # The DB file must EXIST at the path the suite's isolation resolves to, or
    # `_maybe_refresh_heartbeat` returns at its exists() guard before ever
    # connecting and the spy below observes nothing -- the test would then pass
    # whatever the ordering, which is exactly how the previous version of this
    # test was vacuous. conftest's autouse `_isolate_genesis_db_path` patches
    # the FUNCTION `genesis.env.genesis_db_path` (NOT the env var, so setenv
    # here would be inert) to this filename under tmp_path. Creating the file
    # opts INTO that isolation rather than fighting it -- the real database is
    # never reachable from here.
    isolated_db = tmp_path / "isolated-genesis.db"
    sqlite3.connect(str(isolated_db)).close()
    from genesis.env import genesis_db_path

    assert Path(genesis_db_path()) == isolated_db, (
        "conftest's DB isolation moved; this test now points at the wrong file "
        "and would silently stop covering the ordering it names"
    )

    obs._maybe_refresh_heartbeat(_SID)  # claims the window

    opened: list[str] = []
    real_connect = sqlite3.connect

    def _spy(*a, **k):
        opened.append(str(a[0]) if a else "?")
        return real_connect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", _spy)
    obs._maybe_refresh_heartbeat(_SID)  # refused by the throttle
    assert opened == [], f"a database open moved above the throttle: {opened}"


def test_the_refused_path_costs_about_one_stat(tmp_path):
    """A soft budget, deliberately loose (measured median 0.044ms, p95 0.09ms).

    Pinned at 50x the measured p95 so it catches a DB OPEN moving above the
    throttle, without flaking on a loaded CI runner.

    It deliberately does NOT claim to catch an IMPORT moving above the throttle:
    this loops a warm in-process function, while a module-scope import is paid
    once per PROCESS and would be invisible here. That cost is a real risk on
    this path and is covered by keeping the heavy imports lazy in
    session_heartbeat.py, not by this assertion.
    """
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is True
    n = 200
    t0 = time.perf_counter()
    for _ in range(n):
        sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path)
    per_call_ms = (time.perf_counter() - t0) / n * 1000
    assert per_call_ms < 5.0, (
        f"the throttled-off path costs {per_call_ms:.3f}ms per call -- something "
        "expensive moved above the throttle check"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses mode bits")
def test_a_readonly_sessions_dir_fails_closed(tmp_path):
    """Fail CLOSED on any error: a missed refresh costs an awareness line, while
    a raising hook costs the tool call."""
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=ro) is False
    finally:
        os.chmod(ro, 0o700)


# --- clock moving BACKWARDS (VM restore, NTP correction) -------------------
#
# Both throttle checks compare `now - <stored>` against the window. A backwards
# clock step makes that difference NEGATIVE, and a negative number is always
# `< window`, so both checks read "inside the window" and refuse -- suppressing
# every liveness write until wall time catches up. A correction larger than
# _STALE_THRESHOLD therefore makes an ACTIVELY WORKING session vanish from its
# peers entirely. The repo already settled this direction elsewhere:
# scripts/genesis_urgent_alerts.py:350 guards `if elapsed < 0: return False`
# with the comment "future marker -> do not suppress; emit".


def test_a_future_stamp_mtime_does_not_suppress_the_refresh(tmp_path):
    """The cheap mtime fast path must treat a FUTURE stamp as claimable."""
    # content is genuinely old, so only the mtime can refuse this
    _stamp(tmp_path, repr(time.time() - 7200.0), age_s=-3600.0)
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is True


def test_a_future_claim_time_does_not_suppress_the_refresh(tmp_path):
    """The under-lock CONTENT check needs the same guard as the mtime check.

    Guard-the-guard: the mtime is deliberately aged past the window so the fast
    path CANNOT be what returns True here -- otherwise this would pass without
    the content check ever being reached, and would keep passing if that second
    call site were left unfixed.
    """
    _stamp(tmp_path, repr(time.time() + 3600.0), age_s=7200.0)
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is True
