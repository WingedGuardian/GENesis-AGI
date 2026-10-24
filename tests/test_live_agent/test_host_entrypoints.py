"""Exercise the owner consumer and the service entrypoint without spawning CC."""

import asyncio
import contextlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_live_agent import test_host
from tests.test_live_agent.test_agent import owner_environment  # noqa: F401

running = test_host.running


@pytest.fixture
def attach_module(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("live_agent_attach_test", scripts / "live_agent_attach.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("line,expected", [
    ("/s", {"id": "r", "op": "status"}),
    ("/i", {"id": "r", "op": "interrupt"}),
    ("/y abc", {"id": "r", "op": "answer", "approval_id": "abc", "allow": True}),
    ("/n abc", {"id": "r", "op": "answer", "approval_id": "abc", "allow": False}),
    ("/steer next", {"id": "r", "op": "steer", "text": "next"}),
    ("hello", {"id": "r", "op": "turn", "text": "hello"}),
    ("/q", None),
])
def test_attach_command_contract(attach_module, line, expected):
    assert attach_module.command(line, "r") == expected


async def test_actual_terminal_consumer_sends_and_detaches(running, attach_module, monkeypatch, capsys):
    h, host = running
    lines = asyncio.Queue()
    lines.put_nowait("hello from owner\n")

    @contextlib.asynccontextmanager
    async def stdin():
        yield lines.get

    monkeypatch.setattr(attach_module, "_stdin_lines", stdin)
    task = asyncio.create_task(attach_module.attach(host.socket_path))
    while not h.client.sent:
        await asyncio.sleep(0)
    assert h.client.sent[0].endswith("hello from owner")
    lines.put_nowait("/q\n")
    await task
    assert h.agent.status()["connected"]
    assert "not replayed" in capsys.readouterr().out


@pytest.mark.parametrize("intentional", [False, True])
async def test_service_wires_speech_and_exit_code(tmp_path, monkeypatch, intentional):
    from genesis.live_agent import __main__ as entry

    seen = []

    def agent(**kwargs):
        seen.append(kwargs)
        return object()

    class Host:
        def __init__(self, agent, socket_path):
            assert socket_path == tmp_path / "control.sock"

        async def run(self, stop):
            if intentional:
                stop.set()

    monkeypatch.setattr(entry, "LiveAgent", agent)
    monkeypatch.setattr(entry, "LocalHost", Host)
    monkeypatch.setattr(entry, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(entry, "genesis_home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["host", "--socket", str(tmp_path / "control.sock")])
    with patch.dict("os.environ", {"GENESIS_SESSION_ID": "parent"}):
        assert await entry.main() == (0 if intentional else 1)
        import os
        assert "GENESIS_SESSION_ID" not in os.environ
    assert seen[0]["output_surface"] == "speech"
    assert seen[0]["state_path"] == tmp_path / "live_agent" / "state.json"
