"""Real launcher/bootstrap/protocol against isolated durable state, without providers."""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import aiosqlite
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from genesis.db.connection import init_db
from genesis.mcp.external_profiles import profile_tools
from scripts.codex_external_mcp import _SESSION_CONTEXT, runtime_root


@pytest.fixture(params=["wrapper", "native"])
async def isolated_child(tmp_path, request):
    source_root = Path(__file__).resolve().parents[2]
    # Model an installed checkout without assuming the runner has .venv there.
    # The real portable launcher still selects .venv/bin/python; use the actual
    # dependency environment, including setup-python's global prefix on CI.
    root = tmp_path / "checkout"
    root.mkdir()
    for relative in (
        "scripts/codex_external_mcp.py", "scripts/genesis_mcp_server.py",
        ".claude/mcp/run-mcp-server",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, target)
    (root / "src").symlink_to(source_root / "src", target_is_directory=True)
    (root / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    state = tmp_path / ".genesis"
    state.mkdir()
    (state / "cc_context_enabled").touch()
    (state / "status.json").write_text(json.dumps({"queue_depths": {"synthetic_fixture": 37}}))
    database = tmp_path / "fixture.db"
    db = await init_db(database)
    await db.close()
    secrets = tmp_path / "synthetic.env"
    secrets.write_text("GENESIS_SESSION_ID=must-not-restore\nGENESIS_REPO_ROOT=/not-the-runtime\n")
    receipt = tmp_path / "source-receipt.json"
    # Observe imported source and final child context without changing tool bodies.
    (tmp_path / "sitecustomize.py").write_text(
        "import atexit,hashlib,json,os,sys\n"
        "script=sys.argv[0]\n"
        "def receipt():\n"
        " if not script.endswith('genesis_mcp_server.py'): return\n"
        " modules=['genesis.mcp.external_profiles','genesis.routing.standalone']\n"
        " sources={m:sys.modules[m].__file__ for m in modules if m in sys.modules}\n"
        " sources['__main__']=script\n"
        " values={'sources':{m:[p,hashlib.sha256(open(p,'rb').read()).hexdigest()] for m,p in sources.items()},"
        "'env_keys':list(os.environ),'root':os.environ.get('GENESIS_REPO_ROOT')}\n"
        f" open({str(receipt)!r},'w').write(json.dumps(values))\n"
        "atexit.register(receipt)\n"
    )
    # A supplied transport env inherits only MCP's small platform-default env,
    # never the parent's provider secrets. No live DB, HTTP service or dispatcher.
    environment = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "GENESIS_HOME": str(state), "GENESIS_DB_PATH": str(database), "GENESIS_REPO_ROOT": str(root),
        "SECRETS_PATH": str(secrets), "PYTHONPATH": os.pathsep.join([str(tmp_path), str(root / "src")]),
        "GENESIS_ENABLE_OLLAMA": "0", "QDRANT_URL": "http://127.0.0.1:1",
        "GENESIS_RECALL_READ_POOL_OFF": "1", "FASTMCP_SHOW_SERVER_BANNER": "false",
        **dict.fromkeys(_SESSION_CONTEXT, "inherited-fixture-context"),
    }

    def transport(server, role):
        command = sys.executable if request.param == "native" else "python3"
        args = [str(root / "scripts/genesis_mcp_server.py"), "--external-client", role]
        if request.param == "wrapper":
            args = [str(root / "scripts/codex_external_mcp.py"), "--profile", role]
        return StdioTransport(
            command=command, args=[*args, "--server", server],
            env=environment, cwd=str(root), keep_alive=False,
        )

    yield database, transport
    evidence = json.loads(receipt.read_text())
    for module, relative in {
        "__main__": "scripts/genesis_mcp_server.py",
        "genesis.mcp.external_profiles": "src/genesis/mcp/external_profiles.py",
        "genesis.routing.standalone": "src/genesis/routing/standalone.py",
    }.items():
        path = root / relative
        assert evidence["sources"][module] == [str(path), hashlib.sha256((source_root / relative).read_bytes()).hexdigest()]
    assert not (set(evidence["env_keys"]) & set(_SESSION_CONTEXT))
    assert evidence["root"] == str(runtime_root(root))


