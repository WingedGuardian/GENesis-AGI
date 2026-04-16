"""Conversation entity/decision/relationship extraction.

Reads formatted conversation text and produces structured extractions:
entities, decisions, evaluations, action items — each with confidence
scores, typed relationships, and temporal context.

Uses the skill refiner prompt pattern (JSON in backticks, regex extraction,
validation, retry on parse failure).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Proven extraction pattern from SkillRefiner (src/genesis/learning/skills/refiner.py)
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


EXTRACTION_PROMPT = """\
You are analyzing a conversation between a user and an AI assistant (Genesis).
Extract the key entities, decisions, evaluations, and action items discussed.

For each extraction, provide:
- content: A concise but complete description (1-3 sentences)
- type: One of: entity, decision, evaluation, action_item, preference, concept
- confidence: 0.0 to 1.0 (how certain are you this is correctly extracted?)
- entities: Named entities mentioned (tools, projects, people, organizations)
- relationships: Typed connections between entities
  - type: One of: discussed_in, evaluated_for, decided, action_item_for,
    categorized_as, related_to, succeeded_by, preceded_by
- temporal: Date or time reference if mentioned (ISO format preferred)

Focus on SUBSTANCE — what was discussed, decided, evaluated, or planned.
Skip: greetings, filler, acknowledgments.
DO extract: tool/project names discussed, opinions expressed, decisions made,
action items created, evaluations of external things, errors encountered,
file paths mentioned, technical concepts introduced.
Extract generously — 5-15 facts per conversation segment is normal.

Respond with a JSON object inside backticks containing:
1. "extractions" — array of extracted facts
2. "session_keywords" — array of keywords from this conversation (proper nouns,
   technical terms, action verbs, project names, tool names)
3. "session_topic" — one-line summary of what this conversation was about

```json
{
  "extractions": [
    {
      "content": "Agentmail — email infrastructure service for AI agents, evaluated positively from YouTube video review",
      "type": "evaluation",
      "confidence": 0.9,
      "entities": ["Agentmail"],
      "relationships": [
        {"from": "Agentmail", "to": "Genesis outreach", "type": "evaluated_for"},
        {"from": "Agentmail", "to": "P-3", "type": "categorized_as"}
      ],
      "temporal": "2026-03-17"
    }
  ],
  "session_keywords": ["agentmail", "email", "outreach", "evaluation"],
  "session_topic": "Evaluation of Agentmail email infrastructure for Genesis outreach"
}
```

If the conversation contains no extractable substance, return:
`{"extractions": [], "session_keywords": [], "session_topic": ""}`

Here is the conversation to analyze:

{conversation_text}
"""

RETRY_PROMPT = """\
Your previous response could not be parsed as valid JSON. Please try again.
Respond with ONLY a JSON object inside triple backticks. Example format:

