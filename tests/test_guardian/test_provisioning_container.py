"""Container-side coordinator: fetch capacity → ask → APPROVE → host execute.

Pure dependency injection — a fake remote records verb calls, a scripted ``ask``
returns the user's reply. Proves: no execute without APPROVE; DENY/timeout and a
capacity-read failure both short-circuit before any mutation; the proposal text
is shown to the user; APPROVE dispatches the correct host verb.
"""

from __future__ import annotations

from genesis.guardian.provisioning.container import (
    coordinate_grow_disk,
    coordinate_grow_memory,
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
