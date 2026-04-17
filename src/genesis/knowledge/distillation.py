"""Distillation pipeline — transforms raw ProcessedContent into structured KnowledgeUnits.

Uses LLM routing to extract understanding from raw text, producing structured
knowledge units suitable for storage in the knowledge base.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from genesis.knowledge.processors.base import ProcessedContent

logger = logging.getLogger(__name__)

_CALL_SITE = "40_knowledge_distillation"

# Max characters per chunk sent to the LLM for distillation
_MAX_CHUNK_CHARS = 12000

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
    ) -> list[KnowledgeUnit]:
        """Distill processed content into knowledge units."""
        if not content.text.strip():
            return []

        # Use sections if available, otherwise chunk the full text
        if content.sections and len(content.sections) > 1:
            chunks = content.sections
        else:
            chunks = _chunk_text(content.text)

        all_units: list[KnowledgeUnit] = []

        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue

            raw_units = await self._distill_chunk(chunk, content, project_type, domain)

            for raw in raw_units:
                confidence = raw.get("confidence", 0.85)
                if confidence < 0.3:
                    logger.info("Skipping low-confidence unit: %s (%.2f)",
                                raw.get("concept", "?")[:60], confidence)
                    continue

                unit = KnowledgeUnit(
                    concept=str(raw.get("concept", ""))[:200],
                    body=str(raw.get("body", "")),
                    domain=str(raw.get("domain", domain)),
                    relationships=raw.get("relationships", []) or [],
                    caveats=raw.get("caveats", []) or [],
                    tags=raw.get("tags", []) or [],
                    confidence=confidence,
                    section_title=f"Section {i + 1}" if len(chunks) > 1 else None,
                    source_date=content.metadata.get("source_date")
                    or content.metadata.get("upload_date"),
                )
                all_units.append(unit)

        logger.info("Distilled %d knowledge units from %s (%d chunks)",
                     len(all_units), content.source_path, len(chunks))
        return all_units

    async def _distill_chunk(
        self,
        chunk: str,
        content: ProcessedContent,
        project_type: str,
        domain: str,
    ) -> list[dict]:
        """Send a single chunk through the LLM for distillation."""
        context_hint = ""
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
