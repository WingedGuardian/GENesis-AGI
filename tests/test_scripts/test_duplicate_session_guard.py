"""Tests for scripts/hooks/duplicate_session_guard.py (+ proc_ident helpers).

The guard enforces one live executor per CC conversation transcript
(2026-07-13 incident: a dropped SSH left a session executing headless while
a resume spawned a second executor over the same transcript). Newest wins;
deny requires positive evidence of every fact — all degraded states allow.

The decision function is pure (injected liveness), so no test depends on
real process state or the wall clock; register/guard flow tests monkeypatch
the module's identity + owners-dir seams.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "hooks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HOOKS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


proc_ident = _load("proc_ident")
guard = _load("duplicate_session_guard")


# -- proc_ident ---------------------------------------------------------------


def test_transcript_key_is_stable_and_path_safe():
    key = proc_ident.transcript_key("/home/u/.claude/projects/x/abc.jsonl")
    assert key == proc_ident.transcript_key("/home/u/.claude/projects/x/abc.jsonl")
    assert len(key) == 16
    assert key.isalnum()
    assert key != proc_ident.transcript_key("/other/path.jsonl")


def test_read_starttime_of_own_process():
    st = proc_ident.read_starttime(os.getpid())
    assert isinstance(st, int) and st > 0


def test_is_alive_requires_matching_starttime():
    pid = os.getpid()
    st = proc_ident.read_starttime(pid)
    assert proc_ident.is_alive(pid, st)
    assert not proc_ident.is_alive(pid, st + 1)  # pid-reuse guard


def test_is_alive_dead_pid_is_false():
    # PID 2^22+ is above the default pid_max; never a live process.
    assert not proc_ident.is_alive(2**22 + 1234, 1)


def test_read_ppid_walks_to_a_real_parent():
    ppid = proc_ident.read_ppid(os.getpid())
    assert ppid == os.getppid()


# -- decide(): the pure newest-wins core ---------------------------------------


def _conflict(*executors):
    return {
        "transcript_path": "/t/x.jsonl",
        "executors": [{"pid": p, "starttime": s, "session_id": "sid"} for p, s in executors],
    }


def _alive_set(live: set[tuple[int, int]]):
    return lambda pid, st: (pid, st) in live


def test_decide_no_conflict_allows():
    action, _ = guard.decide(None, 100, 5)
    assert action == guard.ALLOW


def test_decide_unknown_self_allows():
    conflict = _conflict((100, 5), (200, 9))
    action, reason = guard.decide(conflict, None, None, _alive_set({(100, 5), (200, 9)}))
    assert action == guard.ALLOW
    assert "ancestor" in reason


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"executors": "nope"},
        {"executors": [{"pid": 1, "starttime": 2}]},  # fewer than two
        {"executors": [{"pid": "1", "starttime": 2}, {"pid": 3, "starttime": 4}]},
        {"executors": [None, {"pid": 3, "starttime": 4}]},
    ],
)
def test_decide_malformed_conflict_allows(bad):
    action, _ = guard.decide(bad, 100, 5, _alive_set({(100, 5)}))
    assert action == guard.ALLOW


def test_decide_stale_conflict_allows():
    # Only one of the two recorded executors is still alive -> resolved.
    conflict = _conflict((100, 5), (200, 9))
    action, reason = guard.decide(conflict, 100, 5, _alive_set({(100, 5)}))
    assert action == guard.ALLOW
    assert "stale" in reason


def test_decide_pid_reuse_is_stale():
    # Same pid, different starttime: recycled pid is NOT the recorded executor.
    conflict = _conflict((100, 5), (200, 9))
    alive = _alive_set({(100, 5), (200, 777)})  # 200 was recycled
    action, reason = guard.decide(conflict, 100, 5, alive)
    assert action == guard.ALLOW
    assert "stale" in reason


def test_decide_third_executor_not_in_file_allows():
    conflict = _conflict((100, 5), (200, 9))
    alive = _alive_set({(100, 5), (200, 9), (300, 12)})
    action, reason = guard.decide(conflict, 300, 12, alive)
    assert action == guard.ALLOW
    assert "not among" in reason


def test_decide_newest_wins_older_denied():
    conflict = _conflict((100, 5), (200, 9))
    alive = _alive_set({(100, 5), (200, 9)})
    action, reason = guard.decide(conflict, 100, 5, alive)  # older
    assert action == guard.DENY
    assert "200" in reason
    action, _ = guard.decide(conflict, 200, 9, alive)  # newer
    assert action == guard.ALLOW


def test_decide_starttime_tie_breaks_on_pid():
    # Same jiffy spawn: total order (starttime, pid) -> higher pid wins.
    conflict = _conflict((100, 5), (200, 5))
    alive = _alive_set({(100, 5), (200, 5)})
    assert guard.decide(conflict, 100, 5, alive)[0] == guard.DENY
    assert guard.decide(conflict, 200, 5, alive)[0] == guard.ALLOW


# -- register / guard flows -----------------------------------------------------


@pytest.fixture
def owners(tmp_path, monkeypatch):
    d = tmp_path / "session-owners"
    monkeypatch.setattr(guard, "OWNERS_DIR", d)
    return d


def _identify_as(monkeypatch, pid: int, starttime: int, alive: set[tuple[int, int]]):
    monkeypatch.setattr(guard, "_self_identity", lambda: (pid, starttime))
    monkeypatch.setattr(guard.proc_ident, "is_alive", lambda p, s: (p, s) in alive)


PAYLOAD = {"transcript_path": "/t/x.jsonl", "session_id": "sid-1"}
KEY = proc_ident.transcript_key("/t/x.jsonl")


def test_register_without_transcript_path_touches_nothing(owners, monkeypatch):
    _identify_as(monkeypatch, 100, 5, {(100, 5)})
    assert guard._register({}) == 0
    assert guard._register({"transcript_path": ""}) == 0
    assert not owners.exists()


def test_register_claims_unowned_transcript(owners, monkeypatch):
    _identify_as(monkeypatch, 100, 5, {(100, 5)})
    assert guard._register(PAYLOAD) == 0
    owner = json.loads((owners / f"{KEY}.json").read_text())
    assert (owner["pid"], owner["starttime"]) == (100, 5)
    assert owner["transcript_path"] == "/t/x.jsonl"
    assert not (owners / f"{KEY}.conflict").exists()


def test_register_overwrites_dead_owner_and_clears_conflict(owners, monkeypatch):
    _identify_as(monkeypatch, 200, 9, {(200, 9)})  # old owner (100,5) is dead
    owners.mkdir(parents=True)
    (owners / f"{KEY}.json").write_text(
        json.dumps({"pid": 100, "starttime": 5, "transcript_path": "/t/x.jsonl"})
    )
    (owners / f"{KEY}.conflict").write_text(json.dumps(_conflict((100, 5), (200, 9))))
    assert guard._register(PAYLOAD) == 0
    owner = json.loads((owners / f"{KEY}.json").read_text())
    assert owner["pid"] == 200
    assert not (owners / f"{KEY}.conflict").exists()


def test_register_live_foreign_owner_writes_conflict_not_steal(owners, monkeypatch):
    _identify_as(monkeypatch, 200, 9, {(100, 5), (200, 9)})
    owners.mkdir(parents=True)
    (owners / f"{KEY}.json").write_text(
        json.dumps({"pid": 100, "starttime": 5, "session_id": "sid-0"})
    )
    assert guard._register(PAYLOAD) == 0
    owner = json.loads((owners / f"{KEY}.json").read_text())
    assert owner["pid"] == 100  # ownership NOT stolen
    conflict = json.loads((owners / f"{KEY}.conflict").read_text())
    pids = [e["pid"] for e in conflict["executors"]]
    assert pids == [100, 200]  # deterministic (starttime, pid) order


def test_register_conflict_bytes_deterministic_from_both_sides(owners, monkeypatch):
    # Whichever executor writes the conflict, content is identical -> the
    # concurrent double-write race is harmless.
    owners.mkdir(parents=True)
    (owners / f"{KEY}.json").write_text(json.dumps({"pid": 100, "starttime": 5, "session_id": "a"}))
    _identify_as(monkeypatch, 200, 9, {(100, 5), (200, 9)})
    guard._register({**PAYLOAD, "session_id": "b"})
    first = (owners / f"{KEY}.conflict").read_bytes()
    first_json = json.loads(first)

    (owners / f"{KEY}.conflict").unlink()
    (owners / f"{KEY}.json").write_text(json.dumps({"pid": 200, "starttime": 9, "session_id": "b"}))
    _identify_as(monkeypatch, 100, 5, {(100, 5), (200, 9)})
    guard._register({**PAYLOAD, "session_id": "a"})
    second_json = json.loads((owners / f"{KEY}.conflict").read_bytes())
    assert first_json["executors"] == second_json["executors"]


def test_guard_fast_path_no_conflict(owners, monkeypatch):
    _identify_as(monkeypatch, 100, 5, {(100, 5)})
    assert guard._guard(PAYLOAD) == 0


def test_guard_missing_transcript_path_allows(owners):
    assert guard._guard({}) == 0


def test_guard_denies_older_executor(owners, monkeypatch, capsys):
    owners.mkdir(parents=True)
    (owners / f"{KEY}.conflict").write_text(json.dumps(_conflict((100, 5), (200, 9))))
    _identify_as(monkeypatch, 100, 5, {(100, 5), (200, 9)})
    assert guard._guard(PAYLOAD) == 2
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert f"{KEY}.override" in err  # actual override path in the message


def test_guard_allows_newer_executor(owners, monkeypatch):
    owners.mkdir(parents=True)
    (owners / f"{KEY}.conflict").write_text(json.dumps(_conflict((100, 5), (200, 9))))
    _identify_as(monkeypatch, 200, 9, {(100, 5), (200, 9)})
    assert guard._guard(PAYLOAD) == 0


def test_guard_stale_conflict_self_heals(owners, monkeypatch):
    owners.mkdir(parents=True)
    (owners / f"{KEY}.conflict").write_text(json.dumps(_conflict((100, 5), (200, 9))))
    (owners / f"{KEY}.override").write_text("")
    _identify_as(monkeypatch, 100, 5, {(100, 5)})  # peer died
    # Override file short-circuits first; remove it to reach the heal path.
    (owners / f"{KEY}.override").unlink()
    assert guard._guard(PAYLOAD) == 0
    assert not (owners / f"{KEY}.conflict").exists()


def test_guard_override_file_allows_older(owners, monkeypatch):
    owners.mkdir(parents=True)
    (owners / f"{KEY}.conflict").write_text(json.dumps(_conflict((100, 5), (200, 9))))
    (owners / f"{KEY}.override").write_text("")
    _identify_as(monkeypatch, 100, 5, {(100, 5), (200, 9)})
    assert guard._guard(PAYLOAD) == 0


def test_guard_env_override_allows_older(owners, monkeypatch):
    owners.mkdir(parents=True)
    (owners / f"{KEY}.conflict").write_text(json.dumps(_conflict((100, 5), (200, 9))))
    _identify_as(monkeypatch, 100, 5, {(100, 5), (200, 9)})
    monkeypatch.setenv("GENESIS_ALLOW_DUAL_SESSION", "1")
    assert guard._guard(PAYLOAD) == 0


def test_guard_torn_conflict_file_allows(owners, monkeypatch):
    owners.mkdir(parents=True)
    (owners / f"{KEY}.conflict").write_text('{"executors": [{"pid": 1')  # torn
    _identify_as(monkeypatch, 100, 5, {(100, 5)})
    assert guard._guard(PAYLOAD) == 0


def test_main_is_fail_open_on_internal_error(owners, monkeypatch, capsys):
    # Any unexpected exception in either mode must exit 0, never block.
    monkeypatch.setattr(guard, "_guard", lambda payload: 1 / 0)
    monkeypatch.setattr(guard.sys, "stdin", __import__("io").StringIO("{}"))
    monkeypatch.setattr(guard.sys, "argv", ["duplicate_session_guard.py"])
    assert guard.main() == 0
    assert "failing open" in capsys.readouterr().err
