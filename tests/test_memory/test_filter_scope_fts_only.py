"""Unit tests for ``_filter_scope_fts_only`` — the wing/room/life_domain
scope filter for FTS5-only recall candidates.

FTS5-only candidates (no Qdrant vector) never appear in Qdrant's already
wing-filtered results, so this function verifies their membership against the
AUTHORITATIVE wing/room now projected onto the FTS row by ``search_ranked``
(from the joined ``memory_metadata``), rather than the denormalized FTS tag
token. This is the fix that makes fts5_only rows reachable by wing-filtered
recall (follow-up 0a3741c4).
"""

from __future__ import annotations

from genesis.memory.retrieval import _filter_scope_fts_only
from genesis.memory.taxonomy import classify_life_domain


def _fts(wing=None, room=None, tags=None):
    """A search_ranked-shaped FTS row carrying the projected wing/room/tags."""
    return {"wing": wing, "room": room, "tags": tags}


def test_no_filter_is_noop():
    candidates = ["a", "b", "c"]
    out = _filter_scope_fts_only(candidates, {}, {}, wing=None, room=None, life_domain=None)
    assert out == candidates


def test_qdrant_hit_always_kept_under_wing_filter():
    # A Qdrant hit was already wing-filtered at query time → always kept,
    # regardless of any FTS-side data.
    out = _filter_scope_fts_only(
        ["q1"],
        {"q1": {"id": "q1"}},
        {},
        wing="memory",
        room=None,
        life_domain=None,
    )
    assert out == ["q1"]


def test_fts_only_matching_wing_kept():
    out = _filter_scope_fts_only(
        ["m1"],
        {},
        {"m1": _fts(wing="routing")},
        wing="routing",
        room=None,
        life_domain=None,
    )
    assert out == ["m1"]


def test_fts_only_nonmatching_wing_dropped():
    out = _filter_scope_fts_only(
        ["m1"],
        {},
        {"m1": _fts(wing="memory")},
        wing="routing",
        room=None,
        life_domain=None,
    )
    assert out == []


def test_fts_only_null_wing_dropped_under_wing_filter():
    out = _filter_scope_fts_only(
        ["m1"],
        {},
        {"m1": _fts(wing=None)},
        wing="routing",
        room=None,
        life_domain=None,
    )
    assert out == []


def test_fts_only_missing_from_fts_map_dropped():
    # Candidate has no FTS row to verify against → excluded.
    out = _filter_scope_fts_only(["ghost"], {}, {}, wing="routing", room=None, life_domain=None)
    assert out == []


def test_room_filter_matches_and_drops():
    kept = _filter_scope_fts_only(
        ["m1"],
        {},
        {"m1": _fts(wing="routing", room="fallback")},
        wing="routing",
        room="fallback",
        life_domain=None,
    )
    assert kept == ["m1"]
    dropped = _filter_scope_fts_only(
        ["m1"],
        {},
        {"m1": _fts(wing="routing", room="other")},
        wing="routing",
        room="fallback",
        life_domain=None,
    )
    assert dropped == []


def test_life_domain_derived_from_authoritative_wing():
    # life_domain has no metadata column; it is derived from the wing. Use the
    # same mapping the write path uses so the test tracks the real derivation.
    wing = "employment"
    ld = classify_life_domain(wing)
    kept = _filter_scope_fts_only(
        ["m1"],
        {},
        {"m1": _fts(wing=wing)},
        wing=None,
        room=None,
        life_domain=ld,
    )
    assert kept == ["m1"]

    # A different life_domain than the wing derives → dropped.
    other = next(d for d in ("personal", "employment", "genesis") if d != ld)
    dropped = _filter_scope_fts_only(
        ["m1"],
        {},
        {"m1": _fts(wing=wing)},
        wing=None,
        room=None,
        life_domain=other,
    )
    assert dropped == []


def test_life_domain_explicit_tag_overrides_wing():
    # An explicit life_domain: tag (set at write time, carried in the FTS tags
    # string) must win over the wing-inferred domain — matching the Qdrant path
    # which filters on the stored payload value.
    wing = "employment"
    inferred = classify_life_domain(wing)
    override = next(d for d in ("personal", "employment", "genesis") if d != inferred)
    fhit = _fts(wing=wing, tags=f"some_tag wing:{wing} life_domain:{override}")
    # Reachable under its EXPLICIT domain...
    assert _filter_scope_fts_only(
        ["m1"], {}, {"m1": fhit}, wing=None, room=None, life_domain=override
    ) == ["m1"]
    # ...and NOT under the wing-inferred one.
    assert (
        _filter_scope_fts_only(["m1"], {}, {"m1": fhit}, wing=None, room=None, life_domain=inferred)
        == []
    )


def test_order_preserved_across_mixed_candidates():
    out = _filter_scope_fts_only(
        ["q1", "drop", "m1"],
        {"q1": {"id": "q1"}},
        {
            "drop": _fts(wing="memory"),
            "m1": _fts(wing="routing"),
        },
        wing="routing",
        room=None,
        life_domain=None,
    )
    # q1 kept (qdrant), drop excluded (wing mismatch), m1 kept — order intact.
    assert out == ["q1", "m1"]
