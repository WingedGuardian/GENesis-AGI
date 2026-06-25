"""Tests for user-shared gating + provenance marking of auto-captured references.

Root-cause fix for reference-store noise: the 2h extraction job ran the regex
classifier over LLM prose from BOTH user and Genesis turns, so Genesis's own
analysis ("HybridRetriever.recall() -> password: rerank") became fake references.
Fix: gate auto-capture to values that appear in the USER's own words, and mark
auto-captured entries distinctly (``auto_captured`` tag + confidence 0.60) so
they're separable from real-time ``reference_store`` captures.

The gate is opt-in via ``user_text`` — ``None`` means ungated (back-compat).
"""

from __future__ import annotations

import json
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


def _ext(content: str, entities: list[str] | None = None) -> Extraction:
    return Extraction(
        content=content,
        extraction_type="entity",
        confidence=0.9,
        entities=entities or [],
    )


# ─── Gate: only capture values that appear in the USER's own words ────────────


class TestUserSharedGate:
    def test_no_user_text_is_ungated(self):
        # Back-compat: user_text=None must behave exactly as before.
        ext = _ext("The admin password: Hunter2!xyz for the box")
        assert classify_as_reference(ext) is not None
        assert classify_as_reference(ext, user_text=None) is not None

    def test_user_shared_credential_captured(self):
        ext = _ext("The admin password: Hunter2!xyz for the box")
        ref = classify_as_reference(ext, user_text="here it is, password: Hunter2!xyz")
        assert ref is not None
        assert ref["kind"] == "credentials"
        assert "Hunter2!xyz" in ref["value"]

    def test_genesis_derived_credential_dropped(self):
        # The secret is in Genesis's analysis prose, NOT the user's words.
        ext = _ext("The service password: rerankXYZ is used internally")
        assert classify_as_reference(
            ext, user_text="user asked about ranking and latency",
        ) is None

    def test_user_shared_pair_captured(self):
        ext = _ext("login: username: bob and password: Hunter2!xyz works")
        ref = classify_as_reference(
            ext, user_text="username: bob password: Hunter2!xyz",
        )
        assert ref is not None and ref["kind"] == "credentials"

    def test_user_shared_url_captured(self):
        ext = _ext("Bookmark the dashboard at https://example.com/admin for later")
        ref = classify_as_reference(
            ext, user_text="save this: https://example.com/admin please",
        )
        assert ref is not None and ref["kind"] == "url"

    def test_genesis_derived_url_dropped(self):
        ext = _ext("Genesis suggests checking https://docs.internal.dev/guide first")
        assert classify_as_reference(
            ext, user_text="how do i call the api",
        ) is None

    def test_user_shared_ip_captured(self):
        ext = _ext("The server runs on host 203.0.113.10 for the app")
        ref = classify_as_reference(
            ext, user_text="the server is at 203.0.113.10",
        )
        assert ref is not None and ref["kind"] == "network"

    def test_genesis_derived_ip_dropped(self):
        ext = _ext("The container host 203.0.113.99 appeared in the debug trace")
        assert classify_as_reference(
            ext, user_text="why did the debug run fail",
        ) is None

    def test_gate_is_case_insensitive(self):
        # LLM may normalize case; the gate must not false-drop on case alone.
        ext = _ext("password: SecretPass99 for the account")
        ref = classify_as_reference(
            ext, user_text="my password is secretpass99",
        )
        assert ref is not None


# ─── Marking: auto_captured tag + confidence 0.60 on ingest ───────────────────


@pytest.fixture
async def _db():
    async with aiosqlite.connect(":memory:") as conn:
        await create_all_tables(conn)
        await conn.commit()
        yield conn


@pytest.fixture
def _store():
    s = AsyncMock()
    s.store = AsyncMock(return_value="qid")
    s.delete = AsyncMock()
    s._embeddings = MagicMock()
    s._embeddings.model_name = "test-embed"
    return s


async def test_ingest_marks_auto_captured_and_low_confidence(_db, _store):
    ext = _ext(
        "login: username: bob password: Hunter2!xyz",
        entities=["MyService"],
    )
    uid = await ingest_reference_from_extraction(
        ext, store=_store, db=_db, source_session_id="s",
        user_text="username: bob password: Hunter2!xyz",
    )
    assert uid is not None
    cur = await _db.execute(
        "SELECT tags, confidence FROM knowledge_units WHERE id=?", (uid,),
    )
    tags, confidence = await cur.fetchone()
    assert "auto_captured" in json.loads(tags)
    assert confidence == 0.60


async def test_ingest_gate_blocks_non_user_shared(_db, _store):
    ext = _ext("The internal password: rerankXYZ for ranking")
    uid = await ingest_reference_from_extraction(
        ext, store=_store, db=_db, source_session_id="s",
        user_text="how does ranking work",
    )
    assert uid is None
    cur = await _db.execute(
        "SELECT COUNT(*) FROM knowledge_units WHERE project_type='reference'"
    )
    assert (await cur.fetchone())[0] == 0


async def test_chunk_gating_e2e(_db, _store):
    """E2E: a chunk mixing Genesis-analysis + a user-shared cred, gated by
    user_text, ingests ONLY the user-shared one (marked auto_captured/0.60)."""
    extractions = [
        # Genesis analysis prose — value not in the user's words → dropped.
        _ext("The internal password: rerankXYZ is used for ranking", ["Ranking"]),
        # User-shared credential → captured.
        _ext("login username: bob password: Hunter2!xyz", ["MyService"]),
    ]
    user_text = "here are my creds: username: bob password: Hunter2!xyz"
    count = await extract_references_from_chunk(
        extractions, store=_store, db=_db, source_session_id="s",
        user_text=user_text,
    )
    assert count == 1
    cur = await _db.execute(
        "SELECT tags, confidence FROM knowledge_units WHERE project_type='reference'"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    tags, confidence = rows[0]
    assert "auto_captured" in json.loads(tags)
    assert confidence == 0.60
