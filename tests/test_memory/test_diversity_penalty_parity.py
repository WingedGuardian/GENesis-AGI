"""Parity + behavior tests for the ``_apply_diversity_penalty`` pre-tokenize
refactor.

The refactor hoisted stopword term-extraction OUT of the O(n²) Jaccard
comparison loop — the original re-tokenized each candidate's content ~n times,
which measured 100-550ms of synchronous, event-loop-blocking CPU on large
candidate pools (the dominant cost of recall, and the cause of the proactive
endpoint 503-ing under concurrency). The refactor must be *exact-parity*: same
term sets → same Jaccard → same cluster penalties.

To guarantee that, ``_reference_penalty`` below is the verbatim PRE-refactor
algorithm (calling ``compute_jaccard`` per pair). The randomized test asserts
the live function and the oracle produce identical surviving-id lists AND
identical in-place ``fused`` mutation, so a future change to the fast path can
never silently alter retrieval results.
"""

from __future__ import annotations

import random

from genesis.memory.retrieval import _apply_diversity_penalty, _get_candidate_content
from genesis.memory.source_verification import compute_jaccard


def _reference_penalty(
    candidates: list[str],
    fused: dict[str, float],
    qdrant_by_id: dict[str, dict],
    fts_by_id: dict[str, dict],
    *,
    jaccard_threshold: float = 0.80,
    penalty: float = 0.5,
    max_per_cluster: int = 3,
) -> list[str]:
    """Verbatim pre-refactor algorithm (compute_jaccard per pair) — the oracle."""
    if len(candidates) < 2:
        return candidates
    sorted_cands = sorted(candidates, key=lambda m: fused.get(m, 0.0), reverse=True)
    cluster_of: dict[str, int] = {}
    cluster_count: dict[int, int] = {}
    next_cluster = 0
    content_cache = {
        mid: _get_candidate_content(mid, qdrant_by_id, fts_by_id) for mid in sorted_cands
    }
    for i, mid in enumerate(sorted_cands):
        content_i = content_cache[mid]
        if not content_i:
            continue
        matched_cluster = None
        for j in range(i):
            earlier = sorted_cands[j]
            content_j = content_cache.get(earlier, "")
            if not content_j:
                continue
            if compute_jaccard(content_i, content_j) >= jaccard_threshold:
                matched_cluster = cluster_of.get(earlier)
                break
        if matched_cluster is not None:
            cluster_of[mid] = matched_cluster
            cluster_count[matched_cluster] = cluster_count.get(matched_cluster, 0) + 1
            if cluster_count[matched_cluster] > max_per_cluster:
                fused[mid] = 0.0
            else:
                fused[mid] *= penalty
        else:
            cluster_of[mid] = next_cluster
            cluster_count[next_cluster] = 1
            next_cluster += 1
    return [mid for mid in candidates if fused.get(mid, 0.0) > 0.0]


# Fragments: near-duplicate echoes, unique content, all-stopword, and empty —
# exercise every branch (clustering, empty-terms → 0.0, empty-content skip).
_FRAGMENTS = [
    "server crashed because memory limit was exceeded during recall pipeline",
    "server crashed because the memory limit was exceeded during recall pipeline",  # echo
    "server crashed as memory limits were exceeded when the recall pipeline ran",
    "voice pipeline swapped ambient speech to text with the sensevoice model",
    "graph expansion adds one hop neighbors to the delivered memory set today",
    "reranker uses a cross encoder to rescore fused candidates by relevance",
    "the of and to a in is on at",  # all stopwords → empty term set
    "",  # empty content → skipped
    "diversity penalty collapses echo clusters in retrieval result output sets",
]


def _build_case(rng: random.Random, n: int):
    ids: list[str] = []
    qdrant_by_id: dict[str, dict] = {}
    fts_by_id: dict[str, dict] = {}
    fused: dict[str, float] = {}
    for k in range(n):
        mid = f"m{k}"
        ids.append(mid)
        frag = rng.choice(_FRAGMENTS)
        # Split across Qdrant/FTS sources to exercise both content-lookup paths.
        if rng.random() < 0.6:
            qdrant_by_id[mid] = {"payload": {"content": frag}}
        else:
            fts_by_id[mid] = {"content": frag}
        fused[mid] = round(rng.uniform(0.001, 0.05), 6)
    return ids, qdrant_by_id, fts_by_id, fused


def test_diversity_penalty_matches_reference_randomized():
    rng = random.Random(1234)
    for _ in range(200):
        n = rng.randint(2, 40)
        ids, q, f, fused = _build_case(rng, n)
        fused_new = dict(fused)
        fused_ref = dict(fused)
        out_new = _apply_diversity_penalty(list(ids), fused_new, q, f)
        out_ref = _reference_penalty(list(ids), fused_ref, q, f)
        assert out_new == out_ref
        assert fused_new == fused_ref  # in-place mutation identical, not just survivors


def test_diversity_penalty_echo_cluster_penalized():
    # a and b share 10/11 terms → Jaccard ≥ 0.80 regardless of stopword set.
    q = {
        "a": {
            "payload": {
                "content": "server crashed memory limit exceeded recall pipeline blocked event loop hard"
            }
        },
        "b": {
            "payload": {
                "content": "server crashed memory limit exceeded recall pipeline blocked event loop today"
            }
        },
        "c": {
            "payload": {"content": "voice pipeline swapped ambient speech to text with sensevoice"}
        },
    }
    fused = {"a": 0.05, "b": 0.03, "c": 0.02}
    out = _apply_diversity_penalty(["a", "b", "c"], fused, q, {})
    assert fused["a"] == 0.05  # highest-scored member of the echo cluster untouched
    assert fused["b"] == 0.015  # echo penalized ×0.5
    assert fused["c"] == 0.02  # unique candidate untouched
    assert set(out) == {"a", "b", "c"}


def test_diversity_penalty_short_circuit():
    assert _apply_diversity_penalty([], {}, {}, {}) == []
    assert _apply_diversity_penalty(["x"], {"x": 1.0}, {}, {}) == ["x"]


def test_diversity_penalty_over_max_cluster_dropped():
    # 5 identical echoes, max_per_cluster=3: the 4th+ are zeroed and dropped.
    same = "server crashed memory limit exceeded recall pipeline blocked event loop hard"
    q = {mid: {"payload": {"content": same}} for mid in ("a", "b", "c", "d", "e")}
    fused = {"a": 0.05, "b": 0.04, "c": 0.03, "d": 0.02, "e": 0.01}
    out = _apply_diversity_penalty(["a", "b", "c", "d", "e"], fused, q, {})
    # a (leader) kept full; b,c penalized (cluster members 2,3); d,e dropped (>3).
    assert fused["a"] == 0.05
    assert fused["d"] == 0.0
    assert fused["e"] == 0.0
    assert "d" not in out and "e" not in out
