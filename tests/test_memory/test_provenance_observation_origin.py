"""WS-3 observation-table origin classifier (fail-closed, Option 1).

The ``observations`` table's ``source`` is uncurated, so the classifier defaults
an unknown source to ``None`` (fail-closed) — NOT ``first_party`` like the
curated-pipeline :func:`derive_origin_class`. Known-external is authoritative
(reuses the pipeline registry); the first-party allowlist is a cosmetic
convenience whose misses only exclude, never leak.
"""

from __future__ import annotations

import pytest

from genesis.memory.provenance import (
    ORIGIN_EXTERNAL_UNTRUSTED,
    ORIGIN_FIRST_PARTY,
    ORIGIN_OWNER,
    _origin_from_source,
    derive_observation_origin,
)

# ── _origin_from_source: pure source-string classification ──────────────────


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # known external (content pulled off the world)
        ("recon", ORIGIN_EXTERNAL_UNTRUSTED),
        ("email_recon", ORIGIN_EXTERNAL_UNTRUSTED),
        # known first-party Genesis operational/cognitive writers
        ("awareness_loop", ORIGIN_FIRST_PARTY),
        ("reflection", ORIGIN_FIRST_PARTY),
        ("deep_reflection", ORIGIN_FIRST_PARTY),
        ("cc_reflection_light", ORIGIN_FIRST_PARTY),
        ("entity_adjudication", ORIGIN_FIRST_PARTY),  # finding #3: Genesis JSON digest
        ("auto_memory_harvest", ORIGIN_FIRST_PARTY),
        ("genesis_version", ORIGIN_FIRST_PARTY),
        ("outreach_recovery", ORIGIN_FIRST_PARTY),
        # intake: routed through the authoritative _pipeline_for_source split
        ("intake:anticipatory_research", ORIGIN_FIRST_PARTY),  # finding #2: Genesis-authored
        ("intake:user_directed", ORIGIN_FIRST_PARTY),
        ("intake:background_task", ORIGIN_FIRST_PARTY),
        ("intake:model_intelligence", ORIGIN_EXTERNAL_UNTRUSTED),
        ("intake:github_landscape", ORIGIN_EXTERNAL_UNTRUSTED),
        ("intake:web_monitoring", ORIGIN_EXTERNAL_UNTRUSTED),
        ("intake:free_model_inventory", ORIGIN_EXTERNAL_UNTRUSTED),  # value≠pipeline
        ("intake:email_recon", ORIGIN_EXTERNAL_UNTRUSTED),
        # fail-closed: unknown / structural-but-unresolved sources → None
        ("intake:not_a_real_source", None),
        ("module:automaton_supervisor", None),
        ("session:bf158c31-5622-4bad-a24b-c0f317d7e67c", None),
        ("conversation_intent", None),
        ("some_brand_new_writer", None),
        ("", None),
        (None, None),
    ],
)
def test_origin_from_source(source, expected):
    assert _origin_from_source(source) == expected


# ── derive_observation_origin: precedence ───────────────────────────────────


def test_explicit_origin_wins(monkeypatch):
    # explicit beats both env and source
    monkeypatch.setenv("GENESIS_SESSION_ORIGIN", "external_untrusted")
    assert derive_observation_origin(origin_class=ORIGIN_OWNER, source="recon") == ORIGIN_OWNER


def test_invalid_explicit_origin_raises(monkeypatch):
    monkeypatch.delenv("GENESIS_SESSION_ORIGIN", raising=False)
    with pytest.raises(ValueError, match="invalid origin_class"):
        derive_observation_origin(origin_class="external", source="recon")  # short form


def test_env_beats_source_forge_proof(monkeypatch):
    # An external judge session writing with a forged INTERNAL source must still
    # be classified external — env (which it can't forge) outranks source.
    monkeypatch.setenv("GENESIS_SESSION_ORIGIN", "external_untrusted")
    assert (
        derive_observation_origin(origin_class=None, source="awareness_loop")
        == ORIGIN_EXTERNAL_UNTRUSTED
    )


def test_source_used_when_no_env(monkeypatch):
    monkeypatch.delenv("GENESIS_SESSION_ORIGIN", raising=False)
    assert derive_observation_origin(source="recon") == ORIGIN_EXTERNAL_UNTRUSTED
    assert derive_observation_origin(source="awareness_loop") == ORIGIN_FIRST_PARTY


def test_unknown_source_fails_closed_to_none(monkeypatch):
    monkeypatch.delenv("GENESIS_SESSION_ORIGIN", raising=False)
    assert derive_observation_origin(source="brand_new_writer") is None
    assert derive_observation_origin(source="session:abc") is None
