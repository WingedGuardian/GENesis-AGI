"""Direct unit coverage for the shared headless runner (session-manager
PR-3 extraction from the arbiter). The arbiter/worker suites exercise
these paths end-to-end through real fake-claude subprocesses; this file
pins the runner's own contract in isolation."""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
from pathlib import Path

import pytest

from genesis.session_awareness.headless import build_argv, run_headless_json

MODEL = "claude-haiku-4-5-20251001"


def _fake_claude(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake_claude.py"
    script.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    script.chmod(0o755)
    return str(script)


def test_build_argv_pinned_shape():
    argv = build_argv(MODEL, "claude", "/tmp/no_mcp.json")
    assert argv[argv.index("--model") + 1] == MODEL
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert "--strict-mcp-config" in argv
    assert "--dangerously-skip-permissions" in argv
    assert "--effort" not in argv
    assert "--output-format" in argv


@pytest.mark.asyncio
async def test_ok_returns_stdout_and_isolated_env(tmp_path, monkeypatch):
    """Zero exit → ok + stdout; child env carries GENESIS_CC_SESSION=1 and
    never GENESIS_SESSION_ORIGIN (WS-3 pop invariant)."""
    monkeypatch.setenv("GENESIS_SESSION_ORIGIN", "should-never-leak")
    fake = _fake_claude(
        tmp_path,
        """
        import json, os, sys
        sys.stdin.read()
        print(json.dumps({
            "result": "hi",
            "cc": os.environ.get("GENESIS_CC_SESSION"),
            "origin": os.environ.get("GENESIS_SESSION_ORIGIN"),
        }))
        """,
    )
    res = await run_headless_json(
        "prompt", model=MODEL, claude_path=fake, no_mcp_config="/dev/null", timeout_s=30
    )
    assert res["status"] == "ok"
    payload = json.loads(res["stdout"])
    assert payload["cc"] == "1"
    assert payload["origin"] is None


@pytest.mark.asyncio
async def test_nonzero_exit_reports_code(tmp_path):
    fake = _fake_claude(tmp_path, "import sys\nsys.stdin.read()\nsys.exit(7)\n")
    res = await run_headless_json(
        "p", model=MODEL, claude_path=fake, no_mcp_config="/dev/null", timeout_s=30
    )
    assert res == {"status": "failed", "reason": "exit_7"}


@pytest.mark.asyncio
async def test_spawn_failure_never_raises():
    res = await run_headless_json(
        "p",
        model=MODEL,
        claude_path="/nonexistent-binary",
        no_mcp_config="/dev/null",
        timeout_s=5,
    )
    assert res["status"] == "failed"
    assert "reason" in res


@pytest.mark.asyncio
async def test_timeout_group_kills_children(tmp_path):
    """A hung child that spawned its own grandchild: after the timeout BOTH
    must be gone (killpg with the pgid>1 guard, not a bare kill)."""
    marker = tmp_path / "child_pid"
    fake = _fake_claude(
        tmp_path,
        f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        open({str(marker)!r}, "w").write(str(child.pid))
        sys.stdin.read()
        time.sleep(600)
        """,
    )
    res = await run_headless_json(
        "p", model=MODEL, claude_path=fake, no_mcp_config="/dev/null", timeout_s=2
    )
    assert res == {"status": "timeout"}
    child_pid = int(marker.read_text())
    assert child_pid > 1  # explicit pid, never a mocked default
    await asyncio.sleep(0.2)  # let SIGKILL land
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)  # signal 0 = existence probe


@pytest.mark.asyncio
async def test_timeout_reaps_grandchild_after_leader_exits(tmp_path):
    """The leader EXITS while its grandchild holds the stdout pipe (so
    communicate() never EOFs and the timeout fires). asyncio reaps the dead
    leader, so an os.getpgid(proc.pid)-based kill raises ProcessLookupError
    and the proc.kill() fallback no-ops — leaking the grandchild. The kill
    must signal proc.pid AS the pgid (kernel reserves it while any member
    lives) so the grandchild is reaped."""
    marker = tmp_path / "child_pid"
    fake = _fake_claude(
        tmp_path,
        f"""
        import subprocess, sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        open({str(marker)!r}, "w").write(str(child.pid))
        sys.exit(0)  # leader exits; grandchild inherited stdout, pipe stays open
        """,
    )
    res = await run_headless_json(
        "p", model=MODEL, claude_path=fake, no_mcp_config="/dev/null", timeout_s=2
    )
    assert res == {"status": "timeout"}
    child_pid = int(marker.read_text())
    assert child_pid > 1
    await asyncio.sleep(0.3)  # let SIGKILL land
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.asyncio
async def test_spawned_in_new_session_not_preexec(monkeypatch):
    """The subprocess must get its own group via start_new_session=True
    (setsid in the C helper) and NOT preexec_fn (arbitrary post-fork Python
    can deadlock in the multi-threaded server)."""
    from unittest.mock import AsyncMock, MagicMock

    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"{}", b""))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    res = await run_headless_json(
        "p", model=MODEL, claude_path="claude", no_mcp_config="/dev/null", timeout_s=5
    )
    assert res["status"] == "ok"
    assert captured.get("start_new_session") is True
    assert "preexec_fn" not in captured


@pytest.mark.asyncio
async def test_timeout_reap_is_bounded(monkeypatch):
    """After the group kill, the reap of the dead leader must be BOUNDED —
    a paused pipe transport can stall an unbounded proc.wait() forever,
    turning the timeout recovery itself into a hang."""
    from unittest.mock import MagicMock

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", lambda *a: None)
    monkeypatch.setattr("genesis.util.proc_kill.DEFAULT_REAP_TIMEOUT_S", 0.2, raising=True)

    proc = MagicMock()
    proc.pid = 424242  # explicit — never a mock default (killpg(1) trap)

    async def _hang(*a, **k):
        await asyncio.sleep(600)

    proc.communicate = _hang
    proc.wait = _hang  # unbounded reap would hang here forever

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    res = await asyncio.wait_for(
        run_headless_json(
            "p",
            model=MODEL,
            claude_path="claude",
            no_mcp_config="/dev/null",
            timeout_s=0.1,
        ),
        timeout=10,  # the test bound: recovery must not hang
    )
    assert res == {"status": "timeout"}
