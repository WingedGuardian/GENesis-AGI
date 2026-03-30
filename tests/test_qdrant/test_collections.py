"""Tests for Qdrant collection management.

These tests use the real Qdrant instance on localhost:6333.
They create test collections with a unique suffix to avoid polluting production data.
"""

import random
import uuid

import pytest
from qdrant_client.models import Distance, VectorParams

from genesis.qdrant.collections import (
    VECTOR_DIM,
    delete_point,
    get_client,
    get_collection_info,
    get_point,
    scroll_points,
    search,
    update_payload,
    upsert_point,
)

TEST_COLLECTION = "test_genesis_" + uuid.uuid4().hex[:8]


def _uuid():
    return str(uuid.uuid4())


def _random_vector():
    return [random.random() for _ in range(VECTOR_DIM)]


@pytest.fixture(scope="module")
def qdrant():
    client = get_client()
    client.create_collection(
        collection_name=TEST_COLLECTION,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
    )
    yield client
    client.delete_collection(TEST_COLLECTION)


def test_collection_created(qdrant):
    info = get_collection_info(qdrant, TEST_COLLECTION)
    assert info["name"] == TEST_COLLECTION
    assert info["status"] == "green"


def test_upsert_and_search(qdrant):
    pid = _uuid()
    vec = _random_vector()
    upsert_point(
        qdrant,
        collection=TEST_COLLECTION,
        point_id=pid,
        vector=vec,
        payload={"content": "test memory", "source_type": "memory", "memory_type": "episodic"},
    )
    results = search(qdrant, collection=TEST_COLLECTION, query_vector=vec, limit=1)
    assert len(results) >= 1
    assert results[0]["payload"]["content"] == "test memory"


def test_search_with_filter(qdrant):
    vec1 = _random_vector()
    vec2 = _random_vector()
    upsert_point(
        qdrant, collection=TEST_COLLECTION,
        point_id=_uuid(), vector=vec1,
        payload={"source_type": "memory", "content": "a"},
    )
    upsert_point(
        qdrant, collection=TEST_COLLECTION,
        point_id=_uuid(), vector=vec2,
        payload={"source_type": "knowledge", "content": "b"},
    )
    results = search(
        qdrant, collection=TEST_COLLECTION,
        query_vector=vec1, limit=10, source_type="memory",
    )
    source_types = {r["payload"]["source_type"] for r in results}
    assert "knowledge" not in source_types


def test_delete_point(qdrant):
    pid = _uuid()
    vec = _random_vector()
    upsert_point(
        qdrant, collection=TEST_COLLECTION,
        point_id=pid, vector=vec,
        payload={"content": "deleteme"},
    )
    delete_point(qdrant, collection=TEST_COLLECTION, point_id=pid)
    results = search(qdrant, collection=TEST_COLLECTION, query_vector=vec, limit=10)
    ids = {r["id"] for r in results}
    assert pid not in ids


def test_update_payload(qdrant):
    pid = _uuid()
    vec = _random_vector()
    upsert_point(
        qdrant, collection=TEST_COLLECTION,
        point_id=pid, vector=vec,
        payload={"content": "original", "source_type": "memory"},
    )
    update_payload(
        qdrant, collection=TEST_COLLECTION,
        point_id=pid, payload={"content": "updated", "extra": "field"},
    )
    results = search(qdrant, collection=TEST_COLLECTION, query_vector=vec, limit=1)
    match = [r for r in results if r["id"] == pid]
    assert len(match) == 1
    assert match[0]["payload"]["content"] == "updated"
    assert match[0]["payload"]["extra"] == "field"
    # source_type should still be there (set_payload merges)
    assert match[0]["payload"]["source_type"] == "memory"


def test_get_point(qdrant):
    pid = _uuid()
    vec = _random_vector()
    upsert_point(
        qdrant,
        collection=TEST_COLLECTION,
        point_id=pid,
        vector=vec,
        payload={"content": "get_point test", "source_type": "memory"},
    )
    result = get_point(qdrant, collection=TEST_COLLECTION, point_id=pid)
    assert result is not None
    assert result["id"] == pid
    assert result["payload"]["content"] == "get_point test"


def test_get_point_not_found(qdrant):
    result = get_point(qdrant, collection=TEST_COLLECTION, point_id=_uuid())
    assert result is None


def test_scroll_points(qdrant):
    # Insert a known point
    pid = _uuid()
    vec = _random_vector()
    upsert_point(
        qdrant,
        collection=TEST_COLLECTION,
        point_id=pid,
        vector=vec,
        payload={"content": "scroll test", "source_type": "scroll_marker"},
    )
    points, next_offset = scroll_points(qdrant, collection=TEST_COLLECTION, limit=100)
    assert isinstance(points, list)
    assert len(points) >= 1
    # Verify structure
    assert "id" in points[0]
    assert "payload" in points[0]
    # next_offset should be None or a string (no more pages for small dataset)


def test_scroll_points_with_filter(qdrant):
    pid = _uuid()
    vec = _random_vector()
    upsert_point(
        qdrant,
        collection=TEST_COLLECTION,
        point_id=pid,
        vector=vec,
        payload={"content": "filtered scroll", "source_type": "scroll_filter_test"},
    )
    points, _ = scroll_points(
        qdrant,
        collection=TEST_COLLECTION,
        limit=100,
        payload_filter={"source_type": "scroll_filter_test"},
    )
    assert len(points) >= 1
    for p in points:
        assert p["payload"]["source_type"] == "scroll_filter_test"


def test_ensure_collections(qdrant):
    from genesis.qdrant.collections import ensure_collections

    ensure_collections(qdrant)
    existing = {c.name for c in qdrant.get_collections().collections}
    assert "episodic_memory" in existing
    assert "knowledge_base" in existing
    # NOTE: Do NOT delete production collections here. ensure_collections is
    # idempotent — leaving them is safe. Deleting them nukes production data.
