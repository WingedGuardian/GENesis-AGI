"""Container-side coordinator: fetch capacity → ask → APPROVE → host execute.

Pure dependency injection — a fake remote records verb calls, a scripted ``ask``
returns the user's reply. Proves: no execute without APPROVE; DENY/timeout and a
capacity-read failure both short-circuit before any mutation; the proposal text
is shown to the user; APPROVE dispatches the correct host verb.
"""

from __future__ import annotations

import genesis.guardian.provisioning.container as container_mod
from genesis.guardian.provisioning.container import (
    _poll_vzdump,
    coordinate_grow_disk,
    coordinate_grow_memory,
    coordinate_vzdump,
)

_GIB = 1024**3


class FakeRemote:
    def __init__(self, status, grow_result=None):
        self._status = status
        self._grow_result = grow_result or {"ok": True, "stage": "executed"}
        self.grow_disk_args = None
        self.grow_mem_args = None

    async def provision_status(self):
        return self._status

    async def request_grow_disk(self, disk, add_gib):
        self.grow_disk_args = (disk, add_gib)
        return self._grow_result

    async def request_grow_memory(self, new_mib):
        self.grow_mem_args = new_mib
        return self._grow_result


def _ask(reply):
    async def _inner(text):
        _inner.prompt = text
        return reply
    return _inner


def _status_ok():
    return {"ok": True, "capacity": {
        "disks": {"scsi1": 32 * _GIB}, "storage_free_bytes": 574 * _GIB,
        "vm_memory_mib": 21500,
    }}


async def test_approve_dispatches_disk_grow():
    remote = FakeRemote(_status_ok())
    ask = _ask("APPROVE")
    res = await coordinate_grow_disk(remote, ask, disk="scsi1", add_gib=32)
    assert res["ok"] is True and res["stage"] == "executed"
    assert remote.grow_disk_args == ("scsi1", 32)
    assert "scsi1" in ask.prompt and "32G" in ask.prompt  # user saw the proposal


async def test_deny_never_dispatches():
    remote = FakeRemote(_status_ok())
    res = await coordinate_grow_disk(remote, _ask("DENY"), disk="scsi1", add_gib=32)
    assert res["stage"] == "denied"
    assert remote.grow_disk_args is None


async def test_timeout_reply_none_never_dispatches():
    remote = FakeRemote(_status_ok())
    res = await coordinate_grow_disk(remote, _ask(None), disk="scsi1", add_gib=32)
    assert res["stage"] == "denied"
    assert remote.grow_disk_args is None


async def test_capacity_read_failure_short_circuits():
    remote = FakeRemote({"ok": False, "error": "unreachable"})
    called = {"asked": False}

    async def ask(text):
        called["asked"] = True
        return "APPROVE"

    res = await coordinate_grow_disk(remote, ask, disk="scsi1", add_gib=32)
    assert res["stage"] == "no_capacity"
    assert called["asked"] is False  # never even asked
    assert remote.grow_disk_args is None


async def test_lowercase_approve_is_accepted():
    remote = FakeRemote(_status_ok())
    res = await coordinate_grow_disk(remote, _ask("approve"), disk="scsi1", add_gib=32)
    assert res["ok"] is True
    assert remote.grow_disk_args == ("scsi1", 32)


async def test_memory_grow_approve_dispatches():
    remote = FakeRemote(_status_ok())
    ask = _ask("APPROVE")
    res = await coordinate_grow_memory(remote, ask, new_mib=24576)
    assert res["ok"] is True
    assert remote.grow_mem_args == 24576
    assert "reboot" in ask.prompt.lower()  # user warned about downtime


# ── JIT backup→grow chain + explicit vzdump (two-phase, tracked poller) ───


class FakeChainRemote(FakeRemote):
    """FakeRemote + the vzdump verbs, with a scripted status sequence."""

    def __init__(self, status, grow_result=None, *, start_result=None, states=None):
        super().__init__(status, grow_result)
        self.start_result = start_result or {
            "ok": True, "stage": "started",
            "upid": "UPID:pve:000A1B2C:001122DD:68765432:vzdump:100:u@pve!t:",
        }
        self.states = list(states or [])
        self.start_calls = 0
        self.status_calls = 0

    async def request_vzdump_start(self):
        self.start_calls += 1
        return self.start_result

    async def request_vzdump_status(self, upid=""):
        self.status_calls += 1
        return self.states.pop(0) if self.states else {"ok": True, "state": "unknown"}


def _stale_status(in_flight=""):
    s = _status_ok()
    s["backup"] = {
        "age_days": None, "require_recent_backup": True,
        "backup_max_age_days": 14, "in_flight_upid": in_flight,
    }
    return s


def _fresh_status():
    s = _status_ok()
    s["backup"] = {
        "age_days": 1.0, "require_recent_backup": True,
        "backup_max_age_days": 14, "in_flight_upid": "",
    }
    return s


class _CapturedTask:
    """Capture the poller coroutine instead of scheduling it, so tests drive
    it deterministically."""

    def __init__(self, monkeypatch):
        self.coros = []
        monkeypatch.setattr(
            container_mod, "tracked_task",
            lambda coro, **kw: self.coros.append(coro) or None,
        )


class _Notify:
    def __init__(self):
        self.messages = []

    async def __call__(self, text):
        self.messages.append(text)


