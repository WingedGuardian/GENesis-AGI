"""WS-3 ``derive_origin_class`` — full store-time derivation matrix.

Precedence under test (each rule short-circuits; see
``genesis.memory.provenance.derive_origin_class``):
  1. explicit ``origin_class`` override (validated, ValueError on garbage)
  2. pipeline in the external set → external_untrusted
  3. pipeline in the first-party set → first_party
  4. any ``source_subsystem`` → first_party
  5. ``collection == 'knowledge_base'`` → external_untrusted
  6. default → first_party
"""

from __future__ import annotations

import pytest

from genesis.memory.provenance import (
    ORIGIN_CLASSES,
    ORIGIN_EXTERNAL_UNTRUSTED,
    ORIGIN_FIRST_PARTY,
    ORIGIN_OWNER,
    derive_origin_class,
)

# Every literal external pipeline value — including the reserved ones
# (email/inbox/web_search/web_fetch have no store() writers today; a future
# writer must be external BY DEFAULT).
EXTERNAL_PIPELINES = (
    "crag_web",
    "recon",
    "knowledge_ingest",
    "knowledge_ingest_source",
    "curated",
    "email",
    "inbox",
    "web_search",
    "web_fetch",
)

# Every literal first-party pipeline value.
FIRST_PARTY_PIPELINES = (
    "conversation",
    "session_observer",
    "harvest",
    "synthesis",
    "event_calendar",
    "dream_cycle",
    "reflection",
    "drift",
    "extraction_job",
    "surplus",
    "reference_store",
)


@pytest.mark.parametrize("pipeline", EXTERNAL_PIPELINES)
def test_external_pipelines_map_external(pipeline):
    assert (
        derive_origin_class(source_pipeline=pipeline)
        == ORIGIN_EXTERNAL_UNTRUSTED
    )


@pytest.mark.parametrize("pipeline", FIRST_PARTY_PIPELINES)
def test_first_party_pipelines_map_first_party(pipeline):
    assert derive_origin_class(source_pipeline=pipeline) == ORIGIN_FIRST_PARTY


@pytest.mark.parametrize("pipeline", FIRST_PARTY_PIPELINES)
def test_first_party_pipelines_win_over_kb_collection(pipeline):
    """Rule 3 outranks rule 5: a first-party pipeline writing INTO the KB
    (e.g. surplus insights, saved references) stays first_party."""
    assert (
        derive_origin_class(source_pipeline=pipeline, collection="knowledge_base")
        == ORIGIN_FIRST_PARTY
    )


def test_explicit_override_wins_over_contradicting_pipeline():
    # 'recon' would derive external; the validated explicit value wins outright.
    assert (
        derive_origin_class(origin_class=ORIGIN_OWNER, source_pipeline="recon")
        == ORIGIN_OWNER
    )
    assert (
        derive_origin_class(
            origin_class=ORIGIN_EXTERNAL_UNTRUSTED, source_pipeline="conversation"
        )
        == ORIGIN_EXTERNAL_UNTRUSTED
    )


@pytest.mark.parametrize("value", ["", "garbage", "OWNER", "first-party"])
def test_invalid_explicit_override_raises(value):
    assert value not in ORIGIN_CLASSES  # test-data sanity
    with pytest.raises(ValueError, match="invalid origin_class"):
        derive_origin_class(origin_class=value)


def test_external_pipeline_outranks_source_subsystem():
    """Rule-order: recon stores web-collected signals WITH
    source_subsystem='triage' — the content is still external."""
    assert (
        derive_origin_class(source_pipeline="recon", source_subsystem="triage")
        == ORIGIN_EXTERNAL_UNTRUSTED
    )


def test_source_subsystem_only_is_first_party():
    assert (
        derive_origin_class(source_subsystem="ego") == ORIGIN_FIRST_PARTY
    )


def test_unknown_pipeline_kb_collection_is_external():
    assert (
        derive_origin_class(
            source_pipeline="some_new_pipeline", collection="knowledge_base"
        )
        == ORIGIN_EXTERNAL_UNTRUSTED
    )


def test_unknown_pipeline_episodic_collection_is_first_party():
    assert (
        derive_origin_class(
            source_pipeline="some_new_pipeline", collection="episodic_memory"
        )
        == ORIGIN_FIRST_PARTY
    )


def test_all_none_defaults_first_party():
    """Conservative store-time default — fail-closed normalization to
    external_untrusted happens only at GATE time, never here."""
    assert derive_origin_class() == ORIGIN_FIRST_PARTY
