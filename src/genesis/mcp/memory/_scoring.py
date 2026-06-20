"""Relative score-floor utilities for the MCP recall surface (audit MEM-004).

Knowledge-base (external-world) results returned alongside first-party episodic
memory carry scores on a scale that shifts with the retrieval mode: positional
``1/(1+rank)`` when the cross-encoder reranker is on, RRF / FTS-mapped values
otherwise. A *fixed absolute* floor therefore drops too much or too little KB
depending on the mode — the defect behind MEM-001's sibling finding MEM-004,
where a 0.15 floor could wipe most or all KB results once their scores landed on
the wrong scale.

The fix is a floor *relative to the strongest KB hit in the same result set*,
which is scale-invariant: multiply every score by a constant and the survivor
set is unchanged. The single best KB result always survives; a proportional
tail is kept; episodic results are never touched.
"""

from __future__ import annotations

from collections.abc import Callable

#: Default KB floor ratio: keep knowledge-base hits scoring at least 20% of the
#: strongest KB hit in the result set. Shared by ``memory_recall`` (source=both)
#: and ``knowledge_recall`` so the two surfaces stay consistent.
DEFAULT_KB_FLOOR_RATIO = 0.2


def relative_kb_floor[T](
    results: list[T],
    *,
    ratio: float,
    score_of: Callable[[T], float],
    is_kb: Callable[[T], bool],
) -> list[T]:
    """Drop weak knowledge-base results relative to the strongest KB hit.

    Keeps every non-KB result untouched and every KB result scoring at least
    ``ratio`` × the top KB score in ``results``. Input order is preserved (this
    filters, it never re-sorts). Duck-typed via ``score_of`` / ``is_kb`` so it
    works on both ``RetrievalResult`` objects (``r.score`` / ``is_external(
    r.collection)``) and plain result dicts (``r["score"]``).

    No-op (returns ``results`` unchanged) when:
      * ``ratio <= 0`` — the floor is disabled;
      * there are no KB results — nothing to floor;
      * the top KB score is non-positive — no meaningful threshold.
    """
    if ratio <= 0:
        return results
    kb_scores = [score_of(r) for r in results if is_kb(r)]
    if not kb_scores:
        return results
    top = max(kb_scores)
    if top <= 0:
        return results
    floor = top * ratio
    return [r for r in results if not is_kb(r) or score_of(r) >= floor]
