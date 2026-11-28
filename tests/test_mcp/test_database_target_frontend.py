"""Actual ordinary CLI/pipe/TCP/SQLite target selection and committed visibility."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tests.test_mcp import test_ordinary_memory_frontend as memory_test
from tests.test_mcp.test_ordinary_memory_frontend import shared_install as shared_install
from tests.test_mcp.test_shared_forwarder import LEGACY, MODERN, _health_frame, receive, send

TARGET_KEY = "genesis/databaseTarget"
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("service", ["memory"])
@pytest.mark.parametrize("era", [MODERN, LEGACY], ids=["modern", "legacy"])
@pytest.mark.parametrize("mixed", [True, False, "pre-contract"], ids=["candidate", "ordinary", "pre-contract"])
@pytest.mark.parametrize("same_db", [True, False], ids=["same-db", "different-db"])
async def test_ordinary_target_selection_preserves_committed_write(
    tmp_path, monkeypatch, service, era, mixed, same_db, shared_install,
):
    backend_db = Path(shared_install.env["GENESIS_DB_PATH"])
    assert backend_db.is_relative_to(tmp_path)
    local_db = backend_db if same_db else tmp_path / "caller-shadow.db"
    if not same_db:
        with closing(sqlite3.connect(backend_db)) as source, closing(sqlite3.connect(local_db)) as destination:
            source.backup(destination)
    events = tmp_path / "selection.jsonl"
    trace = tmp_path / "stdin.trace"
    original_environment = memory_test.environment
    original_spawn = asyncio.create_subprocess_exec

    with events.open("xb", buffering=0) as observer:
        descriptor = observer.fileno()

        def environment(install, directory, injection, *, role, mixed):
            env = original_environment(install, directory, injection, role=role, mixed=mixed)
            (injection / "sitecustomize.py").write_text(
                "import os,traceback\ntry:\n"
                "    from tests.test_mcp.local_memory_target_restart_fixture import install\n    install()\n"
                "except BaseException:\n    traceback.print_exc()\n    os._exit(72)\n"
            )
            if role == "mixed":
                env["GENESIS_DB_PATH"] = str(local_db)
                env["LOCAL_MEMORY_TEST_MIXED"] = "1" if candidate else "0"
                env["LOCAL_MEMORY_TARGET_OBSERVER_FD"] = str(descriptor)
            elif role == "backend" and pre_contract:
                env["LOCAL_MEMORY_TARGET_BACKEND"] = "pre-contract"
            return env

        async def spawn(*argv, **kwargs):
            if len(argv) > 1 and argv[1] == str(ROOT / "scripts/genesis_mcp_server.py"):
                kwargs["pass_fds"] = (*kwargs.get("pass_fds", ()), descriptor)
                if os.environ.get("GENESIS_TEST_TARGET_STDIN_TRACE") == "1":
                    tracer = shutil.which("strace")
                    assert tracer is not None, "requested owned tracing requires strace; never silently skip it"
                    argv = (tracer, "--kill-on-exit", "-I", "1", "--seccomp-bpf", "-f", "-ttt", "-s", "1024",
                            "-e", "trace-fds=0," + str(descriptor),
                            "-e", "trace=read,readv,dup,dup2,dup3,fcntl,write,close",
                            "-o", str(trace), *argv)
            return await original_spawn(*argv, **kwargs)

        candidate, pre_contract = mixed is not False, mixed == "pre-contract"
        monkeypatch.setattr(memory_test, "environment", environment)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        marker = "owned-target-binding-write"
        async with memory_test.memory_pair(tmp_path, shared_install) as (child, local, remote):
            # Queue the first real client frame immediately, before launch preflight
            # finishes. Await the write before the read to avoid a concurrency oracle.
            if era == LEGACY:
                await send(child, _health_frame(era, "init", "initialize", protocolVersion=era,
                    capabilities={}, clientInfo={"name": "fixture", "version": "1"}))
                initialized = await receive(child, timeout=120)
                assert initialized["id"] == "init" and initialized["result"]["protocolVersion"] == era
                await send(child, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            written = await memory_test.invoke(child, "binding-write", "observation_write",
                {"content": marker, "source": "fixture", "type": "finding"}, era=era)
            observed = await memory_test.invoke(child, "binding-read", "observation_query", {"limit": 10}, era=era)
            counts = {}
            for name, path in [("caller", local_db), ("backend", backend_db)]:
                with closing(sqlite3.connect(path)) as database:
                    counts[name] = database.execute(
                        "SELECT COUNT(*) FROM observations WHERE content=?", (marker,),
                    ).fetchone()[0]
            receipts = memory_test.receipt(local)
            reads = [row for row in receipts if row["event"] == "http-request"
                     and isinstance(row.get("body"), dict) and row["body"].get("id") == "binding-read"]
            writes = [row for row in receipts if row["event"] == "http-request"
                      and isinstance(row.get("body"), dict) and row["body"].get("id") == "binding-write"]
        # Acceptance assertions follow normal EOF/shutdown even in the RED run.
        decisions = [json.loads(line) for line in events.read_text().splitlines()]
        actual = {"era": era, "candidate": candidate, "pre_contract": pre_contract, "same_db": same_db, "selection": decisions,
                  "caller_db": str(local_db), "backend_db": str(backend_db), "marker_rows": counts,
                  "write": written, "query": observed, "forwarded_reads": reads, "forwarded_writes": writes,
                  "visible": marker in json.dumps(observed)}
        if trace.exists():
            text = trace.read_text()
            boundary = re.search(r"write\(" + str(descriptor) + r",.*frontend-selection", text)
            assert boundary is not None, "an absent decision trace is not a clean input witness"
            actual["pre_selection_input_claims"] = re.findall(
                r"(?m)^.*(?:readv?\(0,|dup(?:2|3)?\(0,?|close\(0\)|fcntl\(0, F_DUPFD).*$",
                text[:boundary.start()],
            )
        (tmp_path / "target-binding-result.json").write_text(json.dumps(actual, indent=2) + "\n")
    expected_shared = candidate and same_db and not pre_contract
    assert counts == {"caller": 1, "backend": 1 if same_db else 0}
    assert len(decisions) == 1 and decisions[0]["ordinary_db"] == str(local_db)
    assert decisions[0]["shared_io_claims"] == 0
    assert decisions[0]["shared"] is expected_shared
    assert not writes and len(reads) == (1 if expected_shared else 0)
    if pre_contract:
        armed = [row for row in memory_test.receipt(remote) if row["event"] == "target-pre-contract"]
        assert len(armed) == 1 and "/mcp/v3" not in armed[0]["paths"] and "/mcp/v2" in armed[0]["paths"]
    if expected_shared:
        assert reads[0]["path"] == "/mcp/v3"
        assert reads[0]["body"]["params"]["_meta"][TARGET_KEY] == {
            "version": 1, "target": hashlib.sha256(
                b"genesis-memory-sqlite-target-v1\0" + os.fsencode(local_db.resolve(strict=True)),
            ).hexdigest(),
        }
    assert actual["visible"]
    assert not actual.get("pre_selection_input_claims", [])
