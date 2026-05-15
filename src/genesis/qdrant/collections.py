"""Qdrant collection management and vector operations for Genesis v3.

Collections:
  - episodic_memory: Active memory (episodic + semantic + references, filtered by memory_type)
  - knowledge_base: External domain knowledge (ingested docs, web content)

Vector dimensions: 1024 (qwen3-embedding:0.6b-fp16 via Ollama)
Distance metric: Cosine
"""

from __future__ import annotations

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

VECTOR_DIM = 1024
COLLECTIONS = ["episodic_memory", "knowledge_base"]
_PROTECTED_COLLECTIONS = frozenset(COLLECTIONS)


def get_client(url: str | None = None) -> QdrantClient:
    """Return a Qdrant client instance."""
    if url is None:
        from genesis.env import qdrant_url

        url = qdrant_url()
    return QdrantClient(url=url)


def ensure_collections(client: QdrantClient) -> None:
    """Create all Genesis collections if they don't exist."""
    existing = {c.name for c in client.get_collections().collections}
    for name in COLLECTIONS:
        if name not in existing:
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            )


def upsert_point(
    client: QdrantClient,
    *,
    collection: str,
    point_id: str,
    vector: list[float],
    payload: dict,
) -> None:
    """Insert or update a single point."""
    client.upsert(
        collection_name=collection,
        points=[PointStruct(id=point_id, vector=vector, payload=payload)],
    )


def update_payload(
    client: QdrantClient,
    *,
    collection: str,
    point_id: str,
    payload: dict,
) -> None:
    """Update payload fields on an existing point without re-uploading vectors."""
    client.set_payload(
        collection_name=collection,
        payload=payload,
        points=[point_id],
    )


def search(
    client: QdrantClient,
    *,
    collection: str,
    query_vector: list[float],
    limit: int = 10,
    source_type: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    exclude_subsystems: list[str] | None = None,
    include_only_subsystems: list[str] | None = None,
) -> list[dict]:
    """Search by vector similarity with optional payload filters.

    ``exclude_subsystems`` and ``include_only_subsystems`` operate on
    the ``source_subsystem`` payload key. Excludes use ``must_not`` —
    points missing the key (legacy data without the tag) are preserved.
    Includes use ``must`` — only points whose key matches are returned.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

    conditions: list = []
    must_not_conditions: list = []

    # Always exclude deprecated memories (dream cycle soft-delete).
    # Points without the field (legacy data) are preserved — Qdrant's
    # must_not only excludes points where the field exists AND matches.
    must_not_conditions.append(
        FieldCondition(key="deprecated", match=MatchValue(value=True))
    )

    if source_type:
        conditions.append(
            FieldCondition(key="source_type", match=MatchValue(value=source_type))
        )
    if wing:
        conditions.append(
            FieldCondition(key="wing", match=MatchValue(value=wing))
        )
    if room:
        conditions.append(
            FieldCondition(key="room", match=MatchValue(value=room))
        )
    if include_only_subsystems:
        conditions.append(
            FieldCondition(
                key="source_subsystem",
                match=MatchAny(any=list(include_only_subsystems)),
            )
        )
    if exclude_subsystems:
        must_not_conditions.append(
            FieldCondition(
                key="source_subsystem",
                match=MatchAny(any=list(exclude_subsystems)),
            )
        )
    query_filter = Filter(
        must=conditions or None,
        must_not=must_not_conditions or None,
    )
    results = client.query_points(
        collection_name=collection,
        query=query_vector,
        limit=limit,
        query_filter=query_filter,
    )
    return [
        {"id": str(hit.id), "score": hit.score, "payload": hit.payload}
        for hit in results.points
    ]


def delete_point(
    client: QdrantClient, *, collection: str, point_id: str
) -> None:
    """Delete a point by ID."""
    from qdrant_client.models import PointIdsList

    client.delete(
        collection_name=collection,
        points_selector=PointIdsList(points=[point_id]),
    )


def get_point(
    client: QdrantClient,
    *,
    collection: str,
    point_id: str,
) -> dict | None:
    """Retrieve a single point by ID. Returns payload dict or None."""
    try:
        points = client.retrieve(
            collection_name=collection,
            ids=[point_id],
            with_payload=True,
            with_vectors=False,
        )
        if points:
            return {"id": str(points[0].id), "payload": points[0].payload}
        return None
    except Exception:
        return None


def scroll_points(
    client: QdrantClient,
    *,
    collection: str,
    limit: int = 1000,
    offset: str | None = None,
    payload_filter: dict | None = None,
) -> tuple[list[dict], str | None]:
    """Paginated listing of points. Returns (points, next_page_offset).

    next_page_offset is None when no more pages remain.
    """
    scroll_filter = None
    if payload_filter:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        conditions = [
            FieldCondition(key=k, match=MatchValue(value=v))
            for k, v in payload_filter.items()
        ]
        scroll_filter = Filter(must=conditions)

    results, next_offset = client.scroll(
        collection_name=collection,
        limit=limit,
        offset=offset,
        scroll_filter=scroll_filter,
        with_payload=True,
        with_vectors=False,
    )
    points = [
        {"id": str(point.id), "payload": point.payload}
        for point in results
    ]
    return points, str(next_offset) if next_offset else None


def delete_collection(
    client: QdrantClient, collection: str, *, force: bool = False
) -> bool:
    """Delete a Qdrant collection.

    Refuses to delete production collections (episodic_memory, knowledge_base)
    unless force=True. This guard exists because automated tests previously
    deleted production data silently for weeks.
    """
    if collection in _PROTECTED_COLLECTIONS and not force:
        raise ValueError(
            f"Refusing to delete protected collection '{collection}'. "
            f"Pass force=True to override."
        )
    return client.delete_collection(collection)


def get_collection_info(client: QdrantClient, collection: str) -> dict:
    """Get collection stats."""
    info = client.get_collection(collection_name=collection)
    return {
        "name": collection,
        "points_count": info.points_count,
        "status": info.status.value,
    }
