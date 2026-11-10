"""Real file lifetime through the nine memory synchronous-work call sites."""

import asyncio
import inspect
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.db.request_database import RequestDatabase
from genesis.memory import intent, proactive, retrieval, store

pytestmark = pytest.mark.asyncio


def configure_call(site, db, operation, monkeypatch):
    """Replace the provider operation, retaining each real consumer boundary."""
    embeddings = SimpleNamespace(tracker=None, embed=AsyncMock(return_value=[1.0]))
    qdrant = MagicMock()
    qdrant.retrieve.return_value = [object()]
    memory = store.MemoryStore(embedding_provider=embeddings, qdrant_client=qdrant, db=db)
    retriever = retrieval.HybridRetriever(embedding_provider=embeddings, qdrant_client=qdrant, db=db)
    if site == "store-upsert":
        monkeypatch.setattr(store.memory_crud, "find_exact_duplicate", AsyncMock(return_value=None))
        monkeypatch.setattr(store, "record_last_run", AsyncMock())
        monkeypatch.setattr(store, "upsert_point", operation)
        return lambda: memory.store_reporting_creation("synthetic memory", "fixture", auto_link=False)
    if site.startswith("supersede-"):
        monkeypatch.setattr(store.memory_crud, "mark_superseded", AsyncMock(return_value=True))
        monkeypatch.setattr(store.memory_crud, "get_metadata", AsyncMock(return_value={"embedding_status": "embedded"}))
        if site == "supersede-retrieve":
            qdrant.retrieve.side_effect = operation
        else:
            monkeypatch.setattr(store, "update_payload", operation)
        return lambda: memory._mark_superseded("old", "new", "fixture-time", strict=True)
    if site.startswith("delete-"):
        from genesis.memory import delete_tombstones

        monkeypatch.setattr(delete_tombstones, "enqueue_tombstone", AsyncMock(return_value=True))
        if site == "delete-retrieve":
            qdrant.retrieve.side_effect = operation
        else:
            monkeypatch.setattr(store, "delete_point", operation)
        return lambda: memory._delete_locked("synthetic-id")
    if site == "recall-search":
        monkeypatch.setattr(retrieval.qdrant_ops, "search", operation)
        return lambda: retriever._gather_vector_candidates(
            vector=[1.0], collections=["episodic_memory"], candidate_limit=1,
            wing=None, room=None, life_domain=None, project_type=None,
            exclude_subsystems=None, include_only_subsystems=None, include_deprecated=False,
        )
    if site == "recall-counts":
        monkeypatch.setattr("genesis.env.memory_writebacks_off", lambda: False)
        monkeypatch.setattr(retrieval.qdrant_ops, "update_payload", operation)
        return lambda: retriever._record_retrievals(
            ["synthetic-id"], {"synthetic-id": {"payload": {}}}, {}, "fixture-time",
        )
    if site == "tag-scan":
        monkeypatch.setattr(intent, "_scan_tag_lists", operation)
        # Characterize the actual old boundary before its optional facade
        # parameter exists, rather than failing only on an unknown keyword.
        options = {"db": db} if "db" in inspect.signature(intent._rebuild_tag_index).parameters else {}
        return lambda: intent._rebuild_tag_index(qdrant, ["episodic_memory"], 1, **options)
    assert site == "procedure-matrix"
    monkeypatch.setattr(db, "execute_fetchall", AsyncMock(return_value=[]))
    monkeypatch.setattr(proactive, "_build_procedure_matrix", operation)
    return lambda: proactive._rebuild_procedure_cache(db)


@pytest.mark.parametrize("site", [
    "store-upsert", "supersede-retrieve", "supersede-update", "delete-retrieve",
    "delete-point", "recall-search", "recall-counts", "tag-scan", "procedure-matrix",
])
async def test_actual_consumer_retains_file_and_lease_after_cancellation(site, tmp_path, monkeypatch):
    path = tmp_path / "consumer.db"
    sqlite3.connect(path).close()
    db = RequestDatabase(path, max_units=1)
    resource = (tmp_path / "provider-resource").open("w")
    release = threading.Event()
    started, finished = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    errors, uses = [], []

    def operation(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(10), "fixture did not release physical work"
            resource.write(site + "\n")
            resource.flush()
            uses.append(site)
            return [object()] if site.endswith("retrieve") else []
        except BaseException as exc:
            errors.append(type(exc).__name__)
            raise
        finally:
            loop.call_soon_threadsafe(finished.set)

    invoke = configure_call(site, db, operation, monkeypatch)

    async def owner():
        try:
            async with db.unit_of_work():
                await invoke()
        finally:
            resource.close()

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        await asyncio.sleep(.05)
        retained = not task.done() and not resource.closed and db._slots._value == 0
        release.set()
        await asyncio.wait_for(finished.wait(), 10)
        await asyncio.gather(task, return_exceptions=True)
        assert (retained, uses, errors) == (True, [site], [])
        assert task.cancelled() and resource.closed
        assert db._slots._value == 1
        assert (tmp_path / "provider-resource").read_text() == site + "\n"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await db.close()
        resource.close()
