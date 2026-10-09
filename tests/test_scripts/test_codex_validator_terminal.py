"""Fail before native entry on profile drift; validate native preflight replies."""

import asyncio
import copy
import json
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis.eval.qualification.evidence import Incomplete
from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return private_module("validator_terminal_under_test", ROOT / "scripts/codex_validator_terminal.py")


@pytest.fixture
def profile(terminal, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / ".codex").mkdir(mode=0o700)
    (workspace / ".codex/client").mkdir(mode=0o700)
    path = workspace / ".codex/client/config.toml"
    key = str(path) + ":pre_tool_use:0:0"
    fingerprint = "sha256:" + "a" * 64
    raw = terminal.policy(workspace) + terminal.trust_suffix(key, fingerprint)
    terminal.write_new(path, raw.encode())
    expected = tomllib.loads(terminal.policy(workspace))
    command = expected["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    hook = {
        "key": key, "eventName": "preToolUse", "handlerType": "command", "command": command,
        "async": False, "matcher": ".*", "timeoutSec": 15, "sourcePath": str(path),
        "enabled": True, "currentHash": fingerprint, "trustStatus": "trusted",
    }
    effective = {**expected, "mcp_servers": {}}
    return workspace, path, hook, effective


@pytest.mark.parametrize("fault", ["mcp", "provider", "feature", "foreign_trust", "hash", "mode", "symlink", "hardlink"])
def test_profile_drift_refuses_before_any_native_process(terminal, profile, monkeypatch, fault):
    workspace, path, _, _ = profile
    raw = path.read_text()
    if fault == "mcp":
        raw += '\n[mcp_servers.forbidden]\ncommand = "never-start-this"\n'
    elif fault == "provider":
        raw = raw.replace('model_provider = "openai"', 'model_provider = "other"')
    elif fault == "feature":
        raw = raw.replace("plugins = false", "plugins = true")
    elif fault == "foreign_trust":
        raw += '\n[hooks.state.other]\ntrusted_hash = "sha256:foreign"\n'
    elif fault == "hash":
        raw = raw.replace("sha256:" + "a" * 64, "sha256:invalid")
    path.write_text(raw)
    if fault == "mode":
        path.chmod(0o644)
    elif fault == "symlink":
        other = path.with_name("other.toml")
        path.rename(other)
        path.symlink_to(other)
    elif fault == "hardlink":
        os.link(path, path.with_name("other.toml"))

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid profile entered a native process")

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises((ValueError, OSError, Incomplete)):
        asyncio.run(terminal.inspect("unused", workspace, {}, trusted=True))


class Writer:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data))

    async def drain(self):
        pass


def _native_reply_fixture(terminal, profile, monkeypatch, mutate=None):
    workspace, _, hook, effective = profile
    listing = {"data": [{"cwd": str(workspace), "errors": [], "hooks": [copy.deepcopy(hook)]}]}
    effective = copy.deepcopy(effective)
    if mutate:
        mutate(listing, effective)
    reader = asyncio.StreamReader()
    for number, result in [(1, {}), (2, listing), (3, {"config": effective})]:
        reader.feed_data(json.dumps({"id": number, "result": result}).encode() + b"\n")
    reader.feed_eof()
    process = SimpleNamespace(stdin=Writer(), stdout=reader)
    entered = []
    cleaned = []

    async def spawn(*args, **kwargs):
        entered.append((args, kwargs))
        return process

    async def finish(proc):
        assert proc is process
        cleaned.append(proc)

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(terminal, "finish_shutdown", finish)
    return entered, cleaned, process


def test_native_preflight_validates_policy_and_cleans_owned_process(terminal, profile, monkeypatch):
    async def check():
        entered, cleaned, proc = _native_reply_fixture(terminal, profile, monkeypatch)
        result = await terminal.inspect("native", profile[0], {"HOME": "private"}, trusted=True)
        assert result == profile[2]
        assert len(entered) == 1 and cleaned == [proc]
        assert [item["method"] for item in proc.stdin.messages] == ["initialize", "initialized", "hooks/list", "config/read"]
        assert entered[0][1]["start_new_session"] is True
    asyncio.run(check())


@pytest.mark.parametrize("field,value", [
    ("eventName", "postToolUse"), ("handlerType", "other"), ("command", "other"),
    ("matcher", "Bash"), ("async", True), ("enabled", False), ("timeoutSec", 1),
    ("sourcePath", "/other"), ("key", "other"), ("trustStatus", "untrusted"),
    ("currentHash", "sha256:invalid"),
])
def test_native_hook_drift_refuses(terminal, profile, monkeypatch, field, value):
    async def check():
        _, cleaned, proc = _native_reply_fixture(
            terminal, profile, monkeypatch,
            lambda listing, config: listing["data"][0]["hooks"][0].update({field: value}),
        )
        with pytest.raises(ValueError):
            await terminal.inspect("native", profile[0], {}, trusted=True)
        assert cleaned == [proc]
    asyncio.run(check())


@pytest.mark.parametrize("fault", ["mcp", "agents", "v2", "extra_hook", "errors", "multiple", "web", *range(10)])
def test_native_effective_policy_and_extra_hooks_refuse(terminal, profile, monkeypatch, fault):
    def mutate(listing, config):
        if type(fault) is int:
            config["features"][terminal.DISABLED[fault]] = True
        elif fault == "mcp":
            config["mcp_servers"] = {"forbidden": {"command": "never"}}
        elif fault == "agents":
            config["agents"]["enabled"] = True
        elif fault == "v2":
            config["features"]["multi_agent_v2"]["enabled"] = True
        elif fault == "extra_hook":
            config["hooks"]["SessionStart"] = [{"hooks": []}]
        elif fault == "errors":
            listing["data"][0]["errors"] = ["fixture error"]
        elif fault == "multiple":
            listing["data"][0]["hooks"].append(copy.deepcopy(profile[2]))
        elif fault == "web":
            config["web_search"] = "live"

    async def check():
        _, cleaned, proc = _native_reply_fixture(terminal, profile, monkeypatch, mutate)
        with pytest.raises(ValueError):
            await terminal.inspect("native", profile[0], {}, trusted=True)
        assert cleaned == [proc]
    asyncio.run(check())


def test_separate_run_lock_refuses_concurrent_run_and_releases(terminal, profile):
    workspace = profile[0]
    with terminal.run_lock(workspace), pytest.raises(BlockingIOError), terminal.run_lock(workspace):
        pytest.fail("concurrent run acquired the lock")
    with terminal.run_lock(workspace):
        assert (workspace / ".codex/run.lock").stat().st_mode & 0o777 == 0o600
