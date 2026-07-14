from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    content: str
    source: str
    memory_type: str
    tags: tuple[str, ...]
    confidence: float
    created_at: str
    retrieved_count: int
    link_count: int
    memory_class: str = "fact"


@dataclass(frozen=True)
class ActivationScore:
    memory_id: str
    base_score: float
    recency_factor: float
    access_frequency: float
    connectivity_factor: float
    final_score: float


@dataclass(frozen=True)
class RetrievalResult:
    memory_id: str
    content: str
    source: str
    memory_type: str
    score: float
    vector_rank: int | None
    fts_rank: int | None
    activation_score: float
    payload: dict
    # Provenance — trace memory back to source conversation
    source_session_id: str | None = None
    transcript_path: str | None = None
    source_line_range: tuple[int, int] | None = None
    source_pipeline: str | None = None
    # Memory classification (rule/fact/reference)
    memory_class: str = "fact"
    # Intent routing — V4 groundwork
    query_intent: str | None = None
    intent_confidence: float = 0.0
    # Provenance discriminator (audit D12): the Qdrant collection this result was
    # retrieved from. ``knowledge_base`` == external-world knowledge; anything
    # else == first-party memory. Authoritative (always known at retrieval),
    # unlike the per-item ``source`` string. Defaults first-party so an unset
    # value is never mislabeled external. Defaulted fields are appended (never
    # inserted) for positional-construction safety. Use
    # ``genesis.memory.provenance`` to turn it into a label.
    collection: str = "episodic_memory"
    # The pre-diversity-penalty fused score (post-rerank, post-graph-
    # boost). ``score`` is the FINAL ordering score — the echo-cluster penalty
    # is applied to it so near-duplicate results rank lower. J-9 quality
    # logging reads THIS field instead, so a dedup artifact (score * 0.5)
    # never masquerades as retrieval quality. 0.0 == not populated (paths
    # other than HybridRetriever.recall don't compute it).
    retrieval_score: float = 0.0
    # WS-3 stored provenance (migration 0054): the origin_class stamped at
    # store time (owner / first_party / external_untrusted). None == the
    # retrieval path couldn't recover it (pre-0054 row or a surface that
    # doesn't read it) — consumers fall back to the (collection,
    # source_pipeline) re-derivation in security.immunity_shadow.
    origin_class: str | None = None


@dataclass(frozen=True)
class LinkRecord:
    source_id: str
    target_id: str
    link_type: str
    strength: float
    created_at: str


@dataclass(frozen=True)
class UserModelSnapshot:
    model: dict
    version: int
    evidence_count: int
    synthesized_at: str