async def test_stale_backup_turns_grow_into_one_approval_chain(monkeypatch):
    captured = _CapturedTask(monkeypatch)
    remote = FakeChainRemote(
        _stale_status(),
        states=[{"ok": True, "state": "running"}, {"ok": True, "state": "verified"}],
    )
    notify = _Notify()
    ask = _ask("APPROVE")
    res = await coordinate_grow_disk(
        remote, ask, disk="scsi1", add_gib=32, notify=notify, poll_interval_s=0.0,
    )
    assert res["stage"] == "chain_started" and res["upid"].startswith("UPID:")
    # S8: the single approval text must disclose the whole chain + the gap
    assert "CHAIN" in ask.prompt and "AUTOMATICALLY" in ask.prompt
    assert remote.grow_disk_args is None, "grow must NOT run before verification"
    # drive the captured poller to completion
    assert len(captured.coros) == 1
    await captured.coros[0]
    assert remote.grow_disk_args == ("scsi1", 32)
    assert any("Chain complete" in m for m in notify.messages)


async def test_stale_backup_deny_runs_nothing(monkeypatch):
    captured = _CapturedTask(monkeypatch)
    remote = FakeChainRemote(_stale_status())
    res = await coordinate_grow_disk(remote, _ask("DENY"), disk="scsi1", add_gib=32)
    assert res["stage"] == "denied"
    assert remote.start_calls == 0 and remote.grow_disk_args is None
    assert captured.coros == []


async def test_backup_in_flight_refuses_before_asking(monkeypatch):
    _CapturedTask(monkeypatch)
    remote = FakeChainRemote(_stale_status(in_flight="UPID:pve:1:1:68765432:vzdump:100:u@p!t:"))
    ask = _ask("APPROVE")
    res = await coordinate_grow_disk(remote, ask, disk="scsi1", add_gib=32)
    assert res["stage"] == "backup_in_flight"
    assert not hasattr(ask, "prompt"), "must not prompt while a backup runs"


async def test_fresh_backup_keeps_plain_grow(monkeypatch):
    captured = _CapturedTask(monkeypatch)
    remote = FakeChainRemote(_fresh_status())
    ask = _ask("APPROVE")
    res = await coordinate_grow_disk(remote, ask, disk="scsi1", add_gib=32)
    assert res == {"ok": True, "stage": "executed"}
    assert "CHAIN" not in ask.prompt
    assert remote.start_calls == 0 and captured.coros == []


async def test_chain_backup_start_failure_aborts(monkeypatch):
    captured = _CapturedTask(monkeypatch)
    remote = FakeChainRemote(
        _stale_status(), start_result={"ok": False, "error": "denied"},
    )
    res = await coordinate_grow_disk(remote, _ask("APPROVE"), disk="scsi1", add_gib=32)
    assert res["stage"] == "backup_start_failed"
    assert remote.grow_disk_args is None and captured.coros == []


async def test_poller_backup_failure_never_grows():
    remote = FakeChainRemote(_stale_status(), states=[
        {"ok": True, "state": "unknown"},  # transient probe tolerated
        {"ok": False, "state": "failed", "error": "task exitstatus: job errors"},
    ])
    notify = _Notify()

    async def _grow_leg():
        return await remote.request_grow_disk("scsi1", 32)

    res = await _poll_vzdump(
        remote, notify, upid="UPID:x", poll_interval_s=0.0, wall_s=30.0,
        on_verified=_grow_leg,
    )
    assert res["state"] == "failed"
    assert remote.grow_disk_args is None
    assert any("NOT executed" in m for m in notify.messages)


async def test_poller_wall_bound_is_unverified_not_failed():
    remote = FakeChainRemote(_stale_status())
    notify = _Notify()
    res = await _poll_vzdump(
        remote, notify, upid="UPID:x", poll_interval_s=0.0, wall_s=0.0,
    )
    assert res["stage"] == "wall_bound"
    assert any("UNVERIFIED" in m for m in notify.messages)


async def test_chain_grow_leg_refusal_notifies_and_stops():
    """S8: a failed post-backup re-check alerts and STOPS — never re-asks."""
    remote = FakeChainRemote(
        _stale_status(),
        grow_result={"ok": False, "stage": "recheck_failed"},
        states=[{"ok": True, "state": "verified"}],
    )
    notify = _Notify()

    async def _grow_leg():
        return await remote.request_grow_disk("scsi1", 32)

    res = await _poll_vzdump(
        remote, notify, upid="UPID:x", poll_interval_s=0.0, wall_s=30.0,
        on_verified=_grow_leg,
    )
    assert res["stage"] == "recheck_failed"
    assert any("did NOT run" in m for m in notify.messages)


async def test_coordinate_vzdump_approve_starts_and_tracks(monkeypatch):
    captured = _CapturedTask(monkeypatch)
    remote = FakeChainRemote(_fresh_status(), states=[{"ok": True, "state": "verified"}])
    res = await coordinate_vzdump(remote, _ask("APPROVE"))
    assert res["stage"] == "started" and res["upid"].startswith("UPID:")
    assert len(captured.coros) == 1
    await captured.coros[0]  # runs to verified without error


async def test_coordinate_vzdump_deny_and_inflight(monkeypatch):
    _CapturedTask(monkeypatch)
    remote = FakeChainRemote(_fresh_status())
    assert (await coordinate_vzdump(remote, _ask("DENY")))["stage"] == "denied"
    assert remote.start_calls == 0
    remote2 = FakeChainRemote(_stale_status(in_flight="UPID:pve:1:1:68765432:vzdump:100:u@p!t:"))
    assert (await coordinate_vzdump(remote2, _ask("APPROVE")))["stage"] == "backup_in_flight"
