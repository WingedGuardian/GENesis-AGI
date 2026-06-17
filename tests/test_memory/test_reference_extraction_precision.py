"""Precision regression tests for the reference classifier.

The 2h ``memory_extraction`` job runs ``classify_as_reference`` over
LLM-generated extraction prose. Before this fix it mis-classified ordinary
conversation/analysis as references, fabricating credential/network values:

- credential patterns matched a bare label word + space + the next word, so
  prose like "can pin checkpoints" / "content pass committed" produced a
  credential with value "checkpoints" / "committed".
- the network gate checked for a context word ANYWHERE in the content (and by
  substring), so an incidental IP plus a far-away "container" / "IP" inside
  "IPs" produced a network reference.

These tests pin the exact junk that was found in the live store, plus a
pair-prose class case, and guard that genuine references still classify.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.memory.extraction import Extraction
from genesis.memory.reference_extraction import (
    classify_as_reference,
    extract_references_from_chunk,
)


def _ext(content: str, entities: list[str] | None = None) -> Extraction:
    return Extraction(
        content=content,
        extraction_type="entity",
        confidence=0.9,
        entities=entities or [],
    )


# ─── Must NOT classify (the real junk that polluted the store) ────────────────


class TestRejectsProseFalsePositives:
    def test_sqlite_wal_insight_is_not_a_credential(self):
        # "can pin checkpoints" → was captured as credentials value "checkpoints"
        ext = _ext(
            "Long-held read transactions in SQLite WAL mode can pin checkpoints, "
            "causing unbounded WAL growth (1.9 GB observed) and system-wide lock "
            "contention. This is a durable system insight about SQLite behavior.",
            entities=["SQLite"],
        )
        assert classify_as_reference(ext) is None

    def test_portfolio_status_is_not_a_credential(self):
        # "content pass committed" → was captured as credentials value "committed"
        ext = _ext(
            "The portfolio website is complete, with all four threads and the "
            "content pass committed to main, authored by Jay Wingard.",
            entities=["portfolio website"],
        )
        assert classify_as_reference(ext) is None

    def test_code_analysis_with_incidental_ip_is_not_network(self):
        # IP appears inside code-analysis prose; the only context word ("IP" in
        # "IPs") is far from the IP → must not become a network reference.
        ext = _ext(
            "FTS search already handles IP addresses correctly: _prepare_fts5 "
            "converts dots to spaces (203.0.113.45 to 192 168 50 123 as ANDed "
            "tokens) and the porter tokenizer splits indexed IPs the same way.",
            entities=["_prepare_fts5"],
        )
        assert classify_as_reference(ext) is None

    def test_planning_commentary_with_incidental_ip_is_not_network(self):
        # "incus container workflow" is ~130 chars from the IP → far-away context
        # word must not promote this planning prose to a network reference.
        ext = _ext(
            "The straggler credential 04d00c42 (203.0.113.45, null qdrant_id, "
            "0 FTS rows) is no longer the only copy. A clean entry cb50ffc8 now "
            "exists. The broken one holds key-based SSH and incus container "
            "workflow guidance; the working one holds the value.",
            entities=["04d00c42"],
        )
        assert classify_as_reference(ext) is None

    def test_labeled_token_prose_is_not_a_credential(self):
        # "api key authentication-middleware-layer" → bare-space token match
        # captured a fabricated token from code-description prose.
        ext = _ext(
            "The api key authentication-middleware-layer handles all inbound "
            "requests for the gateway before they reach the service.",
            entities=["api key"],
        )
        assert classify_as_reference(ext) is None

    def test_pair_prose_is_not_a_credential(self):
        # "user can pass through" → bare-space pair match captured can / through.
        ext = _ext(
            "The user can pass through the gateway to reach the backend service "
            "without any additional authentication being required.",
            entities=["gateway"],
        )
        assert classify_as_reference(ext) is None


# ─── Must STILL classify (guards the fix didn't over-correct) ─────────────────


class TestStillClassifiesGenuine:
    def test_colon_separated_single_credential_still_captured(self):
        ext = _ext(
            "The Home Assistant dashboard password: Hunter2!xyz for the admin user",
            entities=["Home Assistant"],
        )
        ref = classify_as_reference(ext)
        assert ref is not None
        assert ref["kind"] == "credentials"
        assert "Hunter2!xyz" in ref["value"]

    def test_lowercase_password_pair_still_captured(self):
        # all-lowercase passwords are legitimate (no entropy gate that rejects them)
        ext = _ext(
            "ScarletAndRage login: username: buckeye614 password: verysecretword",
            entities=["ScarletAndRage"],
        )
        ref = classify_as_reference(ext)
        assert ref is not None
        assert ref["kind"] == "credentials"
        assert "verysecretword" in ref["value"]

    def test_ip_with_adjacent_context_still_network(self):
        ext = _ext(
            "Server runs on host 203.0.113.50 listening for connections",
            entities=["server"],
        )
        ref = classify_as_reference(ext)
        assert ref is not None
        assert ref["kind"] == "network"
        assert ref["value"] == "203.0.113.50"


# ─── End-to-end: full classify → ingest path over a mixed chunk ───────────────


@pytest.fixture
async def _db():
    async with aiosqlite.connect(":memory:") as conn:
        await create_all_tables(conn)
        await conn.commit()
        yield conn


@pytest.fixture
def _store():
    store = AsyncMock()
    store.store = AsyncMock(return_value="qid")
    store.delete = AsyncMock()
    store._embeddings = MagicMock()
    store._embeddings.model_name = "test-embed"
    return store


async def test_mixed_chunk_ingests_only_genuine_references(_db, _store):
    """E2E: a chunk of 4 junk-prose + 2 real refs ingests exactly the 2 real."""
    extractions = [
        _ext(
            "Long-held read transactions in SQLite WAL mode can pin checkpoints, "
            "causing unbounded WAL growth and system-wide lock contention.",
            ["SQLite"],
        ),
        _ext(
            "The portfolio website is complete; the content pass committed to "
            "main, authored by Jay Wingard.",
            ["portfolio website"],
        ),
        _ext(
            "FTS handles IP addresses: _prepare_fts5 converts the dots in "
            "203.0.113.45 to spaces; the porter tokenizer splits indexed IPs.",
            ["_prepare_fts5"],
        ),
        _ext(
            "The user can pass through the gateway to reach the backend service "
            "with no extra authentication required.",
            ["gateway"],
        ),
        # genuine references that MUST still be captured:
        _ext(
            "ScarletAndRage login: username: buckeye614 password: Sup3r!secret",
            ["ScarletAndRage"],
        ),
        _ext(
            "The Genesis API server runs on host 203.0.113.50 for embeddings",
            ["Genesis API"],
        ),
    ]
    count = await extract_references_from_chunk(
        extractions, store=_store, db=_db, source_session_id="e2e",
    )
    assert count == 2

    cur = await _db.execute(
        "SELECT domain FROM knowledge_units WHERE project_type='reference' "
        "ORDER BY domain"
    )
    domains = [r[0] for r in await cur.fetchall()]
    assert domains == ["reference.credentials", "reference.network"]
