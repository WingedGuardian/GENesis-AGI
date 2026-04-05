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
