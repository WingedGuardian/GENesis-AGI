"""Distillation pipeline — transforms raw ProcessedContent into structured KnowledgeUnits.

Uses LLM routing to extract understanding from raw text, producing structured
knowledge units suitable for storage in the knowledge base.
"""

from __future__ import annotations

import asyncio
import json
import logging
import typing
from dataclasses import dataclass, field

from genesis.knowledge.processors.base import ProcessedContent

logger = logging.getLogger(__name__)

_CALL_SITE = "40_knowledge_distillation"

# Max characters per chunk sent to the LLM for distillation
_MAX_CHUNK_CHARS = 12000

# Max concurrent LLM calls for parallel chunk processing
_MAX_CONCURRENT_CHUNKS = 4

_DISTILLATION_SYSTEM_PROMPT = """\
You are a knowledge distillation engine. Your job is to extract structured
knowledge units from raw content.

For each meaningful concept, fact, or insight in the content, produce a
knowledge unit. Each unit should capture UNDERSTANDING, not reproduction
— distill the material into what someone would need to know.

Output a JSON array of objects with these fields:
- concept: Short title (max 200 chars) — what this unit is about
- body: The distilled knowledge (1-3 paragraphs)
- domain: Knowledge domain (e.g., "aws", "python", "leadership", "resume-advice")
- relationships: JSON array of related concepts (strings)
- caveats: JSON array of limitations or qualifications
- tags: JSON array of topical tags
- confidence: Float 0.0-1.0 — how well-structured and clear the source was

Rules:
- Extract UNDERSTANDING, not quotes. Paraphrase and synthesize.
- Each unit should stand alone — readable without the source.
- Skip trivial or redundant content.
- If the content is poorly structured or unclear, set confidence < 0.5.
- Return an empty array if there's nothing meaningful to extract.
- These are machine-extracted summaries, not authoritative facts. Include
  appropriate caveats noting the source and extraction context.

Output ONLY the JSON array, no markdown fences, no explanation."""


@dataclass
class KnowledgeUnit:
    """A structured knowledge unit ready for storage."""

    concept: str
    body: str
    domain: str
    relationships: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.85
    section_title: str | None = None
    source_date: str | None = None


@dataclass
class ChunkResult:
    """Result of distilling a single chunk."""

    index: int
    units: list[KnowledgeUnit]


def _chunk_text(text: str, max_chars: int = _MAX_CHUNK_CHARS) -> list[str]:
    """Split text into chunks, preferring section boundaries."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    # Try to split on double newlines (paragraph boundaries)
    paragraphs = text.split("\n\n")
    current_chunk: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2  # +2 for the \n\n separator
        if current_len + para_len > max_chars and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [para]
            current_len = para_len
        else:
            current_chunk.append(para)
            current_len += para_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def _parse_llm_response(response_text: str) -> list[dict]:
    """Parse JSON array from LLM response, handling common format issues."""
    text = response_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
    except json.JSONDecodeError:
        # Try to find a JSON array in the response
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

    logger.warning("Failed to parse LLM distillation response as JSON")
    return []


class DistillationPipeline:
    """Transform raw content into structured knowledge units via LLM."""

    def __init__(self, router: object) -> None:
        self._router = router

    async def distill(
        self,
        content: ProcessedContent,
        *,
        project_type: str,
        domain: str = "auto",
        user_context: str | None = None,
        on_chunk_done: typing.Callable | None = None,
    ) -> list[KnowledgeUnit]:
        """Distill processed content into knowledge units.

        Args:
            content: Processed source content.
            project_type: Project classification for the knowledge.
            domain: Knowledge domain (or "auto" for LLM detection).
            user_context: Optional user-provided context about the document
                (e.g., "This is a proposed plan, not established fact").
            on_chunk_done: Optional async callback(chunk_index, units) called
                after each chunk completes. Used for checkpoint progress.
        """
        if not content.text.strip():
            return []

        # Use sections if available, otherwise chunk the full text
        if content.sections and len(content.sections) > 1:
            chunks = content.sections
        else:
            chunks = _chunk_text(content.text)

        # Process chunks in parallel with concurrency limit
        sem = asyncio.Semaphore(_MAX_CONCURRENT_CHUNKS)

        async def _process_one(i: int, chunk: str) -> ChunkResult:
            async with sem:
                raw_units = await self._distill_chunk(
                    chunk, content, project_type, domain, user_context,
                )
                units = []
                for raw in raw_units:
                    confidence = raw.get("confidence", 0.85)
                    if confidence < 0.3:
                        logger.info(
                            "Skipping low-confidence unit: %s (%.2f)",
                            raw.get("concept", "?")[:60], confidence,
                        )
                        continue

                    # Include user context in caveats if provided
                    caveats = raw.get("caveats", []) or []
                    if user_context:
                        caveats.append(f"User context: {user_context[:200]}")

                    unit = KnowledgeUnit(
                        concept=str(raw.get("concept", ""))[:200],
                        body=str(raw.get("body", "")),
                        domain=str(raw.get("domain", domain)),
                        relationships=raw.get("relationships", []) or [],
                        caveats=caveats,
                        tags=raw.get("tags", []) or [],
                        confidence=confidence,
                        section_title=f"Section {i + 1}" if len(chunks) > 1 else None,
                        source_date=content.metadata.get("source_date")
                        or content.metadata.get("upload_date"),
                    )
                    units.append(unit)

                result = ChunkResult(index=i, units=units)

                # Notify caller of chunk completion (for checkpointing)
                if on_chunk_done is not None:
                    try:
                        await on_chunk_done(i, units)
                    except Exception:
                        logger.warning("on_chunk_done callback failed for chunk %d", i, exc_info=True)

                return result

        tasks = [
            _process_one(i, chunk)
            for i, chunk in enumerate(chunks)
            if chunk.strip()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect units in chunk order
        all_units: list[KnowledgeUnit] = []
        for r in sorted(
            (r for r in results if isinstance(r, ChunkResult)),
            key=lambda cr: cr.index,
        ):
            all_units.extend(r.units)

        # Log any chunk-level exceptions
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Chunk distillation failed: %s", r)

        logger.info(
            "Distilled %d knowledge units from %s (%d chunks, %d concurrent)",
            len(all_units), content.source_path, len(chunks), _MAX_CONCURRENT_CHUNKS,
        )
        return all_units

    async def _distill_chunk(
        self,
        chunk: str,
        content: ProcessedContent,
        project_type: str,
        domain: str,
        user_context: str | None = None,
    ) -> list[dict]:
        """Send a single chunk through the LLM for distillation."""
        context_hint = ""

        # Include user-provided context about the document
        if user_context:
            context_hint += f"\nUser context about this document: {user_context}"

        if content.metadata.get("title"):
            context_hint += f"\nSource title: {content.metadata['title']}"
        if content.metadata.get("channel"):
            context_hint += f"\nSource channel: {content.metadata['channel']}"
        context_hint += f"\nSource type: {content.source_type}"
        context_hint += f"\nProject: {project_type}"
        if domain != "auto":
            context_hint += f"\nDomain: {domain}"

        user_message = f"Distill the following content into knowledge units.{context_hint}\n\n---\n\n{chunk}"

        messages = [
            {"role": "system", "content": _DISTILLATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        try:
            result = await self._router.route_call(_CALL_SITE, messages)
            if not result.success or not result.content:
                logger.warning("Distillation LLM call failed for chunk: %s",
                               result.error or "empty response")
                return []
            return _parse_llm_response(result.content)
        except Exception:
            logger.warning("Distillation failed for chunk", exc_info=True)
            return []
