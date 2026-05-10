"""Tests for knowledge CRUD operations."""

import pytest

from genesis.db.crud import evolution_proposals, knowledge
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    import aiosqlite

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = None
    await create_all_tables(conn)
    yield conn
    await conn.close()


# ─── knowledge.insert / get ──────────────────────────────────────────────────


async def test_insert_and_get(db):
    uid = await knowledge.insert(
        db,
        project_type="cloud-eng",
        domain="aws-vpc",
        source_doc="module-3",
        concept="VPC Subnet Config",
        body="A VPC subnet is a subdivision of the VPC CIDR range.",
    )
    assert uid  # non-empty string
    row = await knowledge.get(db, uid)
    assert row is not None
    assert row["project_type"] == "cloud-eng"
    assert row["domain"] == "aws-vpc"
    assert row["concept"] == "VPC Subnet Config"
    assert row["confidence"] == 0.85


async def test_get_nonexistent(db):
    assert await knowledge.get(db, "nonexistent") is None


async def test_insert_with_all_fields(db):
    uid = await knowledge.insert(
        db,
        project_type="ml",
        domain="transformers",
        source_doc="course-1",
        concept="Attention",
        body="Self-attention computes...",
        source_platform="thinkific",
        section_title="Module 5",
        relationships='["embeddings", "positional-encoding"]',
        caveats='["simplified explanation"]',
        tags='["ml", "attention"]',
        confidence=0.92,
        source_date="2026-01-15",
        embedding_model="qwen3-embedding:0.6b",
        source_pipeline="curated",
        purpose='["resume-prep"]',
        ingestion_source="/home/user/docs/course.pdf",
    )
    row = await knowledge.get(db, uid)
    assert row["source_platform"] == "thinkific"
    assert row["confidence"] == 0.92
    assert '"ml"' in row["tags"]
    assert row["source_pipeline"] == "curated"
    assert row["purpose"] == '["resume-prep"]'
    assert row["ingestion_source"] == "/home/user/docs/course.pdf"


# ─── knowledge.search_fts ────────────────────────────────────────────────────


async def test_fts_search(db):
    await knowledge.insert(
        db, project_type="cloud", domain="aws", source_doc="m1",
        concept="VPC", body="Virtual Private Cloud for network isolation",
    )
    await knowledge.insert(
        db, project_type="cloud", domain="gcp", source_doc="m2",
        concept="GKE", body="Google Kubernetes Engine for container orchestration",
    )

    results = await knowledge.search_fts(db, "network isolation")
    assert len(results) >= 1
    assert results[0]["concept"] == "VPC"


async def test_fts_search_with_project_filter(db):
    await knowledge.insert(
        db, project_type="cloud", domain="aws", source_doc="m1",
        concept="VPC", body="Virtual Private Cloud",
    )
    await knowledge.insert(
        db, project_type="ml", domain="nlp", source_doc="m2",
        concept="NLP Cloud", body="Cloud-based NLP services",
    )

    results = await knowledge.search_fts(db, "cloud", project="ml")
    assert all(r["project_type"] == "ml" for r in results)


async def test_fts_search_with_domain_filter(db):
    await knowledge.insert(
        db, project_type="cloud", domain="aws", source_doc="m1",
        concept="VPC", body="AWS networking",
    )
    await knowledge.insert(
        db, project_type="cloud", domain="gcp", source_doc="m2",
        concept="VPC", body="GCP networking",
    )

    results = await knowledge.search_fts(db, "networking", domain="gcp")
    assert all(r["domain"] == "gcp" for r in results)


async def test_fts_search_returns_source_pipeline(db):
    await knowledge.insert(
        db, project_type="cloud", domain="aws", source_doc="m1",
        concept="VPC", body="Virtual Private Cloud for network isolation",
        source_pipeline="curated",
    )
    results = await knowledge.search_fts(db, "network isolation")
    assert len(results) >= 1
    assert results[0]["source_pipeline"] == "curated"


# ─── knowledge.stats ─────────────────────────────────────────────────────────


async def test_stats_empty(db):
    s = await knowledge.stats(db)
    assert s["total"] == 0
    assert s["by_domain"] == {}


