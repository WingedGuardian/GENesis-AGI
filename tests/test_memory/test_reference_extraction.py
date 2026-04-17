"""Tests for the automatic reference extraction pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.memory.extraction import Extraction
from genesis.memory.reference_extraction import (
    classify_as_reference,
    extract_references_from_chunk,
    ingest_reference_from_extraction,
)

# ─── Classifier: credential pair ──────────────────────────────────────────────


class TestClassifyCredentialPair:
    def test_colon_separated(self):
        ext = Extraction(
            content="Jay's ScarletAndRage login: username: 614Buckeye password: OhioState614!Bucks",
            extraction_type="entity",
            confidence=0.9,
            entities=["ScarletAndRage", "614Buckeye"],
        )
        ref = classify_as_reference(ext)
        assert ref is not None
        assert ref["kind"] == "credentials"
        assert "614Buckeye" in ref["value"]
        assert "OhioState614!Bucks" in ref["value"]
        assert ref["identifier"] == "ScarletAndRage"

    def test_natural_language(self):
        ext = Extraction(
            content="The user's login is 614Buckeye and the password is OhioState614!Bucks",
            extraction_type="entity",
            confidence=0.85,
            entities=[],
        )
        ref = classify_as_reference(ext)
        assert ref is not None
        assert ref["kind"] == "credentials"

    def test_rejects_ephemeral_example(self):
        ext = Extraction(
            content="For example, the username: test password: secret would work",
            extraction_type="entity",
            confidence=0.8,
        )
        assert classify_as_reference(ext) is None

    def test_rejects_placeholder(self):
        ext = Extraction(
            content="TODO: set the username: xxx password: yyy once provisioned",
            extraction_type="entity",
            confidence=0.8,
        )
        assert classify_as_reference(ext) is None

    def test_rejects_too_short_password(self):
        ext = Extraction(
            content="username: u password: ab",
            extraction_type="entity",
            confidence=0.9,
        )
        assert classify_as_reference(ext) is None


# ─── Classifier: standalone token ────────────────────────────────────────────


class TestClassifyToken:
    def test_api_key(self):
        ext = Extraction(
            content="The GitHub API key is ghp_abcdefghijklmnopqrstuvwxyz1234",
            extraction_type="entity",
            confidence=0.9,
            entities=["GitHub"],
        )
        ref = classify_as_reference(ext)
        assert ref is not None
        assert ref["kind"] == "credentials"
        assert "ghp_abcdefghijklmnopqrstuvwxyz1234" in ref["value"]

    def test_short_token_rejected(self):
        ext = Extraction(
            content="The api_key is short",
            extraction_type="entity",
            confidence=0.9,
        )
        assert classify_as_reference(ext) is None


# ─── Classifier: URL with context ────────────────────────────────────────────


class TestClassifyUrl:
    def test_url_with_description(self):
        ext = Extraction(
            content=(
                "The Ohio State fan forum at https://forum.thescarletandrage.com/ "
                "is where the 614Buckeye persona posts"
            ),
            extraction_type="entity",
            confidence=0.85,
            entities=["ScarletAndRage"],
        )
        ref = classify_as_reference(ext)
        assert ref is not None
        assert ref["kind"] == "url"
        assert ref["value"] == "https://forum.thescarletandrage.com/"

    def test_bare_url_rejected(self):
        ext = Extraction(
            content="https://example.com",
            extraction_type="entity",
            confidence=0.9,
        )
        assert classify_as_reference(ext) is None


# ─── Classifier: IP address with network context ────────────────────────────


class TestClassifyNetwork:
    def test_container_ip(self):
        ext = Extraction(
            content="The Genesis container runs at IP address ${CONTAINER_IP:-localhost}",
            extraction_type="entity",
            confidence=0.9,
            entities=["Genesis container"],
        )
        ref = classify_as_reference(ext)
        assert ref is not None
        assert ref["kind"] == "network"
        assert ref["value"] == "${CONTAINER_IP:-localhost}"

    def test_ip_without_context_rejected(self):
        ext = Extraction(
            content="Version ${CONTAINER_IP:-localhost} was released last week",
            extraction_type="entity",
            confidence=0.9,
        )
        # Technically matches IPv4 regex but no network context words
        assert classify_as_reference(ext) is None

    def test_ip_with_port(self):
        ext = Extraction(
            content="Ollama server listens on ${OLLAMA_URL:-localhost:11434} for embeddings",
            extraction_type="entity",
            confidence=0.85,
            entities=["Ollama"],
        )
        ref = classify_as_reference(ext)
        assert ref is not None
        assert ref["kind"] == "network"
        assert "${OLLAMA_URL:-localhost:11434}" in ref["value"]


# ─── Classifier: non-reference extractions ───────────────────────────────────


class TestClassifyNonReference:
    def test_plain_statement(self):
        ext = Extraction(
            content="Jay decided to move forward with the memory rebalance plan",
            extraction_type="decision",
            confidence=0.9,
        )
        assert classify_as_reference(ext) is None

    def test_too_short(self):
        ext = Extraction(
            content="test",
            extraction_type="entity",
            confidence=0.5,
        )
        assert classify_as_reference(ext) is None

    def test_empty(self):
        ext = Extraction(
            content="",
            extraction_type="entity",
            confidence=0.5,
        )
        assert classify_as_reference(ext) is None


# ─── Full ingest pipeline ────────────────────────────────────────────────────


@pytest.fixture
async def db_with_schema():
    async with aiosqlite.connect(":memory:") as conn:
        await create_all_tables(conn)
        await conn.commit()
        yield conn


@pytest.fixture
def mock_store():
    store = AsyncMock()
    store.store = AsyncMock(return_value="qdrant-test-id")
    store.delete = AsyncMock()
    store._embeddings = MagicMock()
    store._embeddings.model_name = "test-embed"
    return store


async def test_ingest_classified_extraction(db_with_schema, mock_store):
    """A credential-shaped extraction gets ingested as a reference."""
    ext = Extraction(
        content=(
            "ScarletAndRage forum login for the 614Buckeye persona: "
            "username: 614Buckeye password: OhioState614!Bucks"
        ),
        extraction_type="entity",
        confidence=0.95,
        entities=["ScarletAndRage"],
    )
    unit_id = await ingest_reference_from_extraction(
        ext,
        store=mock_store,
        db=db_with_schema,
        source_session_id="test-session-abc",
    )
    assert unit_id is not None

    # Verify row landed in knowledge_units
    cursor = await db_with_schema.execute(
        "SELECT project_type, domain, concept, body, tags FROM knowledge_units "
        "WHERE id = ?",
        (unit_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "reference"
    assert row[1] == "reference.credentials"
    assert row[2] == "ScarletAndRage"
    assert "614Buckeye" in row[3]
    assert "OhioState614!Bucks" in row[3]
    assert "reference.credentials" in row[3]  # header salt
    assert "credentials" in row[4]
    assert "reference" in row[4]


async def test_ingest_non_reference_returns_none(db_with_schema, mock_store):
    """A plain decision extraction does not create a reference entry."""
    ext = Extraction(
        content="We decided to ship the new memory pipeline on Monday",
        extraction_type="decision",
        confidence=0.9,
    )
    unit_id = await ingest_reference_from_extraction(
        ext,
        store=mock_store,
        db=db_with_schema,
        source_session_id="test-session-abc",
    )
    assert unit_id is None
    cursor = await db_with_schema.execute(
        "SELECT COUNT(*) FROM knowledge_units WHERE project_type = 'reference'"
    )
    assert (await cursor.fetchone())[0] == 0


async def test_ingest_upsert_on_duplicate(db_with_schema, mock_store):
    """Re-ingesting the same logical extraction updates in place."""
    ext1 = Extraction(
        content="Container IP is ${CONTAINER_IP:-localhost} for the Genesis runtime container",
        extraction_type="entity",
        confidence=0.9,
        entities=["Genesis runtime"],
    )
    uid1 = await ingest_reference_from_extraction(
        ext1, store=mock_store, db=db_with_schema, source_session_id="s1",
    )
    assert uid1 is not None

    # Re-ingest with the same identifier
    mock_store.store = AsyncMock(return_value="qdrant-test-id-2")
    ext2 = Extraction(
        content="Container IP is 10.176.34.207 for the Genesis runtime container (rotated)",
        extraction_type="entity",
        confidence=0.92,
        entities=["Genesis runtime"],
    )
    uid2 = await ingest_reference_from_extraction(
        ext2, store=mock_store, db=db_with_schema, source_session_id="s2",
    )
    # Same identifier → same id (upsert)
    assert uid2 == uid1

    cursor = await db_with_schema.execute(
        "SELECT COUNT(*) FROM knowledge_units WHERE project_type = 'reference'"
    )
    assert (await cursor.fetchone())[0] == 1

    cursor = await db_with_schema.execute(
        "SELECT body FROM knowledge_units WHERE id = ?", (uid1,),
    )
    body = (await cursor.fetchone())[0]
    assert "10.176.34.207" in body
    assert "rotated" in body


async def test_extract_references_from_chunk_counts(db_with_schema, mock_store):
    """extract_references_from_chunk returns the count of classified refs."""
    store_calls = [0]

    async def fake_store(*args, **kwargs):
        store_calls[0] += 1
        return f"qdrant-{store_calls[0]}"

    mock_store.store = AsyncMock(side_effect=fake_store)

    extractions = [
        Extraction(
            content="username: user1 password: verysecret for example.com login",
            extraction_type="entity",
            confidence=0.9,
            entities=["example.com"],
        ),
        Extraction(
            content="The forum is at https://forum.example.com for public discussion",
            extraction_type="entity",
            confidence=0.85,
            entities=["forum"],
        ),
        Extraction(
            content="We decided to ship on Monday",
            extraction_type="decision",
            confidence=0.9,
        ),
        Extraction(
            content="Server runs on host 192.168.1.50 listening for connections",
            extraction_type="entity",
            confidence=0.9,
            entities=["server"],
        ),
    ]
    count = await extract_references_from_chunk(
        extractions, store=mock_store, db=db_with_schema,
        source_session_id="test",
    )
    assert count == 3  # credentials, url, network — not the decision

    cursor = await db_with_schema.execute(
        "SELECT COUNT(*) FROM knowledge_units WHERE project_type = 'reference'"
    )
    assert (await cursor.fetchone())[0] == 3


async def test_ingest_never_raises(db_with_schema):
    """Classifier / ingestion errors log a warning and return None."""
    broken_store = AsyncMock()
    broken_store.store = AsyncMock(side_effect=RuntimeError("Qdrant offline"))
    broken_store.delete = AsyncMock()
    broken_store._embeddings = MagicMock()
    broken_store._embeddings.model_name = "m"

    ext = Extraction(
        content="username: user1 password: verysecret for service.example login",
        extraction_type="entity",
        confidence=0.9,
        entities=["service.example"],
    )
    result = await ingest_reference_from_extraction(
        ext, store=broken_store, db=db_with_schema,
        source_session_id="test",
    )
    assert result is None  # Error swallowed, None returned

    cursor = await db_with_schema.execute(
        "SELECT COUNT(*) FROM knowledge_units WHERE project_type = 'reference'"
    )
    assert (await cursor.fetchone())[0] == 0
