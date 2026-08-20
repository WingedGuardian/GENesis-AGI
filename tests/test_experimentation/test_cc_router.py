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


async def test_route_call_timeout_group_kills_subprocess(monkeypatch):
    """Timeout must SIGKILL the whole process GROUP (claude spawns MCP/helper
    children — a bare proc.kill() orphans them), signalling proc.pid AS the
    pgid, with the direct kill only as the guard fallback."""
    killpg_calls = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    direct_killed = {"v": False}

    class _HangProc:
        returncode = None
        pid = 77777  # explicit — never a mock default (killpg(1) trap)

        async def communicate(self, input=None):
            await asyncio.sleep(10)  # exceed the tiny timeout

        def kill(self):
            direct_killed["v"] = True

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
    assert killpg_calls == [(77777, __import__("signal").SIGKILL)]
    assert direct_killed["v"] is False  # group kill, not the bare fallback


async def test_route_call_timeout_pgid_guard(monkeypatch):
    """A pid<=1 (mock default / pathological) must never reach killpg —
    fall back to the direct kill."""
    killpg_calls = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    direct_killed = {"v": False}

    class _HangProc:
        returncode = None
        pid = 1

        async def communicate(self, input=None):
            await asyncio.sleep(10)

        def kill(self):
            direct_killed["v"] = True

        async def wait(self):
            self.returncode = -9

    async def fake_exec(*a, **k):
        return _HangProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    res = await CCCliRouter("haiku", timeout_s=0.05).route_call(
        "gen", [{"role": "user", "content": "U"}],
    )
    assert res.error == "timeout"
    assert killpg_calls == []
    assert direct_killed["v"] is True


async def test_spawned_in_new_session_not_preexec(monkeypatch):
    """claude must be spawned with start_new_session=True (own group so the
    timeout tree-kill can killpg it) and never via preexec_fn."""
    captured = {}

    async def fake_exec(*args, **kw):
        captured.update(kw)
        return _FakeProc(out=b"fine")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    res = await CCCliRouter("haiku").route_call("gen", [{"role": "user", "content": "U"}])
    assert res.success is True
    assert captured.get("start_new_session") is True
    assert "preexec_fn" not in captured


async def test_route_call_cancel_group_kills_and_reraises(monkeypatch):
    """Cancellation mid-call must group-kill the detached claude tree before
    propagating (same class as the timeout path)."""
    killpg_calls = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    started = asyncio.Event()

    class _HangProc:
        returncode = None
        pid = 77778

        async def communicate(self, input=None):
            started.set()
            await asyncio.sleep(600)

        def kill(self):
            pass

        async def wait(self):
            self.returncode = -9

    async def fake_exec(*a, **k):
        return _HangProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    router = CCCliRouter("haiku", timeout_s=600)
    task = asyncio.get_running_loop().create_task(
        router.route_call("gen", [{"role": "user", "content": "U"}])
    )
    await started.wait()
    task.cancel()
    import pytest as _pytest

    with _pytest.raises(asyncio.CancelledError):
        await task
    assert killpg_calls and killpg_calls[0][0] == 77778
