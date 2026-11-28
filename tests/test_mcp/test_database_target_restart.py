"""Actual ordinary CLI, owned backend process replacement and committed SQL."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_mcp import test_ordinary_memory_frontend as memory_test
from tests.test_mcp.test_ordinary_memory_frontend import shared_install as shared_install
from tests.test_mcp.test_shared_forwarder import (
    LEGACY,
    MODERN,
    _finish_health_backend,
    _health_frame,
    receive,
    send,
)


def sql_counts(path, marker):
    with closing(sqlite3.connect(path)) as database:
        return {"observations": database.execute("SELECT COUNT(*) FROM observations WHERE content=?", (marker,)).fetchone()[0],
                "bookmarks": database.execute("SELECT COUNT(*) FROM session_bookmarks").fetchone()[0]}


async def wait_claim(child, local):
    async with asyncio.timeout(120):
        while not Path(local["LOCAL_MEMORY_TEST_REPORT"]).exists() or not any(
                row["event"] == "target-input-claimed" for row in memory_test.receipt(local)):
            assert child.returncode is None
            await asyncio.sleep(.02)


async def initialize(child, era):
    if era == LEGACY:
        await send(child, _health_frame(era, "init", "initialize", protocolVersion=era,
                                      capabilities={}, clientInfo={"name": "fixture", "version": "1"}))
        response = await receive(child, timeout=120)
        assert response["id"] == "init"
        if "error" not in response:
            assert response["id"] == "init" and response["result"]["protocolVersion"] == era
            await send(child, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response


async def query(child, era, identifier):
    await send(child, _health_frame(era, identifier, "tools/call", name="observation_query", arguments={"limit": 10}))
    response = await receive(child, timeout=120)
    assert response["id"] == identifier
    return response


def refused(response):
    return "error" in response or response.get("result", {}).get("isError") is True


@pytest.mark.parametrize("service", ["memory"])
@pytest.mark.parametrize("era", [MODERN, LEGACY], ids=["modern", "legacy"])
@pytest.mark.parametrize("replacement", ["same", "different", "pre-contract"])
@pytest.mark.parametrize("phase", ["first-call", "idle"])
async def test_target_client_survives_or_refuses_replacement_without_replay(
    tmp_path, service, era, replacement, phase, shared_install,
):
    path = Path(shared_install.env["GENESIS_DB_PATH"])
    shadow = tmp_path / "replacement.db"
    if replacement == "different":
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(shadow)) as target:
            source.backup(target)
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import os,traceback\ntry:\n"
        "    from tests.test_mcp.local_memory_target_restart_fixture import install\n    install()\n"
        "except BaseException:\n    traceback.print_exc()\n    os._exit(72)\n")
    first = memory_test.environment(shared_install, tmp_path / "backend", injection, role="backend", mixed=False)
    first["LOCAL_MEMORY_TEST_SEED"] = "1"
    local = memory_test.environment(shared_install, tmp_path / "mixed", injection, role="mixed", mixed=True)
    changed = memory_test.environment(shared_install, tmp_path / "replacement", injection, role="backend", mixed=False)
    # Observation visibility and bookmark admission use persisted SQL. The
    # fixture's vector/knowledge population is not an idempotent restart hook.
    changed.update(LOCAL_MEMORY_TEST_SEED="0", GENESIS_DB_PATH=str(shadow if replacement == "different" else path))
    if replacement == "pre-contract":
        changed["LOCAL_MEMORY_TARGET_BACKEND"] = "pre-contract"
    next_install = SimpleNamespace(**{**vars(shared_install), "events": tmp_path / "replacement-events.jsonl"})
    marker = "owned-restart-committed-marker"
    child = owner = next_owner = None
    responses = []
    with (tmp_path / "backend.stderr").open("w") as first_errors, (tmp_path / "replacement.stderr").open("w") as next_errors, (
        tmp_path / "mixed/stderr.log").open("w") as errors:
        try:
            owner = await memory_test.backend(shared_install, first_errors, first)
            child = await asyncio.create_subprocess_exec(sys.executable,
                str(memory_test.ROOT / "scripts/genesis_mcp_server.py"), "--server", "memory",
                cwd="/", env=local, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=errors)
            await wait_claim(child, local)
            # Independent committed data, before any first-call race. The
            # different-target DB was copied before this update.
            with closing(sqlite3.connect(path)) as database:
                database.execute("UPDATE observations SET content=? WHERE id='cli-observation'", (marker,))
                database.commit()
            if phase == "idle":
                assert not (await initialize(child, era) or {}).get("error")
                before = await query(child, era, "before")
                assert not refused(before) and marker in json.dumps(before)
            pending = Path(local["HOME"]) / ".genesis/plan_bookmark_pending.json"
            (pending.parent / "sessions/12345678-caller-owned").mkdir(parents=True)
            pending.write_text(json.dumps({"title": "Replacement caller plan", "session_id_hint": "12345678"}))
            before_counts = sql_counts(path, marker)
            await _finish_health_backend(owner, shared_install)
            next_owner = await memory_test.backend(next_install, next_errors, changed)
            assert owner.returncode is not None and next_owner.pid != owner.pid
            if phase == "first-call":
                initial = await initialize(child, era)
                if initial is not None:
                    responses.append(initial)
                    if replacement == "same":
                        assert not refused(initial)
                    else:
                        assert refused(initial)
            if not (phase == "first-call" and era == LEGACY and replacement != "same"):
                after = await query(child, era, "after")
                responses.append(after)
                if replacement == "same":
                    if era == LEGACY and phase == "idle" and refused(after):
                        # A new explicit request may recover an expired legacy
                        # session; the failed tool request must never replay.
                        responses.append(await query(child, era, "explicit-next"))
                    assert not refused(responses[-1]) and marker in json.dumps(responses[-1])
                else:
                    assert refused(after)
                    again = await query(child, era, "explicit-next")
                    responses.append(again)
                    assert refused(again)
            if replacement == "same":
                # Pending-file admission detaches the index write. Reuse the
                # ordinary memory E2E's bounded committed-effect wait.
                async with asyncio.timeout(120):
                    while sql_counts(path, marker)["bookmarks"] == before_counts["bookmarks"]:
                        await asyncio.sleep(.02)
            local_rows, remote_rows = memory_test.receipt(local), memory_test.receipt(changed)
            posts = [row for row in local_rows if row["event"] == "http-request" and row["method"] == "POST"]
            assert all(row["path"] == "/mcp/v3" for row in posts)
            if phase == "idle" or replacement == "same":
                assert posts
            else:
                assert not posts, "a fresh incompatible generation must refuse at status, before any POST"
            for identifier in ["before", "after", "explicit-next"]:
                assert sum(row["body"].get("id") == identifier and row["body"].get("method") == "tools/call"
                           for row in posts) <= 1, "a tool request must never replay"
            assert not [row for row in local_rows if row["event"] == "body-guard"]
            after_bodies = [row for row in remote_rows if row["event"] == "body-guard"]
            if replacement == "same":
                assert [row["request_id"] for row in after_bodies] == [responses[-1]["id"]] and not pending.exists()
                assert sql_counts(path, marker) == {"observations": 1, "bookmarks": before_counts["bookmarks"] + 1}
                with closing(sqlite3.connect(path)) as database:
                    assert database.execute("SELECT topic,source,cc_session_id FROM session_bookmarks WHERE topic=?",
                        ("Replacement caller plan",)).fetchall() == [("Replacement caller plan", "plan", "12345678-caller-owned")]
            else:
                assert not after_bodies and pending.exists() and sql_counts(path, marker) == before_counts
                if replacement == "different":
                    assert sql_counts(shadow, marker) == {"observations": 0, "bookmarks": 0}
                else:
                    armed = [row for row in remote_rows if row["event"] == "target-pre-contract"]
                    assert len(armed) == 1 and "/mcp/v3" not in armed[0]["paths"] and "/mcp/v2" in armed[0]["paths"]
            if era == MODERN and phase == "idle":
                tokens = [row["generation"] for row in local_rows if row["event"] == "target-generation"]
                assert tokens and set(tokens) == {1}, "modern restart must exercise a retained logical generation"
                assert len([row for row in local_rows if row["event"] == "http-request" and row["path"] == "/status"]) == 2
            (tmp_path / "restart-result.json").write_text(json.dumps({
                "era": era, "replacement": replacement, "phase": phase, "responses": responses,
                "before_counts": before_counts, "after_counts": sql_counts(path, marker),
                "local_events": local_rows, "replacement_events": remote_rows,
                "old_pid": owner.pid, "replacement_pid": next_owner.pid,
            }, indent=2) + "\n")
            child.stdin.close()
            assert await asyncio.wait_for(child.wait(), 30) == 0
        finally:
            if child is not None and child.returncode is None:
                child.terminate()
                await child.wait()
            await _finish_health_backend(next_owner, next_install)
            await _finish_health_backend(owner, shared_install)
