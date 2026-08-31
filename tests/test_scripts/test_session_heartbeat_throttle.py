"""Tests for the PostToolUse liveness throttle (scripts/hooks/session_heartbeat.py).

This gate runs on EVERY tool call in every session, so its two properties matter
more than usual: the false path must be cheap, and exactly ONE process may win a
window even when Claude Code issues tool calls in parallel.
"""

from __future__ import annotations

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


def _stamp(tmp_path: Path, body: str, *, age_s: float) -> Path:
    """Write a stamp whose MTIME is ``age_s`` old, independent of its content."""
    p = tmp_path / _SID / "heartbeat.stamp"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    old = time.time() - age_s
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
    moves a DB open above the throttle, this fails."""
    import sqlite3

    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is True

    opened: list[str] = []
    real_connect = sqlite3.connect

    def _spy(*a, **k):
        opened.append(str(a[0]) if a else "?")
        return real_connect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", _spy)
    assert sh.throttle_ok(_SID, window_s=60.0, sessions_dir=tmp_path) is False
    assert opened == [], f"the throttled-off path opened a database: {opened}"


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
