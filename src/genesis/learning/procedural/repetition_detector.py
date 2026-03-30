"""RepetitionDetector — proactive detection of repetitive workflows.

Scans recent observations for topic clusters that appear 3+ times but
have no corresponding procedure. Surfaces these as candidates for the
deep reflection cycle to consider formalizing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Common words to ignore when extracting topic keywords.
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "must", "to", "of",
    "in", "for", "on", "with", "at", "by", "from", "as", "into", "through",
    "during", "before", "after", "above", "below", "between", "out", "off",
    "over", "under", "again", "further", "then", "once", "here", "there",
    "when", "where", "why", "how", "all", "each", "every", "both", "few",
    "more", "most", "other", "some", "such", "no", "nor", "not", "only",
    "own", "same", "so", "than", "too", "very", "just", "because", "but",
    "and", "or", "if", "while", "that", "this", "it", "its", "i", "we",
    "they", "them", "their", "what", "which", "who", "whom", "these",
    "those", "am", "about", "up", "down", "also", "any",
})

# Minimum keyword overlap for two observations to be "similar".
_MIN_OVERLAP = 3

# Minimum cluster size before suggesting a procedure.
_MIN_CLUSTER_SIZE = 3


@dataclass(frozen=True)
class ProcedureCandidate:
    """A detected repetitive pattern that could become a procedure."""

    topic: str
    observation_ids: list[str] = field(default_factory=list)
    sample_contents: list[str] = field(default_factory=list)
    cluster_size: int = 0
    shared_keywords: list[str] = field(default_factory=list)


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords from observation content."""
    words = re.findall(r"[a-z_]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _compute_overlap(kw_a: set[str], kw_b: set[str]) -> set[str]:
    """Compute keyword overlap between two keyword sets."""
    return kw_a & kw_b


class RepetitionDetector:
    """Detects repetitive patterns in observations.

    Uses keyword overlap clustering: observations that share >= _MIN_OVERLAP
    keywords are grouped. Clusters of size >= _MIN_CLUSTER_SIZE that don't
    match an existing procedure's task_type become candidates.
    """

    def __init__(
        self,
        *,
        min_overlap: int = _MIN_OVERLAP,
        min_cluster_size: int = _MIN_CLUSTER_SIZE,
    ) -> None:
        self._min_overlap = min_overlap
        self._min_cluster_size = min_cluster_size

    def detect_candidates(
        self,
        observations: list[dict],
        existing_procedures: list[dict],
    ) -> list[ProcedureCandidate]:
        """Detect repetitive patterns not covered by existing procedures.

        Args:
            observations: Recent observations (dicts with 'id', 'content', 'type').
            existing_procedures: Active procedures (dicts with 'task_type',
                'context_tags', 'principle').

        Returns:
            List of ProcedureCandidate for patterns worth formalizing.
        """
        if len(observations) < self._min_cluster_size:
            return []

        # Extract keywords per observation.
        obs_keywords: list[tuple[dict, set[str]]] = [
            (obs, _extract_keywords(obs.get("content", "")))
            for obs in observations
        ]

        # Build adjacency: which observations are "similar"?
        n = len(obs_keywords)
        adjacency: dict[int, set[int]] = {i: set() for i in range(n)}
        overlap_cache: dict[tuple[int, int], set[str]] = {}

        for i in range(n):
            for j in range(i + 1, n):
                overlap = _compute_overlap(obs_keywords[i][1], obs_keywords[j][1])
                if len(overlap) >= self._min_overlap:
                    adjacency[i].add(j)
                    adjacency[j].add(i)
                    overlap_cache[(i, j)] = overlap

        # Greedy clustering: find connected components of similar observations.
        visited: set[int] = set()
        clusters: list[list[int]] = []

        for i in range(n):
            if i in visited:
                continue
            if not adjacency[i]:
                continue
            # BFS from this node.
            cluster = []
            queue = [i]
            while queue:
                node = queue.pop(0)
                if node in visited:
                    continue
                visited.add(node)
                cluster.append(node)
                for neighbor in adjacency[node]:
                    if neighbor not in visited:
                        queue.append(neighbor)
            clusters.append(cluster)

        # Filter to clusters that meet size threshold.
        large_clusters = [c for c in clusters if len(c) >= self._min_cluster_size]

        if not large_clusters:
            return []

        # Build procedure keyword set for dedup.
        proc_keywords: set[str] = set()
        for proc in existing_procedures:
            proc_keywords.update(_extract_keywords(proc.get("task_type", "")))
            proc_keywords.update(_extract_keywords(proc.get("principle", "")))
            for tag in proc.get("context_tags", []):
                if isinstance(tag, str):
                    proc_keywords.update(_extract_keywords(tag))

        # Convert clusters to candidates, deduping against existing procedures.
        candidates: list[ProcedureCandidate] = []

        for cluster_indices in large_clusters:
            # Find shared keywords across the cluster.
            cluster_kw_sets = [obs_keywords[i][1] for i in cluster_indices]
            shared = cluster_kw_sets[0]
            for kw_set in cluster_kw_sets[1:]:
                shared = shared & kw_set

            # Check if this cluster's topic overlaps significantly with
            # an existing procedure.
            if shared and len(shared & proc_keywords) >= len(shared) * 0.5:
                continue  # Already covered by a procedure.

            # Build candidate.
            obs_list = [obs_keywords[i][0] for i in cluster_indices]
            topic_keywords = sorted(shared)[:5]  # Top 5 shared keywords
            topic = " + ".join(topic_keywords) if topic_keywords else "unknown_pattern"

            candidates.append(ProcedureCandidate(
                topic=topic,
                observation_ids=[obs.get("id", "") for obs in obs_list],
                sample_contents=[obs.get("content", "")[:100] for obs in obs_list[:3]],
                cluster_size=len(cluster_indices),
                shared_keywords=topic_keywords,
            ))

        logger.info(
            "Repetition detection: %d observations → %d clusters → %d candidates",
            len(observations), len(large_clusters), len(candidates),
        )
        return candidates
