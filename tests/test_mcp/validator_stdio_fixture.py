"""Owned stdio servers: real tools/SQL, fake memory embedding/vector boundaries."""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


async def prepare(root, *, profile="validator", recording=False):
    import aiosqlite

    from genesis.db.crud import pr_verifications
    from genesis.db.schema import create_all_tables, seed_data

    home = root / "home"
    (home / ".genesis/config").mkdir(parents=True)
    (home / ".genesis/config/codex_validator.local.yaml").write_text(
        "dry_run_only: " + ("false" if recording else "true") + "\n"
    )
    database = root / "owned.sqlite"
    async with aiosqlite.connect(database) as db:
        await create_all_tables(db)
        await seed_data(db)
        await db.commit()
        await pr_verifications.open_verification(
            db, repo="owner/repo", pr_number=7, pr_title="Synthetic native obligation",
            merged_at="2030-01-01", now="2030-01-01",
        )
    environment = {
        "HOME": str(home), "GENESIS_HOME": str(home / ".genesis"),
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.defpath)),
        "LANG": "C.UTF-8", "GENESIS_REPO_ROOT": str(ROOT),
    }
    return {
        "database": database, "environment": environment,
        "servers": {name: {
            "command": sys.executable,
            "args": [str(Path(__file__).resolve()), name, str(root), profile],
        } for name in ("health", "memory")},
    }


async def memory_lifespan(root):
    from contextlib import asynccontextmanager

    from genesis.db.connection import get_db
    from genesis.mcp import memory as memory_mcp
    from genesis.memory import entity_anchors, entity_resolution, graph
    from genesis.memory import store as store_mod
    from genesis.memory.store import MemoryStore
    from genesis.perception import confidence

    @asynccontextmanager
    async def lifespan(server):
        db = await get_db(root / "owned.sqlite", foreign_keys=False)
        vectors = {}
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[0.25] * 4), tracker=None)
        qdrant = SimpleNamespace(retrieve=lambda *, collection_name, ids, **kw: [
            SimpleNamespace(id=mid) for mid in ids if (collection_name, mid) in vectors
        ])

        def save_vectors():
            (root / "vectors.json").write_text(json.dumps([
                {"collection": collection, "id": mid, "payload": payload}
                for (collection, mid), payload in vectors.items()
            ]))

        def upsert(client, *, collection, point_id, vector, payload):
            assert client is qdrant
            vectors[collection, point_id] = dict(payload)
            save_vectors()

        def update(client, *, collection, point_id, payload):
            assert client is qdrant
            vectors[collection, point_id].update(payload)
            save_vectors()

        store_mod.upsert_point, store_mod.update_payload = upsert, update
        entity_resolution.load_aliases = lambda: {}
        confidence.load_config = lambda: confidence.ConfidenceGatesConfig()
        entity_anchors.record_anchors = AsyncMock()
        graph.invalidate_graph_cache = lambda: None
        memory_mcp._store = MemoryStore(embedding_provider=embedding, qdrant_client=qdrant, db=db)
        memory_mcp._db, memory_mcp._retriever = db, object()
        memory_mcp._process_pending_bookmarks = False
        try:
            yield
        finally:
            await db.close()

    return lifespan


def serve(server_name, root, profile):
    assert server_name in {"health", "memory"} and profile in {"validator", "external"}
    assert root.is_absolute() and (root / "owned.sqlite").is_file()
    if server_name == "health":
        path = ROOT / "scripts/genesis_mcp_server.py"
        spec = importlib.util.spec_from_file_location("owned_stdio_health", path)
        wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wrapper)
        wrapper._DEFAULT_DB, wrapper._DEFAULT_STATUS = root / "owned.sqlite", root / "absent-status.json"
        wrapper._bootstrap_health({"transport": "stdio"}, external_profile=profile)
    else:
        from genesis.mcp import memory as memory_mcp
        from genesis.mcp.external_profiles import ExternalProfileMiddleware

        memory_mcp.mcp.add_middleware(ExternalProfileMiddleware("memory", profile))
        memory_mcp.mcp._lifespan = asyncio.run(memory_lifespan(root))
        memory_mcp.mcp.run(transport="stdio")


if __name__ == "__main__":
    serve(sys.argv[1], Path(sys.argv[2]), sys.argv[3])
