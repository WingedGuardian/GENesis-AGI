"""Selective corrective retrieval (W-CRAG v1).

Implements a CRAG-style grade-and-correct loop on top of the existing
hybrid recall pipeline. The entry point ``maybe_correct_recall`` takes the
results an MCP recall tool already produced, grades the top few against a
calibrated relevance rubric, and — only when the recall looks genuinely
*Incorrect* — performs one conservative round of corrective augmentation
(relaxed re-retrieval, raw KB, and for knowledge queries a web fallback).

Design constraints (all LOCKED — see the module spec):

- **Best-effort.** Every public path is wrapped so that any failure or
  timeout returns the ORIGINAL ``results`` unchanged. Corrective retrieval
  must never degrade a recall that already worked.
- **Conservative rollout.** ``Ambiguous`` verdicts take NO action (DARK
  mode — log only). Only an unambiguous ``Incorrect`` triggers
  augmentation, and only one round.
- **Latency-aware.** A confident top score skips grading entirely; grading
  is parallel, capped, and per-call timeout-bounded.
- **No new dependencies.** Reranking reuses ``VoyageReranker`` (which
  degrades to ``[]`` on failure) rather than any hand-rolled similarity.

Calibration data (grades + the action that was/would-have-been taken) is
logged for every graded recall via ``emit_recall_corrected`` so the dark
``Ambiguous`` branch still produces training signal.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (module-level constants per spec — adjust here, not inline)
# ---------------------------------------------------------------------------

# Latency pre-gate: if the top result already scores at/above this, treat the
# recall as Correct and skip grading entirely. Avoids spending grader calls on
# recalls that are obviously fine.
_SKIP_GRADE_ABOVE = 0.75

# How many top results to grade. Grading the whole list is wasteful; the
# bucket decision only needs the strongest few candidates.
_GRADE_TOP_N = 4

# Relevance bar from the runtime rubric (memory_recall_grounding_runtime,
# pass_threshold=0.6). At/above this an item is "relevant".
_RELEVANT = 0.6

# Below this the best item is not even tangentially on-topic → Incorrect.
_INCORRECT_MAX = 0.3

# Per-grade call timeout. Grading is a single short LLM judge call; if a
# provider hangs we drop that item's grade rather than block the recall.
_GRADE_TIMEOUT_S = 8.0

# Max concurrent grader calls. Keeps load on the router bounded while still
# grading the top-N in parallel.
_GRADE_CONCURRENCY = 3

# Web fallback is slow (search + fetch + rerank); give it a generous bound
# and skip silently on timeout.
_WEB_TIMEOUT_S = 25.0

# Re-retrieval / output sizing.
_ORIG_LIMIT = 10  # assumed original recall limit (MCP default)
_RELAXED_LIMIT = _ORIG_LIMIT * 2  # relaxed re-retrieve fetches a wider net
_KB_LIMIT = 10  # raw knowledge-base re-retrieve limit

# Web refinement bounds.
_WEB_MAX_URLS = 2  # fetch at most this many search-result URLs
_WEB_TOP_CHUNKS = 3  # keep this many reranked paragraphs per snippet
_WEB_SNIPPET_MAX_CHARS = 1500  # recomposed snippet cap


# ---------------------------------------------------------------------------
# Result-shape normalization
# ---------------------------------------------------------------------------


def _norm(r: dict) -> dict:
    """Normalize a recall result dict to ``{"id", "content", "score"}``.

    ``memory_recall`` results carry ``memory_id``/``content``/``score`` (and a
    ``payload``); ``knowledge_recall`` results carry
    ``unit_id``/``content``/``score`` (and an ``origin``). We grade against the
    normalized view but always return the ORIGINAL dicts, so this is a read-only
    projection.
    """
    rid = r.get("memory_id") or r.get("unit_id") or r.get("id") or ""
    content = r.get("content", "") or ""
    try:
        score = float(r.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return {"id": str(rid), "content": content, "score": score}


def _result_id(r: dict) -> str:
    """Best-effort stable identity for dedup across original + augmented dicts."""
    return _norm(r)["id"]


# ---------------------------------------------------------------------------
# Tolerant grade parse (mirrors LLMJudgeScorer.score_async, scorers.py ~330-390)
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> str:
    """Extract a JSON object from text that may have fences or prose.

    Replicated from ``genesis.eval.scorers._extract_json`` to avoid importing a
    private helper across module boundaries.
    """
    text = (text or "").strip()
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                json.loads(candidate)
                return candidate
            except (json.JSONDecodeError, ValueError):
                pass
    return text


def _parse_grade(raw: str | None) -> float | None:
    """Parse ``{"score": float, "rationale": str}`` tolerantly.

    Mirrors the scorers.py judge parse: strip fences, ``json.loads``, clamp to
    [0, 1], reject non-finite. A grade that fails to parse returns ``None``
    (unknown), NEVER 0.0 — an unparseable grade is missing data, not a verdict
    of "irrelevant".
    """
    extracted = _extract_json(raw or "")
    try:
        parsed = json.loads(extracted)
        score_val = float(parsed.get("score", None))  # type: ignore[arg-type]
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return None
    if not math.isfinite(score_val):
        return None
    return max(0.0, min(1.0, score_val))


# ---------------------------------------------------------------------------
# Router resolution (mirrors knowledge.py _get_orchestrator ~632)
# ---------------------------------------------------------------------------


def _resolve_router() -> Any | None:
    """Fetch the live router, bootstrapping standalone if needed.

    Returns ``None`` if the router cannot be obtained — the caller then skips
    grading and returns the original results.
    """
    try:
        from genesis.runtime._core import GenesisRuntime

        rt = GenesisRuntime.instance()
        if rt._router is None:
            from genesis.routing.standalone import create_standalone_router

            create_standalone_router()
        return rt._router
    except Exception:
        logger.debug("CRAG: router resolution failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


async def _grade_one(
    router: Any,
    semaphore: asyncio.Semaphore,
    query: str,
    content: str,
    prompt_template: str,
) -> float | None:
    """Grade a single (query, content) pair. Returns score or None on failure.

    Each call is concurrency-capped and timeout-bounded. A grader call that
    fails with ``result.success == False`` (provider down / degradation-skipped)
    returns ``None`` — that is NOT an Incorrect verdict, it is missing data.
    """
    prompt = prompt_template.format(query=query, actual=content)
    async with semaphore:
        try:
            result = await asyncio.wait_for(
                router.route_call(
                    call_site_id="crag_grade",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                ),
                timeout=_GRADE_TIMEOUT_S,
            )
        except TimeoutError:
            logger.debug("CRAG: grade timed out")
            return None
        except Exception:
            logger.debug("CRAG: grade call raised", exc_info=True)
            return None

    if not getattr(result, "success", False):
        # Grader unavailable / degradation-skipped — unknown, not Incorrect.
        return None
    return _parse_grade(getattr(result, "content", None))


async def _grade_results(
    router: Any,
    query: str,
    normalized: list[dict],
) -> list[float | None]:
    """Grade the top-N normalized results in parallel. Returns aligned grades.

    Returns a list the same length as the graded slice (top-N). Each entry is a
    float score or ``None`` (failed/unparseable/timed-out).
    """
    from genesis.eval.rubrics import get_rubric

    rubric = get_rubric("memory_recall_grounding_runtime")
    template = rubric.prompt_template

    semaphore = asyncio.Semaphore(_GRADE_CONCURRENCY)
    to_grade = normalized[:_GRADE_TOP_N]
    grades = await asyncio.gather(
        *(_grade_one(router, semaphore, query, item["content"], template) for item in to_grade),
        return_exceptions=True,
    )
    # gather(return_exceptions=True) can surface raised exceptions as objects;
    # coerce anything that isn't a float|None into None.
    cleaned: list[float | None] = []
    for g in grades:
        if isinstance(g, float):
            cleaned.append(g)
        else:
            cleaned.append(None)
    return cleaned


def _bucket(grades: list[float | None]) -> str:
    """Classify the recall from per-item grades.

    - ``Correct``     — best graded score >= _RELEVANT.
    - ``Incorrect``   — best graded score < _INCORRECT_MAX (nothing even
      tangentially relevant).
    - ``Ambiguous``   — otherwise (best score in the borderline band).

    Assumes at least one non-None grade (caller guards the all-None case).
    """
    valid = [g for g in grades if g is not None]
    best = max(valid)
    if best >= _RELEVANT:
        return "Correct"
    if best < _INCORRECT_MAX:
        return "Incorrect"
    return "Ambiguous"


# ---------------------------------------------------------------------------
# Web fallback (knowledge path only)
# ---------------------------------------------------------------------------


async def _web_augment(query: str, *, rerank_enabled: bool = True) -> list[dict]:
    """Search + fetch + web-strip refine into result-shaped dicts.

    ONLY called for ``path == "knowledge"``. Search the web, fetch the top 1-2
    URLs, chunk each fetch into paragraphs, rerank the chunks against the query,
    and recompose the top chunks into a bounded snippet. Every web call is
    wrapped — any failure yields an empty list (skip web silently).

    ``rerank_enabled=False`` (the reranker kill switch) skips the Voyage chunk
    rerank entirely and falls back to leading chunks — no Voyage call is made.
    """
    from genesis.mcp.health.web_tools import _impl_web_fetch, _impl_web_search
    from genesis.memory.reranker import VoyageReranker

    out: list[dict] = []
    try:
        search = await _impl_web_search(query, max_results=5)
    except Exception:
        logger.debug("CRAG: web_search failed", exc_info=True)
        return out

    urls: list[str] = []
    for item in (search or {}).get("results", []):
        u = item.get("url")
        if u:
            urls.append(u)
        if len(urls) >= _WEB_MAX_URLS:
            break
    if not urls:
        return out

    # Kill switch off (rerank_enabled=False) → never instantiate/call Voyage;
    # the leading-chunks fallback below covers it.
    reranker = VoyageReranker() if rerank_enabled else None
    for url in urls:
        try:
            fetched = await _impl_web_fetch(url)
        except Exception:
            logger.debug("CRAG: web_fetch failed for %s", url, exc_info=True)
            continue
        text = (fetched or {}).get("content", "") or ""
        if not text.strip():
            continue

        # Chunk into paragraphs; rerank for query relevance; recompose.
        chunks = [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]
        if not chunks:
            continue
        docs = [{"id": str(i), "text": c} for i, c in enumerate(chunks)]
        snippet = ""
        score = 0.0
        try:
            reranked = await reranker.rerank(query, docs, top_k=_WEB_TOP_CHUNKS) if reranker else []
        except Exception:
            reranked = []
        if reranked:
            by_id = {d["id"]: d["text"] for d in docs}
            picked = [by_id[r["id"]] for r in reranked if r["id"] in by_id]
            score = float(reranked[0].get("score", 0.0) or 0.0)
        else:
            # Reranker degraded — fall back to leading chunks.
            picked = chunks[:_WEB_TOP_CHUNKS]
        snippet = "\n\n".join(picked)[:_WEB_SNIPPET_MAX_CHARS]
        if snippet.strip():
            # Match the knowledge_recall result shape (unit_id/origin) so web
            # candidates survive merge + don't KeyError downstream consumers.
            out.append(
                {
                    "unit_id": url,
                    "id": url,
                    "content": snippet,
                    "score": score,
                    "origin": "web",
                    "source_pipeline": "crag_web",
                }
            )

    if reranker is not None:
        with contextlib.suppress(Exception):
            await reranker.close()
    return out


# ---------------------------------------------------------------------------
# Corrective augmentation (Incorrect bucket only)
# ---------------------------------------------------------------------------


def _retrieval_to_dict(rr: Any) -> dict:
    """Convert a RetrievalResult to the memory_recall result dict shape."""
    return {
        "memory_id": getattr(rr, "memory_id", ""),
        "content": getattr(rr, "content", ""),
        "score": getattr(rr, "score", 0.0),
        "payload": getattr(rr, "payload", {}) or {},
        # Preserve the provenance discriminator (audit D12) so augmented items
        # are labeled first-party vs external-world at the MCP return pass —
        # the relaxed/raw-KB re-retrieve can legitimately pull KB content.
        "collection": getattr(rr, "collection", "episodic_memory"),
        "source_pipeline": getattr(rr, "source_pipeline", None),
        # WS-3: carry the STORED origin so an external_untrusted EPISODIC item
        # (whose collection alone reads first-party) is still wrapped + counted
        # at the MCP return pass. Without this, a CRAG-augmented external
        # episodic hit reopens the stored-origin bypass on the corrective
        # memory_recall path (Codex #1048).
        "origin_class": getattr(rr, "origin_class", None),
    }


async def _augment(
    *,
    query: str,
    kept: list[dict],
    retriever: Any,
    path: str,
) -> list[dict]:
    """One conservative round of corrective augmentation.

    Relaxed re-retrieve via the RAW retriever, raw KB, optional web (knowledge
    path only), then merge with ``kept``, dedup, rerank, and trim to the
    original limit. Returns result-shaped dicts.
    """
    from genesis.memory.graph_expansion import reranker_enabled
    from genesis.memory.reranker import VoyageReranker

    # CRAG runs inside the memory_recall / knowledge_recall tool path, so it
    # honors the same reranker kill switch (config mode + GENESIS_MEMORY_RERANK_OFF)
    # as the primary recall — otherwise the switch would still leak Voyage calls
    # through corrective augmentation.
    rerank_on = reranker_enabled()

    candidates: list[dict] = list(kept)

    # 1. Relaxed re-retrieve via the raw retriever (wide net, no filters).
    try:
        relaxed = await retriever.recall(
            query,
            limit=_RELAXED_LIMIT,
            min_activation=0.0,
            wing=None,
            room=None,
            life_domain=None,
            expand_query_terms=True,
            include_subsystem=True,
            rerank=rerank_on,
        )
        candidates.extend(_retrieval_to_dict(rr) for rr in relaxed)
    except Exception:
        logger.debug("CRAG: relaxed re-retrieve failed", exc_info=True)

    # 2. Raw knowledge-base retrieve.
    try:
        kb = await retriever.recall(query, source="knowledge", limit=_KB_LIMIT)
        candidates.extend(_retrieval_to_dict(rr) for rr in kb)
    except Exception:
        logger.debug("CRAG: raw KB re-retrieve failed", exc_info=True)

    # 3. Web fallback — knowledge path ONLY, never for memory.
    if path == "knowledge":
        try:
            web = await asyncio.wait_for(
                _web_augment(query, rerank_enabled=rerank_on), timeout=_WEB_TIMEOUT_S
            )
            candidates.extend(web)
        except TimeoutError:
            logger.debug("CRAG: web augment timed out")
        except Exception:
            logger.debug("CRAG: web augment failed", exc_info=True)

    # 4. Dedup by id (fallback to content) preserving first occurrence.
    deduped: list[dict] = []
    seen: set[str] = set()
    for c in candidates:
        key = _result_id(c) or _norm(c)["content"][:120]
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(c)

    # 5. Re-rank the merged set (degrade gracefully) and trim.
    docs = []
    for i, c in enumerate(deduped):
        content = _norm(c)["content"]
        if content:
            docs.append({"id": str(i), "text": content})
    if docs and rerank_on:
        reranker = VoyageReranker()
        try:
            reranked = await reranker.rerank(query, docs, top_k=_ORIG_LIMIT)
        except Exception:
            reranked = []
        finally:
            with contextlib.suppress(Exception):
                await reranker.close()
        if reranked:
            ordered = [deduped[int(r["id"])] for r in reranked if r["id"].isdigit()]
            if ordered:
                return ordered[:_ORIG_LIMIT]

    return deduped[:_ORIG_LIMIT]


# ---------------------------------------------------------------------------
# Calibration logging
# ---------------------------------------------------------------------------


async def _log(
    *,
    db: Any,
    recall_event_id: str | None,
    bucket: str,
    grades: list[float | None],
    action_taken: str,
    result_count_before: int,
    result_count_after: int,
    latency_ms: float,
) -> None:
    """Best-effort calibration log. Never raises (logging must not break recall)."""
    try:
        from genesis.eval.j9_hooks import emit_recall_corrected

        await emit_recall_corrected(
            db,
            recall_event_id=recall_event_id,
            bucket=bucket,
            grades=grades,
            action_taken=action_taken,
            result_count_before=result_count_before,
            result_count_after=result_count_after,
            latency_ms=latency_ms,
        )
    except Exception:
        logger.debug("CRAG: emit_recall_corrected unavailable", exc_info=True)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def maybe_correct_recall(
    *,
    query: str,
    results: list[dict],
    retriever: Any,
    db: Any,
    path: str,
    pipeline_used: str | None = None,
    recall_event_id: str | None = None,
    router: Any | None = None,
) -> list[dict]:
    """Selective corrective retrieval (CRAG).

    Returns possibly-corrected results in the SAME dict shape as input.
    BEST-EFFORT: must NEVER raise — on any failure or timeout, return the
    ORIGINAL ``results`` unchanged.
    """
    _t0 = time.monotonic()

    def _ms() -> float:
        return (time.monotonic() - _t0) * 1000

    try:
        # Drift skip — auto_drift already corrected; don't double-correct.
        if pipeline_used == "auto_drift":
            return results

        normalized = [_norm(r) for r in results]

        # Latency pre-gate: a confidently-high top score is treated as Correct
        # WITHOUT grading. (Empty recall has no top score → falls through to
        # grading as a candidate "Incorrect".)
        if normalized:
            top_score = max(n["score"] for n in normalized)
            if top_score >= _SKIP_GRADE_ABOVE:
                return results

        # Resolve the router; without it we cannot grade → return original.
        active_router = router if router is not None else _resolve_router()
        if active_router is None:
            await _log(
                db=db,
                recall_event_id=recall_event_id,
                bucket="skipped",
                grades=[],
                action_taken="skipped_no_router",
                result_count_before=len(results),
                result_count_after=len(results),
                latency_ms=_ms(),
            )
            return results

        # Grade the top-N. If grading can't run at all, keep original.
        try:
            grades = await _grade_results(active_router, query, normalized)
        except Exception:
            logger.debug("CRAG: grading failed", exc_info=True)
            return results

        if not any(g is not None for g in grades):
            # Grader effectively unavailable (all None/failed) — not Incorrect.
            await _log(
                db=db,
                recall_event_id=recall_event_id,
                bucket="skipped",
                grades=grades,
                action_taken="skipped_grader_unavailable",
                result_count_before=len(results),
                result_count_after=len(results),
                latency_ms=_ms(),
            )
            return results

        bucket = _bucket(grades)

        if bucket == "Correct":
            # Keep only items graded >= _RELEVANT, preserve order. Items beyond
            # the graded top-N have no grade → conservatively keep them.
            kept: list[dict] = []
            for i, r in enumerate(results):
                if i < len(grades):
                    g = grades[i]
                    if g is None or g >= _RELEVANT:
                        kept.append(r)
                else:
                    kept.append(r)
            out = kept if kept else results
            await _log(
                db=db,
                recall_event_id=recall_event_id,
                bucket=bucket,
                grades=grades,
                action_taken="keep_relevant",
                result_count_before=len(results),
                result_count_after=len(out),
                latency_ms=_ms(),
            )
            return out

        if bucket == "Ambiguous":
            # DARK: take NO action. Log only what would have happened.
            await _log(
                db=db,
                recall_event_id=recall_event_id,
                bucket=bucket,
                grades=grades,
                action_taken="dark_ambiguous",
                result_count_before=len(results),
                result_count_after=len(results),
                latency_ms=_ms(),
            )
            return results

        # bucket == "Incorrect" → ACT: one round of corrective augmentation.
        # Keep any items that DID grade relevant (usually none for Incorrect).
        kept_relevant: list[dict] = [
            results[i]
            for i, g in enumerate(grades)
            if g is not None and g >= _RELEVANT and i < len(results)
        ]
        try:
            corrected = await _augment(
                query=query,
                kept=kept_relevant,
                retriever=retriever,
                path=path,
            )
        except Exception:
            logger.debug("CRAG: augmentation failed", exc_info=True)
            corrected = results

        out = corrected if corrected else results
        await _log(
            db=db,
            recall_event_id=recall_event_id,
            bucket=bucket,
            grades=grades,
            action_taken="augment",
            result_count_before=len(results),
            result_count_after=len(out),
            latency_ms=_ms(),
        )
        return out

    except Exception:
        logger.warning("CRAG: maybe_correct_recall failed, returning original", exc_info=True)
        return results
