"""Tests for first-party vs external-world provenance labeling (audit D12).

The KB (``knowledge_base`` collection) is external-world knowledge; episodic
memory is Genesis's own first-party content. These helpers produce the labels
that keep the two distinguishable wherever recalled content enters an LLM
context.
"""

from __future__ import annotations

from genesis.memory.provenance import (
    is_external,
    label_result_dicts,
    provenance_descriptor,
    short_source,
)


def test_is_external_knowledge_base():
    assert is_external("knowledge_base") is True


def test_is_external_episodic():
    assert is_external("episodic_memory") is False


def test_is_external_none_is_first_party():
    # Missing/unknown collection must NOT be treated as external — defaulting to
    # first-party is the conservative, non-alarming choice.
    assert is_external(None) is False
    assert is_external("") is False


def test_descriptor_first_party():
    assert (
        provenance_descriptor(collection="episodic_memory", source_pipeline="anything")
        == "first-party memory"
    )


def test_descriptor_external_names_the_source():
    d = provenance_descriptor(
        collection="knowledge_base", source_pipeline="curated",
    )
    assert d.startswith("external-world knowledge")
    assert "user-curated" in d


def test_descriptor_external_recon():
    d = provenance_descriptor(collection="knowledge_base", source_pipeline="recon")
    assert "external-world knowledge" in d
    assert "recon" in d


def test_descriptor_external_null_pipeline_safe_default():
    # A KB item with NO source_pipeline (the SQLite-NULL case) must still read
    # as external, just with a generic source — never crash, never first-party.
    d = provenance_descriptor(collection="knowledge_base", source_pipeline=None)
    assert d.startswith("external-world knowledge")


def test_descriptor_includes_source_doc_when_meaningful():
    d = provenance_descriptor(
        collection="knowledge_base",
        source_pipeline="knowledge_ingest_source",
        source_doc="fastapi-docs.pdf",
    )
    assert "fastapi-docs.pdf" in d


def test_descriptor_omits_placeholder_source_doc():
    d = provenance_descriptor(
        collection="knowledge_base",
        source_pipeline="knowledge_ingest",
        source_doc="manual",
    )
    assert "manual" not in d


def test_short_source_terse_tokens():
    # Proactive-hook budget: single, space-free tokens.
    assert short_source("curated") == "curated"
    assert short_source("recon") == "recon"
    assert short_source("knowledge_ingest_source") == "ingested"
    assert short_source(None) == "ext"
    assert " " not in short_source("extraction_job")


def test_more_specific_pipeline_wins():
    # 'knowledge_ingest' is a substring of 'knowledge_ingest_source'; both map
    # to the same label here, but the match must be deterministic, not error.
    assert short_source("knowledge_ingest_source") == "ingested"
    assert short_source("knowledge_ingest") == "ingested"


# ── label_result_dicts (post-CRAG MCP-return pass) ──────────────────────────


def test_label_result_dicts_episodic_default():
    dicts = [{"memory_id": "m1", "content": "x"}]
    label_result_dicts(dicts, default_collection="episodic_memory")
    assert dicts[0]["collection"] == "episodic_memory"
    assert dicts[0]["provenance"] == "first-party memory"


def test_label_result_dicts_knowledge_default():
    dicts = [{"unit_id": "u1", "content": "doc", "source_pipeline": "curated"}]
    label_result_dicts(dicts, default_collection="knowledge_base")
    assert dicts[0]["collection"] == "knowledge_base"
    assert dicts[0]["provenance"].startswith("external-world knowledge")
    assert "user-curated" in dicts[0]["provenance"]


def test_label_result_dicts_crag_web_is_external_web():
    dicts = [{"unit_id": "https://x", "content": "w", "origin": "web",
              "source_pipeline": "crag_web"}]
    label_result_dicts(dicts, default_collection="episodic_memory")
    assert dicts[0]["collection"] == "knowledge_base"
    assert "web" in dicts[0]["provenance"]


def test_label_result_dicts_reads_collection_from_payload():
    # CRAG augmented dicts may carry collection at top level OR in payload.
    dicts = [{"memory_id": "m", "content": "c", "payload": {"collection": "knowledge_base"}}]
    label_result_dicts(dicts, default_collection="episodic_memory")
    assert dicts[0]["collection"] == "knowledge_base"
    assert dicts[0]["provenance"].startswith("external-world knowledge")


def test_label_result_dicts_skips_sentinels():
    dicts = [{"not_found": ["a", "b"]}]
    label_result_dicts(dicts)
    assert dicts[0] == {"not_found": ["a", "b"]}  # untouched


def test_label_result_dicts_idempotent():
    dicts = [{"memory_id": "m", "content": "c", "collection": "knowledge_base",
              "source_pipeline": "recon"}]
    label_result_dicts(dicts)
    first = dicts[0]["provenance"]
    label_result_dicts(dicts)
    assert dicts[0]["provenance"] == first
