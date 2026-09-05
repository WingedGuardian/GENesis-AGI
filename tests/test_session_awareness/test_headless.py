"""Direct unit coverage for the shared headless runner (session-manager
PR-3 extraction from the arbiter). The arbiter/worker suites exercise
these paths end-to-end through real fake-claude subprocesses; this file
pins the runner's own contract in isolation."""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from genesis.session_awareness.headless import build_argv, run_headless_json
from tests.proc_asserts import process_terminated

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
    # Pure-completion judge over text that includes EXTERNAL content, running
    # outside project-guard scope: every built-in tool is denied wholesale.
    # Execution-proof measured 2026-09-04: a name list reopens with every new
    # built-in; the wildcard closed the set (touch-via-Bash probe, both
    # directions). Default-deny without the flag was REFUTED on a live
    # install — user settings made the tool run.
    assert argv[argv.index("--disallowedTools") + 1] == "*"
    assert "--effort" not in argv
    assert "--output-format" in argv
    assert argv.index("claude") == 0  # bare command stays bare (PATH lookup)


def test_build_argv_anchors_relative_paths():
    """The child runs from a per-call judge dir — relative path arguments must
    be resolved against the PARENT's cwd before the spawn, or every ambient
    call breaks under a relative override (incl. GENESIS_REPO_ROOT=.)."""
    argv = build_argv(MODEL, "./bin/claude", "config/no_mcp.json")
    import os

    assert os.path.isabs(argv[0])
    assert os.path.isabs(argv[argv.index("--mcp-config") + 1])


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
    assert process_terminated(child_pid)  # gone-or-zombie (tests/proc_asserts)


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
    assert process_terminated(child_pid)  # gone-or-zombie (tests/proc_asserts)


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


@pytest.mark.asyncio
async def test_parent_symlink_refused(tmp_path, monkeypatch):
    """A symlinked judge ROOT would relocate every judge cwd somewhere else's
    ancestors — the same shape as the symlink that walked past the sweep this
    design replaced, one level up. O_NOFOLLOW refuses it; the call degrades to
    the documented failed status rather than running from an unverified path."""
    import genesis.session_awareness.headless as headless_mod

    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "judge-root"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(headless_mod, "_AMBIENT_JUDGE_ROOT", link)

    spawned = []

    async def fake_exec(*a, **k):  # pragma: no cover - must never run
        spawned.append(a)
        raise AssertionError("spawned from a symlinked judge root")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    res = await run_headless_json(
        "p", model=MODEL, claude_path="claude",
        no_mcp_config="/dev/null", timeout_s=5,
    )
    assert res["status"] == "failed"
    assert not spawned
    assert list(real.iterdir()) == []  # nothing was created behind the link


@pytest.mark.asyncio
async def test_cancel_group_kills_and_reraises(monkeypatch):
    """Task cancellation mid-call must group-kill the detached claude tree
    before propagating — with its own session, no ambient signal reaches it."""
    from unittest.mock import AsyncMock, MagicMock

    killpg_calls = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    proc = MagicMock()
    proc.pid = 424243
    started = asyncio.Event()

    async def _hang(*a, **k):
        started.set()
        await asyncio.sleep(600)

    proc.communicate = _hang
    proc.wait = AsyncMock(return_value=-9)
    cwds: list[str] = []

    async def fake_exec(*args, **kwargs):
        cwds.append(kwargs["cwd"])
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    task = asyncio.get_running_loop().create_task(
        run_headless_json(
            "p", model=MODEL, claude_path="claude",
            no_mcp_config="/dev/null", timeout_s=600,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killpg_calls and killpg_calls[0][0] == 424243
    # _judge_cwd's docstring promises cleanup "including after a timeout or a
    # cancellation" — the timeout half is locked elsewhere; this is the other.
    assert cwds and not Path(cwds[0]).exists()


def test_production_dirs_disjoint_and_unnested(production_dirs):
    """The isolation invariant on the REAL constants — the spawn test above
    patches both dirs to tmp paths it chose disjoint, so only this test
    fails if someone nests the judge dir back under the shared one."""
    bg = production_dirs["background"]
    judge = production_dirs["judge"]
    assert judge != bg
    assert bg not in judge.parents
    assert judge not in bg.parents


@pytest.mark.asyncio
async def test_each_call_gets_a_fresh_private_cwd(tmp_path, monkeypatch):
    """The isolation mechanism, end to end: every call runs from a
    directory created for it alone, under the stable judge root, outside
    the shared background-sessions dir — and the directory is gone
    afterwards.

    Nothing can pre-plant context (CLAUDE.md, .mcp.json, a hook-bearing
    .claude/) in a directory whose name did not exist a moment ago, which
    is why this design needs no sweep. It also keeps the original fix: an
    out-of-repo cwd means CC's resume picker never lists these one-turn
    judgments (measured 2026-09-04)."""
    import genesis.cc.types as cc_types
    import genesis.session_awareness.headless as headless_mod

    root = tmp_path / "judge-root"
    monkeypatch.setattr(headless_mod, "_AMBIENT_JUDGE_ROOT", root)
    shared = tmp_path / "bg-sessions"
    monkeypatch.setattr(cc_types, "_BACKGROUND_SESSION_DIR", shared)
    seen: list[str] = []
    # Recorded, not asserted, INSIDE the mock: the runner's `except Exception`
    # would swallow an AssertionError raised here into a status dict with an
    # empty reason, destroying the diagnostic. Assert after the call.
    at_spawn: list[tuple[bool, list]] = []

    async def fake_exec(*argv, **kwargs):
        cwd = kwargs["cwd"]
        seen.append(cwd)
        at_spawn.append((Path(cwd).is_dir(), list(Path(cwd).iterdir())))

        class _P:
            returncode = 0
            pid = 12345

            async def communicate(self, _in):
                return b"{}", b""

        return _P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    for _ in range(2):
        res = await run_headless_json(
            "p", model=MODEL, claude_path="claude",
            no_mcp_config="/dev/null", timeout_s=5,
        )
        assert res["status"] == "ok"

    # LIVE and EMPTY at spawn time, every call.
    assert at_spawn and all(live and files == [] for live, files in at_spawn), at_spawn
    assert len(set(seen)) == 2, "each call must get its OWN directory"
    for cwd in seen:
        assert Path(cwd).parent == root
        assert not Path(cwd).exists(), "the call must remove its own dir"
        assert shared not in Path(cwd).parents


@pytest.mark.asyncio
async def test_judge_cwd_removed_after_timeout(tmp_path, monkeypatch):
    """Cleanup is in a finally — a timed-out call leaks no directory."""
    import genesis.session_awareness.headless as headless_mod

    root = tmp_path / "judge-root"
    monkeypatch.setattr(headless_mod, "_AMBIENT_JUDGE_ROOT", root)
    seen: list[str] = []

    async def fake_exec(*argv, **kwargs):
        seen.append(kwargs["cwd"])

        class _P:
            returncode = None
            pid = 12345

            async def communicate(self, _in):
                await asyncio.sleep(10)

        return _P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", lambda *a: None)
    res = await run_headless_json(
        "p", model=MODEL, claude_path="claude",
        no_mcp_config="/dev/null", timeout_s=0.05,
    )
    assert res["status"] == "timeout"
    assert seen and not Path(seen[0]).exists()
