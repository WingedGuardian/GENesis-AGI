"""Synthetic provider boundary; real memory storage, retrieval and SQL bodies."""

from datetime import UTC, datetime


def providers(record):
    from genesis.bookmark.manager import BookmarkManager
    from genesis.memory.embeddings import EmbeddingProvider

    class OwnedEmbedding:
        name = "owned-memory-fixture"
        vector_space = "owned-memory-fixture-1024"

        async def is_available(self):
            return True

        async def embed(self, text):
            record("synthetic-embedding", characters=len(text))
            return [1.0] + [0.0] * 1023

    EmbeddingProvider.build_chain = staticmethod(lambda **kwargs: [OwnedEmbedding()])

    async def extraction(self, *args, **kwargs):
        # Optional model extraction is an explicit fixture boundary. The
        # manager's real store, bookmark index and pending consumer still run.
        record("synthetic-extraction")

    BookmarkManager._trigger_extraction = extraction


async def seed(memory, record):
    from genesis.db.crud import knowledge, procedural
    from genesis.qdrant.collections import ensure_collections

    ensure_collections(memory._qdrant)
    async with memory._db.unit_of_work():
        episodic = await memory._store.store(
            "Owned fixture cobalt memory", "fixture", confidence=.95,
            memory_class="fact", auto_link=False, origin_class="first_party",
        )
        vector = await memory._store.store(
            "Owned fixture cobalt knowledge", "fixture", memory_type="knowledge",
            confidence=.95, auto_link=False, origin_class="first_party",
            project_type="fixture",
        )
        unit = await knowledge.insert(
            memory._db, project_type="fixture", domain="fixture",
            source_doc="fixture", concept="cobalt", body="Owned fixture cobalt knowledge",
            qdrant_id=vector, origin_class="first_party",
        )
        procedure = await procedural.create(
            memory._db, id="owned-cobalt-procedure", task_type="cobalt",
            principle="Use the owned cobalt fixture", steps=["Read cobalt"],
            tools_used=["observation_query"], context_tags=["cobalt"],
            created_at=datetime.now(UTC).isoformat(), draft=0, confidence=.9,
        )
    record("seed", episodic=episodic, knowledge=unit, procedure=procedure)
