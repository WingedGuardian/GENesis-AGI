"""Own shared-service resources outside the SDK's HTTP task lifecycle."""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from genesis.db.request_database import RequestDatabase
    from genesis.observability.provider_activity import ProviderActivityTracker


@dataclass(frozen=True)
class InitializedService:
    server: FastMCP
    database: RequestDatabase
    tracker: ProviderActivityTracker


async def close_owned_resources(database, closers, *, grace_s):
    # An unconfirmed drain propagates BEFORE touching dependent resources.
    await database.drain(grace_s=grace_s)
    failures = []
    for closer in reversed(closers):
        try:
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception as error:
            failures.append(error)
    if failures:
        raise ExceptionGroup("Shared resource cleanup failed", failures)


@asynccontextmanager
async def initialize_shared_service(settings, *, db_path, status_path, cache_dir,
                                    setup_routing, setup_health_actions):
    """Construct one resource set; borrowed backend clients retain ownership.

    The CLI supplies its existing standalone setup callbacks. No runtime is
    bootstrapped, credentials created, listener started or request replayed here.
    """
    from genesis.mcp.shared_http import require_qualified_stack

    require_qualified_stack()
    from genesis.db.request_database import RequestDatabase
    from genesis.db.schema import TABLES
    from genesis.mcp.request_service import initialize_request_service
    from genesis.observability.provider_activity import ProviderActivityTracker

    if not Path(db_path).is_file():
        raise ValueError("Shared MCP requires an existing database")
    from genesis.runtime._core import GenesisRuntime

    runtime = GenesisRuntime.peek()
    if runtime is not None and runtime._bootstrapped:
        raise ValueError("Shared MCP requires a standalone process")
    database = RequestDatabase(
        db_path, max_units=settings.http.db_units,
        checkout_timeout_s=settings.db_checkout_timeout_s,
        max_background_units=settings.db_background_units, max_background_jobs=settings.db_background_jobs,
    )
    closers = []
    try:
        async with database.unit_of_work():
            cursor = await database.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in await cursor.fetchall()}
            if not set(TABLES) <= tables:
                raise ValueError("Shared MCP requires the existing Genesis table schema")
            tracker = ProviderActivityTracker()
            tracker.set_db(database)
            await tracker.warm_from_db()

        setup_routing()
        name, dependencies = settings.http.service, {}
        if name == "health":
            from genesis.mcp.standalone_health import StandaloneHealthDataService

            dependencies["service"] = StandaloneHealthDataService(status_path=status_path, db=database)
            async with database.unit_of_work():
                await setup_health_actions(database)
        elif name == "memory":
            from qdrant_client import QdrantClient

            from genesis.env import embed_priority_tier, qdrant_url
            from genesis.memory.embeddings import EmbeddingProvider
            from genesis.memory.reranker import VoyageReranker

            def backend_created(backend):
                # build_chain invokes this synchronously while constructing;
                # registration never runs concurrently with resource closure.
                closers.append(backend.aclose)

            qdrant = QdrantClient(url=qdrant_url(), timeout=5)
            closers.append(qdrant.close)
            storage = EmbeddingProvider(
                backends=EmbeddingProvider.build_chain(on_backend_created=backend_created), cache_dir=cache_dir,
            )
            closers.append(storage.close_cache)
            recall = EmbeddingProvider(
                backends=EmbeddingProvider.build_chain(
                    priority_tier=embed_priority_tier(), on_backend_created=backend_created,
                ), cache_dir=cache_dir,
            )
            closers.append(recall.close_cache)
            reranker = VoyageReranker()
            closers.append(reranker.close)
            dependencies.update(qdrant_client=qdrant, storage_embedding_provider=storage,
                                recall_embedding_provider=recall, reranker=reranker,
                                read_pool=None, process_pending_bookmarks=False)
        elif name == "outreach":
            dependencies.update(pipeline=None, engagement=None, config=None)
        server = await initialize_request_service(
            name, db=database, activity_tracker=tracker, **dependencies,
        )
        yield InitializedService(server, database, tracker)
    finally:
        # Cache closers execute directly on the constructing loop thread.
        # Do not submit new registry work after it begins draining.
        await close_owned_resources(database, closers, grace_s=settings.shutdown_grace_s)
