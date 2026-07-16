"""Container-side coordinators for grow-root + set-container-limits (PR-C).

Same DI pattern as test_provisioning_container: a fake remote records the verb
call, a scripted ask returns the reply. Proves no execute without APPROVE and
that APPROVE dispatches the correct host verb with the right args.
"""

from __future__ import annotations

from genesis.guardian.provisioning.container import (
    coordinate_grow_root,
    coordinate_set_container_limits,
)


class FakeRemote:
    def __init__(self, result=None):
        self._result = result or {"ok": True, "action": "executed"}
        self.grow_root_arg = None
        self.set_limits_args = None

    async def request_grow_root(self, new_gb):
        self.grow_root_arg = new_gb
        return self._result

    async def request_set_container_limits(self, mem_mib, cpu):
        self.set_limits_args = (mem_mib, cpu)
        return self._result


def _ask(reply):
    async def _inner(text):
        _inner.prompt = text
        return reply

    return _inner


async def test_grow_root_approve_dispatches():
    remote = FakeRemote()
    ask = _ask("APPROVE")
    res = await coordinate_grow_root(remote, ask, new_gb=40)
    assert res["ok"] is True
    assert remote.grow_root_arg == 40
    assert "40GB" in ask.prompt


async def test_grow_root_deny_no_execute():
    remote = FakeRemote()
    res = await coordinate_grow_root(remote, _ask("DENY"), new_gb=40)
    assert res["ok"] is False and res["stage"] == "denied"
    assert remote.grow_root_arg is None


async def test_set_limits_approve_dispatches():
    remote = FakeRemote()
    ask = _ask("APPROVE")
    res = await coordinate_set_container_limits(remote, ask, mem_mib=20480, cpu=4)
    assert res["ok"] is True
    assert remote.set_limits_args == (20480, 4)
    assert "memory" in ask.prompt and "cpu" in ask.prompt


async def test_set_limits_deny_no_execute():
    remote = FakeRemote()
    res = await coordinate_set_container_limits(remote, _ask("DENY"), mem_mib=20480, cpu=None)
    assert res["ok"] is False and res["stage"] == "denied"
    assert remote.set_limits_args is None