async def test_stats_with_data(db):
    await knowledge.insert(
        db, project_type="cloud", domain="aws", source_doc="m1",
        concept="VPC", body="vpc", source_pipeline="curated",
    )
    await knowledge.insert(
        db, project_type="cloud", domain="gcp", source_doc="m2",
        concept="GKE", body="gke", source_pipeline="recon",
    )
    await knowledge.insert(
        db, project_type="cloud", domain="aws", source_doc="m3",
        concept="S3", body="s3",
    )

    s = await knowledge.stats(db)
    assert s["total"] == 3
    assert s["by_domain"]["aws"] == 2
    assert s["by_domain"]["gcp"] == 1
    assert s["by_tier"]["curated"] == 1
    assert s["by_tier"]["recon"] == 1
    assert s["by_tier"]["unknown"] == 1


async def test_stats_filtered_by_project(db):
    await knowledge.insert(
        db, project_type="cloud", domain="aws", source_doc="m1",
        concept="VPC", body="vpc",
    )
    await knowledge.insert(
        db, project_type="ml", domain="nlp", source_doc="m2",
        concept="NLP", body="nlp",
    )

    s = await knowledge.stats(db, project="cloud")
    assert s["total"] == 1


# ─── knowledge.delete ────────────────────────────────────────────────────────


async def test_delete(db):
    uid = await knowledge.insert(
        db, project_type="cloud", domain="aws", source_doc="m1",
        concept="VPC", body="vpc content for testing deletion",
    )
    assert await knowledge.delete(db, uid)
    assert await knowledge.get(db, uid) is None

    # FTS should also be cleared
    results = await knowledge.search_fts(db, "vpc content")
    assert len(results) == 0


async def test_delete_nonexistent(db):
    assert not await knowledge.delete(db, "nonexistent")


# ─── knowledge.find_by_unique_key ────────────────────────────────────────────


async def test_find_by_unique_key_hit(db):
    uid = await knowledge.insert(
        db, project_type="reference", domain="reference.credentials",
        source_doc="manual", concept="ScarletAndRage login",
        body="forum creds",
    )
    row = await knowledge.find_by_unique_key(
        db, project_type="reference", domain="reference.credentials",
        concept="ScarletAndRage login",
    )
    assert row is not None
    assert row["id"] == uid
    assert row["body"] == "forum creds"


async def test_find_by_unique_key_miss(db):
    row = await knowledge.find_by_unique_key(
        db, project_type="reference", domain="reference.credentials",
        concept="nonexistent",
    )
    assert row is None


# ─── knowledge.upsert ────────────────────────────────────────────────────────


async def test_upsert_insert_path(db):
    uid, inserted = await knowledge.upsert(
        db, project_type="reference", domain="reference.urls",
        source_doc="session-a", concept="ScarletAndRage forum",
        body="https://forum.thescarletandrage.com — Ohio State fan forum",
    )
    assert inserted is True
    assert uid
    row = await knowledge.get(db, uid)
    assert row is not None
    assert row["body"] == "https://forum.thescarletandrage.com — Ohio State fan forum"


async def test_upsert_update_path_preserves_id(db):
    uid_a, inserted_a = await knowledge.upsert(
        db, project_type="reference", domain="reference.network",
        source_doc="session-a", concept="Container IP",
        body="${CONTAINER_IP:-localhost}",
    )
    assert inserted_a is True

    # Re-upsert with same unique key but updated body
    uid_b, inserted_b = await knowledge.upsert(
        db, project_type="reference", domain="reference.network",
        source_doc="session-b", concept="Container IP",
        body="${CONTAINER_IP:-localhost} (Incus container running Genesis runtime)",
    )
    assert inserted_b is False
    assert uid_b == uid_a  # stable id on conflict

    row = await knowledge.get(db, uid_b)
    assert row["body"] == "${CONTAINER_IP:-localhost} (Incus container running Genesis runtime)"
    assert row["source_doc"] == "session-b"


async def test_upsert_update_path_preserves_retrieved_count(db):
    uid, _ = await knowledge.upsert(
        db, project_type="reference", domain="reference.facts",
        source_doc="m1", concept="fact A", body="body v1",
    )
    # Manually bump retrieved_count to simulate retrieval activity
    await db.execute(
        "UPDATE knowledge_units SET retrieved_count = 5 WHERE id = ?",
        (uid,),
    )
    await db.commit()

    await knowledge.upsert(
        db, project_type="reference", domain="reference.facts",
        source_doc="m1", concept="fact A", body="body v2",
    )
    row = await knowledge.get(db, uid)
    assert row["retrieved_count"] == 5  # not reset on update
    assert row["body"] == "body v2"


