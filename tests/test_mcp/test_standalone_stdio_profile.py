"""Real launcher/bootstrap/protocol against isolated durable state, without providers."""

import hashlib
import json
import os
from pathlib import Path

import aiosqlite
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from genesis.db.connection import init_db
from genesis.mcp.external_profiles import profile_tools
from scripts.codex_external_mcp import _SESSION_CONTEXT, runtime_root


@pytest.fixture
async def isolated_child(tmp_path):
    root = Path(__file__).resolve().parents[2]
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
        "GENESIS_HOME": str(state), "GENESIS_DB_PATH": str(database),
        "SECRETS_PATH": str(secrets), "PYTHONPATH": os.pathsep.join([str(tmp_path), str(root / "src")]),
        "GENESIS_ENABLE_OLLAMA": "0", "QDRANT_URL": "http://127.0.0.1:1",
        "GENESIS_RECALL_READ_POOL_OFF": "1", "FASTMCP_SHOW_SERVER_BANNER": "false",
        **dict.fromkeys(_SESSION_CONTEXT, "inherited-fixture-context"),
    }

    def transport(server, role):
        return StdioTransport(
            command="python3", args=[str(root / "scripts/codex_external_mcp.py"),
                                     "--server", server, "--profile", role],
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
        assert evidence["sources"][module] == [str(path), hashlib.sha256(path.read_bytes()).hexdigest()]
    assert not (set(evidence["env_keys"]) & set(_SESSION_CONTEXT))
    assert evidence["root"] == str(runtime_root())


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
            result = await client.call_tool("health_status")
            assert result.data["queues"]["synthetic_fixture"] == 37


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