@pytest.mark.parametrize("role", ["external", "validator", "interactive"])
@pytest.mark.parametrize("server", ["health", "memory"])
async def test_real_stdio_catalog_and_direct_refusal(isolated_child, role, server):
    _, transport = isolated_child
    async with Client(transport(server, role), timeout=45) as client:
        assert {tool.name for tool in await client.list_tools()} == profile_tools(server, role)
        forbidden = "document_delete" if server == "memory" else "campaign_trigger"
        with pytest.raises(ToolError, match="unavailable"):
            await client.call_tool(forbidden, {} if server == "health" else {"doc_id": "synthetic"})
        if server == "health":
            with pytest.raises(ToolError, match="unavailable"):
                await client.call_tool("session_config", {"session_id": "synthetic", "effort": "low"})
            result = await client.call_tool("health_status")
            assert result.data["queues"]["synthetic_fixture"] == 37


async def test_real_interactive_follow_up_has_external_provenance(isolated_child):
    database, transport = isolated_child
    arguments = {
        "content": "Synthetic external-client follow-up", "reason": "owner directed",
        "strategy": "ego_judgment", "work_state": "ready", "domain": "internal",
    }
    known = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    unknown = "12345678-1234-4321-8123-123456789abc"
    async with aiosqlite.connect(database) as db:
        await db.execute("INSERT INTO session_heartbeats(cc_session_id,updated_at) VALUES (?,'t0')", (known,))
        await db.commit()
    async with Client(transport("health", "interactive"), timeout=45) as client:
        result = await client.call_tool("follow_up_create", arguments)
        follow_up_id = result.data["id"]
        dropped = await client.call_tool("follow_up_create", {
            **arguments, "content": "Synthetic unknown follow-up provenance", "source_session": unknown,
        })
        assert dropped.data["source_session"] is None
        assert "existing Genesis session" in dropped.data["message"]
        retained = await client.call_tool("follow_up_create", {
            **arguments, "content": "Synthetic known follow-up provenance", "source_session": known[:8],
        })
        assert retained.data["source_session"] == known
    async with Client(transport("health", "external"), timeout=45) as narrow:
        with pytest.raises(ToolError, match="unavailable"):
            await narrow.call_tool("follow_up_create", arguments)
    async with aiosqlite.connect(database) as db:
        row = await (await db.execute(
            "SELECT source, kind, source_session, content FROM follow_ups WHERE id=?", (follow_up_id,),
        )).fetchone()
        assert row == ("external_interactive", "follow_up", None, arguments["content"])
        assert await (await db.execute("SELECT COUNT(*) FROM follow_ups")).fetchone() == (3,)
        for result_id, expected in ((dropped.data["id"], None), (retained.data["id"], known)):
            assert await (await db.execute("SELECT source_session FROM follow_ups WHERE id=?", (result_id,))).fetchone() == (expected,)


async def test_real_external_charter_writes_require_an_existing_target(isolated_child):
    database, transport = isolated_child
    existing = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    unknown = "12345678-1234-4321-8123-123456789abc"
    async with aiosqlite.connect(database) as db:
        await db.execute("INSERT INTO session_heartbeats(cc_session_id,updated_at) VALUES (?,'t0')", (existing,))
        await db.commit()
    async with Client(transport("health", "interactive"), timeout=45) as client:
        for name, payload in (
            ("session_charter_update", {"mission": "Synthetic directed mission"}),
            ("session_ledger_add", {"text": "Synthetic directed commitment"}),
        ):
            rejected = await client.call_tool(name, {"session_id": unknown, **payload})
            assert "existing Genesis session" in rejected.data["error"]
            accepted = await client.call_tool(name, {"session_id": existing[:8], **payload})
            assert "error" not in accepted.data
    async with aiosqlite.connect(database) as db:
        assert await (await db.execute("SELECT session_id,mission FROM session_charters")).fetchall() == [
            (existing, "Synthetic directed mission"),
        ]
        assert await (await db.execute("SELECT session_id,text FROM session_ledger")).fetchall() == [
            (existing, "Synthetic directed commitment"),
        ]