async def test_upsert_fts_shadow_row_updated(db):
    uid, _ = await knowledge.upsert(
        db, project_type="reference", domain="reference.urls",
        source_doc="m1", concept="test url",
        body="first body text for full-text search",
    )
    results_before = await knowledge.search_fts(db, "first body")
    assert any(r["unit_id"] == uid for r in results_before)

    await knowledge.upsert(
        db, project_type="reference", domain="reference.urls",
        source_doc="m1", concept="test url",
        body="replacement body content indexed fresh",
    )
    results_old = await knowledge.search_fts(db, "first body")
    # Old content no longer indexed
    assert not any(r["unit_id"] == uid for r in results_old)
    results_new = await knowledge.search_fts(db, "replacement body")
    assert any(r["unit_id"] == uid for r in results_new)


async def test_upsert_unique_constraint_on_insert(db):
    """Direct insert() should fail if a row already exists with the same unique key."""
    import aiosqlite

    await knowledge.insert(
        db, project_type="reference", domain="reference.urls",
        source_doc="m1", concept="already exists", body="first",
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await knowledge.insert(
            db, project_type="reference", domain="reference.urls",
            source_doc="m2", concept="already exists", body="second",
        )


# ─── evolution_proposals ─────────────────────────────────────────────────────


async def test_proposal_create_and_get(db):
    pid = await evolution_proposals.create(
        db,
        proposal_type="soul_update",
        current_content="old content",
        proposed_change="new content",
        rationale="better phrasing",
    )
    row = await evolution_proposals.get(db, pid)
    assert row is not None
    assert row["status"] == "pending"
    assert row["proposal_type"] == "soul_update"
    assert row["rationale"] == "better phrasing"


async def test_proposal_update_status(db):
    pid = await evolution_proposals.create(
        db,
        proposal_type="steering_rule",
        current_content="old",
        proposed_change="new",
        rationale="reason",
    )
    assert await evolution_proposals.update_status(db, pid, "approved")
    row = await evolution_proposals.get(db, pid)
    assert row["status"] == "approved"
    assert row["reviewed_at"] is not None


async def test_proposal_list_pending(db):
    await evolution_proposals.create(
        db, proposal_type="a", current_content="x",
        proposed_change="y", rationale="z",
    )
    pid2 = await evolution_proposals.create(
        db, proposal_type="b", current_content="x",
        proposed_change="y", rationale="z",
    )
    await evolution_proposals.update_status(db, pid2, "approved")

    pending = await evolution_proposals.list_pending(db)
    assert len(pending) == 1
    assert pending[0]["proposal_type"] == "a"


async def test_proposal_list_pending_filters(db):
    """`since` and `proposal_type` filters narrow the pending list."""
    pid_a = await evolution_proposals.create(
        db, proposal_type="alpha", current_content="x",
        proposed_change="y", rationale="z",
    )
    pid_b = await evolution_proposals.create(
        db, proposal_type="beta", current_content="x",
        proposed_change="y", rationale="z",
    )

    # proposal_type filter
    alphas = await evolution_proposals.list_pending(db, proposal_type="alpha")
    assert len(alphas) == 1
    assert alphas[0]["id"] == pid_a

    betas = await evolution_proposals.list_pending(db, proposal_type="beta")
    assert len(betas) == 1
    assert betas[0]["id"] == pid_b

    # since filter — far-future timestamp filters everything out
    none = await evolution_proposals.list_pending(db, since="9999-01-01T00:00:00+00:00")
    assert none == []

    # since filter — far-past returns all pending
    all_pending = await evolution_proposals.list_pending(db, since="1970-01-01T00:00:00+00:00")
    assert len(all_pending) == 2

    # combined filters
    only_alpha = await evolution_proposals.list_pending(
        db, since="1970-01-01T00:00:00+00:00", proposal_type="alpha",
    )
    assert len(only_alpha) == 1
    assert only_alpha[0]["id"] == pid_a
