"""Hermetic tests for CCCliRouter — the claude-CLI completion provider.

The subprocess is mocked, so these exercise arg-shaping, result mapping, and
the failure/kwargs paths without spawning `claude`.
"""

import asyncio

import pytest

from genesis.experimentation.cc_router import CCCliRouter


class _FakeProc:
    def __init__(self, out=b"OUT", err=b"", rc=0):
        self._out = out
        self._err = err
        self.returncode = rc

    async def communicate(self, input=None):
        return self._out, self._err


def test_model_normalization_and_validation():
    assert CCCliRouter("cc-haiku")._model == "haiku"
    assert CCCliRouter("cc-sonnet")._model == "sonnet"
    assert CCCliRouter("HAIKU")._model == "haiku"
    with pytest.raises(ValueError):
        CCCliRouter("gpt-4")


async def test_route_call_success(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kw):
        captured["args"] = args
        return _FakeProc(out=b'```json\n{"observations":["x"]}\n```')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    r = CCCliRouter("cc-haiku")
    # temperature kwarg must be tolerated (judge/harness pass API-shaped args)
    res = await r.route_call(
        "gen",
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}],
        temperature=0.3,
    )
    assert res.success is True
    assert "observations" in res.content
    assert res.provider_used == "cc-cli"
    assert res.model_id == "haiku"
    # claude CLI invoked with the right shape
    args = captured["args"]
    assert args[0] == "claude" and "-p" in args
    assert "--model" in args and "haiku" in args
    assert "--system-prompt" in args  # system message → --system-prompt


async def test_route_call_failure_nonzero_exit(monkeypatch):
    async def fake_exec(*a, **k):
        return _FakeProc(out=b"", err=b"kaboom", rc=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    res = await CCCliRouter("haiku").route_call("gen", [{"role": "user", "content": "U"}])
    assert res.success is False
    assert res.content is None
    assert res.error


async def test_route_call_subprocess_raises(monkeypatch):
    async def boom(*a, **k):
        raise OSError("no claude binary")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    res = await CCCliRouter("haiku").route_call("gen", [{"role": "user", "content": "U"}])
    assert res.success is False
    assert "no claude binary" in (res.error or "")


async def test_route_call_timeout_kills_subprocess(monkeypatch):
    killed = {"v": False}

    class _HangProc:
        returncode = None

        async def communicate(self, input=None):
            await asyncio.sleep(10)  # exceed the tiny timeout

        def kill(self):
            killed["v"] = True

        async def wait(self):
            self.returncode = -9

    async def fake_exec(*a, **k):
        return _HangProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    res = await CCCliRouter("haiku", timeout_s=0.05).route_call(
        "gen", [{"role": "user", "content": "U"}],
    )
    assert res.success is False
    assert res.error == "timeout"
    assert killed["v"] is True  # the orphaned claude proc was reaped