async def test_real_memory_save_duplicate_restart_and_keyword_recall(isolated_child):
    database, transport = isolated_child
    content = "Synthetic quartzviolet memory survives a standalone process restart."
    arguments = {"content": content, "source": "interactive-fixture", "confidence": 0.9}
    async with Client(transport("memory", "interactive"), timeout=45) as client:
        memory_id = (await client.call_tool("memory_store", arguments)).data
        assert isinstance(memory_id, str) and memory_id
        assert (await client.call_tool("memory_store", arguments)).data == memory_id
    # Independent SQL reader proves durable content and honest degraded indexing.
    async with aiosqlite.connect(database) as db:
        row = await (await db.execute("SELECT content FROM memory_fts WHERE memory_id=?", (memory_id,))).fetchone()
        assert row == (content,)
        pending = await (await db.execute("SELECT COUNT(*) FROM pending_embeddings WHERE memory_id=?", (memory_id,))).fetchone()
        assert pending == (1,)
    async with Client(transport("memory", "interactive"), timeout=45) as restarted:
        recalled = await restarted.call_tool("memory_recall", {
            "query": "quartzviolet", "include_graph": False, "expand_query_terms": False,
            "rerank": False, "corrective": False,
        })
        assert any(row.get("memory_id") == memory_id and row.get("content") == content
                   for row in recalled.structured_content["result"])


async def test_real_memory_session_references_require_recorded_targets(isolated_child):
    database, transport = isolated_child
    known = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    unknown = "12345678-1234-4321-8123-123456789abc"
    async with aiosqlite.connect(database) as db:
        await db.execute("INSERT INTO session_heartbeats(cc_session_id,updated_at) VALUES (?,'t0')", (known,))
        await db.commit()
    async with Client(transport("memory", "interactive"), timeout=45) as client:
        for name, arguments in (
            ("bookmark_shelve", {"session_id": unknown, "context": "Synthetic rejected bookmark"}),
            ("knowledge_ingest", {"content": "Synthetic rejected knowledge", "project": "fixture",
                                  "domain": "fixture", "provenance": {"session_id": unknown}}),
            ("reference_store", {"kind": "fact", "identifier": "rejected-synthetic-reference",
                                 "value": "synthetic", "description": "Synthetic rejected reference",
                                 "source": {"session_id": unknown}}),
        ):
            with pytest.raises(ToolError, match="existing Genesis session"):
                await client.call_tool(name, arguments)
        await client.call_tool("bookmark_shelve", {"session_id": known[:8], "context": "Synthetic recorded bookmark"})
        knowledge_id = (await client.call_tool("knowledge_ingest", {
            "content": "Synthetic recorded knowledge", "project": "fixture", "domain": "fixture",
            "provenance": {"session_id": known[:8]},
        })).data
        reference_id = (await client.call_tool("reference_store", {
            "kind": "fact", "identifier": "recorded-synthetic-reference", "value": "synthetic",
            "description": "Synthetic recorded reference", "source": {"session_id": known[:8]},
        })).data
    async with aiosqlite.connect(database) as db:
        assert await (await db.execute("SELECT cc_session_id FROM session_bookmarks")).fetchall() == [(known,)]
        rows = await (await db.execute(
            "SELECT k.id,p.source_session_id FROM knowledge_units k"
            " JOIN pending_embeddings p ON p.memory_id=k.qdrant_id ORDER BY k.id",
        )).fetchall()
        assert rows == sorted([(knowledge_id, known), (reference_id, known)])
        assert (await (await db.execute("SELECT COUNT(*) FROM knowledge_units")).fetchone())[0] == 2