```json
{"extractions": [{"content": "...", "type": "entity", "confidence": 0.8, "entities": ["..."], "relationships": [], "temporal": null}], "session_keywords": ["keyword1"], "session_topic": "One-line topic"}
```
"""


@dataclass
class Extraction:
    """A single extracted entity/decision/evaluation from a conversation."""

    content: str
    extraction_type: str  # entity, decision, evaluation, action_item, preference, concept
    confidence: float
    entities: list[str] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)
    temporal: str | None = None


@dataclass
class ExtractionResult:
    """Result of extracting from a conversation chunk."""

    extractions: list[Extraction]
    chunk_line_start: int
    chunk_line_end: int
    raw_response: str | None = None
    parse_error: str | None = None
    session_keywords: list[str] = field(default_factory=list)
    session_topic: str = ""


def build_extraction_prompt(conversation_text: str) -> str:
    """Build the extraction prompt for an LLM call.

    Uses string replace instead of .format() because the prompt template
    contains JSON examples with literal braces that would conflict.
    """
    return EXTRACTION_PROMPT.replace("{conversation_text}", conversation_text)


@dataclass
class ParsedResponse:
    """Parsed extraction response with optional session metadata."""

    extractions: list[Extraction]
    session_keywords: list[str] = field(default_factory=list)
    session_topic: str = ""


def parse_extraction_response(text: str) -> list[Extraction]:
    """Parse LLM response into Extraction objects (legacy interface).

    Uses the proven refiner pattern: extract JSON from backtick block,
    fall back to raw text, validate structure.

    Raises ValueError on parse failure (caller should retry or skip).
    """
    parsed = parse_extraction_response_full(text)
    return parsed.extractions


def parse_extraction_response_full(text: str) -> ParsedResponse:
    """Parse LLM response into extractions + session keywords/topic.

    Handles both formats:
    - New: {"extractions": [...], "session_keywords": [...], "session_topic": "..."}
    - Legacy: [...] (bare array of extractions)

    Raises ValueError on parse failure.
    """
    match = _JSON_BLOCK_RE.search(text)
    raw = match.group(1).strip() if match else text.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # LLMs often produce invalid \escapes in JSON (e.g., file paths).
        # Attempt to fix by escaping lone backslashes before re-parsing.
        try:
            import re as _re
            sanitized = _re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)
            data = json.loads(sanitized)
        except json.JSONDecodeError as exc2:
            raise ValueError(f"Failed to parse extraction JSON: {exc2}") from exc2

    # Handle new object format with extractions + metadata
    session_keywords: list[str] = []
    session_topic: str = ""

    if isinstance(data, dict):
        session_keywords = [
            str(k).lower().strip() for k in data.get("session_keywords", [])
            if isinstance(k, str) and k.strip()
        ]
        session_topic = str(data.get("session_topic", "")).strip()
        data = data.get("extractions", [])

    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")

    extractions: list[Extraction] = []
    for item in data:
        if not isinstance(item, dict):
            continue

        content = item.get("content", "").strip()
        if not content:
            continue

        ext_type = item.get("type", "entity")
        if ext_type not in (
            "entity", "decision", "evaluation",
            "action_item", "preference", "concept",
        ):
            ext_type = "entity"

        confidence = item.get("confidence", 0.5)
        if not isinstance(confidence, (int, float)):
            confidence = 0.5
        confidence = max(0.0, min(1.0, float(confidence)))

        entities = item.get("entities", [])
        if not isinstance(entities, list):
            entities = []
        entities = [str(e) for e in entities if e]

        relationships = item.get("relationships", [])
        if not isinstance(relationships, list):
            relationships = []
        # Validate relationship structure
        valid_rels: list[dict] = []
        for rel in relationships:
            if isinstance(rel, dict) and "from" in rel and "to" in rel and "type" in rel:
                valid_rels.append({
                    "from": str(rel["from"]),
                    "to": str(rel["to"]),
                    "type": str(rel["type"]),
                })

        temporal = item.get("temporal")
        if temporal is not None:
            temporal = str(temporal)

        extractions.append(Extraction(
            content=content,
            extraction_type=ext_type,
            confidence=confidence,
            entities=entities,
            relationships=valid_rels,
            temporal=temporal,
        ))

    return ParsedResponse(
        extractions=extractions,
        session_keywords=session_keywords,
        session_topic=session_topic,
    )


def extractions_to_store_kwargs(
    extraction: Extraction,
    *,
    source_session_id: str | None = None,
    transcript_path: str | None = None,
    source_line_range: tuple[int, int] | None = None,
) -> dict:
    """Convert an Extraction to kwargs for MemoryStore.store().

    Returns a dict ready to be unpacked as **kwargs to store().
    """
    now_iso = datetime.now(UTC).isoformat()

    tags = list(extraction.entities)
    tags.append(extraction.extraction_type)
    if extraction.temporal:
        tags.append(extraction.temporal)

    return {
        "content": extraction.content,
        "source": "session_extraction",
        "memory_type": "episodic",
        "tags": tags,
        "confidence": extraction.confidence,
        "auto_link": True,
        "source_session_id": source_session_id,
        "transcript_path": transcript_path,
        "source_line_range": source_line_range,
        "extraction_timestamp": now_iso,
        "source_pipeline": "harvest",
    }
