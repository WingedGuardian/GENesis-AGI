"""An in-memory stand-in for the ``QdrantClient.retrieve`` reads in tests.

It answers ``retrieve`` the way a Qdrant server does: the points of one
collection that exist among the ids asked for, and an error for the whole call
when any id is not a valid point id (the server answers 400, which
qdrant-client's local mode does not reproduce). Tests name their memories with
:func:`point_id` to get valid ids from readable names. The real client is
covered in the ``hydrate_neighbours`` tests of
``tests/test_memory/test_graph_expansion.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace


def point_id(name: str) -> str:
    """A stable UUID for a readable test name such as ``"nbr-1"``."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"genesis-test:{name}"))


class FakeVectorStore:
    def __init__(self) -> None:
        self.points: dict[str, dict[str, dict]] = {"episodic_memory": {}, "knowledge_base": {}}

    def put(self, collection: str, memory_id: str, **payload) -> None:
        self.points[collection][memory_id] = payload

    def retrieve(self, collection_name, ids, with_payload=True, with_vectors=False):
        for i in ids:
            uuid.UUID(i)  # raises ValueError, as the server rejects the whole call
        held = self.points.get(collection_name, {})
        return [SimpleNamespace(id=i, payload=dict(held[i])) for i in ids if i in held]
