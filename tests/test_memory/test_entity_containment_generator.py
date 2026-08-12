"""MW-3 PR-2 — containment candidate generator in the reconcile sweep.

difflib >= 0.85 is mathematically blind to the fragmentation class that matters:
a bare identifier vs the same identifier plus a qualifier word ("foo" vs
"foo server"), and a short form vs its fully-qualified/dotted form ("113.7" vs
"203.0.113.7"). All ratios sit ~0.4-0.6. This adds token-set CONTAINMENT
blocking (shorter name's tokens subset of the longer's, via a rare-token index)
plus a dotted-numeric suffix rule, unioned with the existing difflib pass.

These are PURE-CPU pair-DISCOVERY tests (no LLM, no DB) — the judgment of whether
a nominated pair actually merges is the adjudicator's job, measured live at F3.
NO private strings — generic fixtures only.
"""

from __future__ import annotations

from genesis.memory import entity_adjudication as adj


def _ent(norm, eid, etype="concept"):
    return (norm, eid, etype)


def _pairs(slice_entities, candidates, *, seen=None, cap=100):
    """Run the pure generator with cluster candidates; return canonical pair keys."""
    group_candidates = {"cluster": candidates, "person": [], "org": []}
    out = adj._compute_sweep_pairs(slice_entities, group_candidates, set(seen or ()), cap)
    return {frozenset(p) for p in out}


def test_containment_catches_qualifier_variant_difflib_misses():
    # "foo" vs "foo server": difflib ratio ~0.46 (< 0.85), but tokens {foo} ⊂
    # {foo, server} → containment nominates.
    a = _ent("foo", "e1")
    b = _ent("foo server", "e2")
    import difflib

    assert difflib.SequenceMatcher(None, "foo", "foo server").ratio() < 0.85
    got = _pairs([a, b], [a, b])
    assert frozenset({"e1", "e2"}) in got


def test_dotted_suffix_catches_short_vs_fully_qualified():
    # "113.7" vs "203.0.113.7" (RFC5737 doc range): no shared token, difflib low;
    # the dotted-suffix rule (203.0.113.7 endswith .113.7) nominates.
    a = _ent("113.7", "e1")
    b = _ent("203.0.113.7", "e2")
    got = _pairs([a, b], [a, b])
    assert frozenset({"e1", "e2"}) in got


def test_common_word_without_containment_not_nominated():
    # "alpha service" vs "beta service" share "service" but neither token set is a
    # subset of the other → NOT a containment pair, and difflib ratio is low.
    a = _ent("alpha service", "e1")
    b = _ent("beta service", "e2")
    got = _pairs([a, b], [a, b])
    assert frozenset({"e1", "e2"}) not in got


def test_digit_series_still_excluded():
    # Numbered siblings are definitionally distinct — the digit-guard must still
    # veto them even though "widget 1" ⊂-ish "widget 12" token overlap exists.
    a = _ent("widget 1", "e1")
    b = _ent("widget 2", "e2")
    got = _pairs([a, b], [a, b])
    assert frozenset({"e1", "e2"}) not in got


def test_difflib_cosmetic_pair_still_nominated():
    # The existing difflib pass must survive: a hyphenation variant (ratio >= 0.85)
    # is still nominated even though its tokens differ.
    a = _ent("neural-monitor", "e1")
    b = _ent("neural monitor", "e2")
    got = _pairs([a, b], [a, b])
    assert frozenset({"e1", "e2"}) in got


def test_seen_pairs_skipped():
    a = _ent("foo", "e1")
    b = _ent("foo server", "e2")
    key = "e1|e2"  # min|max
    got = _pairs([a, b], [a, b], seen={key})
    assert frozenset({"e1", "e2"}) not in got


def test_cap_bounds_output():
    # Three mutually-containment-related entities → 3 pairs possible; cap=1 → 1.
    a = _ent("svc", "e1")
    b = _ent("svc alpha", "e2")
    c = _ent("svc alpha beta", "e3")
    group_candidates = {"cluster": [a, b, c], "person": [], "org": []}
    out = adj._compute_sweep_pairs([a, b, c], group_candidates, set(), 1)
    assert len(out) == 1


def test_rare_token_df_cap_limits_common_token_pairs():
    # A token shared by MANY entities (> DF cap) must not become a pair generator:
    # 'node' appears in 12 entities → not a rare token → 'node' ⊂ 'node primary'
    # is nominated ONLY if they share ANOTHER rare token, which they don't here.
    common = [_ent(f"node role{i}", f"c{i}") for i in range(12)]
    a = _ent("node", "e1")
    b = _ent("node primary", "e2")
    cands = [*common, a, b]
    got = _pairs([a, b], cands)
    # 'node' is over-DF-cap → 'node'/'node primary' not nominated by containment.
    assert frozenset({"e1", "e2"}) not in got


def test_dotted_core_token_exempt_from_df_cap():
    # A heavily-fragmented address token appearing in > _DF_CAP shards must STILL
    # connect them (dotted tokens are exempt from the word DF cap) — the exact
    # fragmentation this generator heals. 12 shards share the address "10.20.30".
    shards = [_ent(f"10.20.30 role{i}", f"s{i}") for i in range(12)]
    # The bare address entity must be nominated against a shard despite DF=13.
    bare = _ent("10.20.30", "bare")
    cands = [*shards, bare]
    got = _pairs([bare], cands)
    assert any("bare" in p for p in got)  # not dropped by the cap


def test_unicode_identifier_containment():
    # Non-Latin names must TOKENIZE (the ASCII-only class dropped them entirely).
    # A >=3-char CJK bare name vs the same + a Latin qualifier — the bare token is
    # now produced and indexed (the separate len<3 floor is an accepted tradeoff,
    # applying equally to ASCII "ha"/"db", so use a 3-char name here).
    a = _ent("東京都", "e1")
    b = _ent("東京都 server", "e2")
    got = _pairs([a, b], [a, b])
    assert frozenset({"e1", "e2"}) in got
    # The bare non-Latin name tokenizes at all (the core of the P2 fix).
    assert adj._tokens("東京") == frozenset({"東京"})
    # underscore still splits (ASCII behaviour preserved)
    assert adj._tokens("dream_cycle") == frozenset({"dream", "cycle"})


def test_cross_type_same_norm_nominated():
    # Two DIFFERENT entities sharing a norm_name across types (UNIQUE is on
    # norm_name+entity_type) are a legitimate merge candidate recoverable only in
    # the sweep — must be nominated (difflib ratio 1.0 and identical token set).
    a = ("acme", "e1", "concept")
    b = ("acme", "e2", "product")  # same cluster group, same norm, different type
    got = _pairs([a, b], [a, b])
    assert frozenset({"e1", "e2"}) in got


def test_determinism_stable_pair_order():
    a = _ent("svc", "e1")
    b = _ent("svc alpha", "e2")
    c = _ent("svc beta", "e3")
    group_candidates = {"cluster": [a, b, c], "person": [], "org": []}
    out1 = adj._compute_sweep_pairs([a, b, c], group_candidates, set(), 100)
    out2 = adj._compute_sweep_pairs([a, b, c], group_candidates, set(), 100)
    assert out1 == out2
