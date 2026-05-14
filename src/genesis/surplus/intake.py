"""Intelligence intake pipeline — atomize, score, route.

Converges surplus insights, recon findings, and web search results into a
unified pipeline.  Design spec:
  docs/superpowers/specs/2026-05-14-intelligence-intake-pipeline-design.md

Three-step pipeline:
  1. atomize(content, source_task_type) -> list[AtomicFinding]
  2. score(finding, source_type) -> ScoredFinding  (conditional — skipped for curated sources)
  3. route(scored_finding) -> "knowledge" | "observation" | "discard"
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.memory.store import MemoryStore
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)


# ── Types ────────────────────────────────────────────────────────────────


class IntakeSource(StrEnum):
    """Source categories with pre-assigned confidence tiers."""

    USER_DIRECTED = "user_directed"              # 0.9
    FOREGROUND_WEB = "foreground_web"            # 0.75
    BACKGROUND_TASK = "background_task"          # 0.7
    EMAIL_RECON = "email_recon"                  # 0.65
    ANTICIPATORY_RESEARCH = "anticipatory_research"  # 0.6
    MODEL_INTELLIGENCE = "model_intelligence"    # 0.6
    GITHUB_LANDSCAPE = "github_landscape"        # 0.5 — needs LLM scoring
    WEB_MONITORING = "web_monitoring"            # 0.5 — needs LLM scoring
    FREE_MODEL_INVENTORY = "free_model_inventory"  # 0.5 — needs LLM scoring
    SOURCE_DISCOVERY = "source_discovery"        # 0.4 — needs LLM scoring


# Sources that skip LLM scoring — already curated by an LLM upstream.
_CURATED_SOURCES: dict[IntakeSource, float] = {
    IntakeSource.USER_DIRECTED: 0.9,
    IntakeSource.FOREGROUND_WEB: 0.75,
    IntakeSource.BACKGROUND_TASK: 0.7,
    IntakeSource.EMAIL_RECON: 0.65,
    IntakeSource.ANTICIPATORY_RESEARCH: 0.6,
    IntakeSource.MODEL_INTELLIGENCE: 0.6,
}

# Sources that require LLM scoring.
_UNCURATED_SOURCES: dict[IntakeSource, float] = {
    IntakeSource.GITHUB_LANDSCAPE: 0.5,
    IntakeSource.WEB_MONITORING: 0.5,
    IntakeSource.FREE_MODEL_INVENTORY: 0.5,
    IntakeSource.SOURCE_DISCOVERY: 0.4,
}

# Map surplus TaskType strings to IntakeSource.
_TASK_TYPE_TO_SOURCE: dict[str, IntakeSource] = {
    "anticipatory_research": IntakeSource.ANTICIPATORY_RESEARCH,
    "brainstorm_user": IntakeSource.ANTICIPATORY_RESEARCH,
    "brainstorm_self": IntakeSource.ANTICIPATORY_RESEARCH,
    "code_audit": IntakeSource.ANTICIPATORY_RESEARCH,
    "gap_clustering": IntakeSource.ANTICIPATORY_RESEARCH,
    "meta_brainstorm": IntakeSource.ANTICIPATORY_RESEARCH,
    "memory_audit": IntakeSource.ANTICIPATORY_RESEARCH,
    "procedure_audit": IntakeSource.ANTICIPATORY_RESEARCH,
    "prompt_effectiveness_review": IntakeSource.ANTICIPATORY_RESEARCH,
}

# Task types that typically produce multiple findings and should be atomized.
_MULTI_FINDING_TASK_TYPES = frozenset({
    "anticipatory_research",
    "code_audit",
    "gap_clustering",
    "brainstorm_user",
    "brainstorm_self",
})


@dataclass(frozen=True)
class AtomicFinding:
    """A single, self-contained finding."""

    title: str
    content: str
    sources: list[str] = field(default_factory=list)
    relevance: str = ""


@dataclass(frozen=True)
class ScoredFinding:
    """An atomic finding with a confidence score and routing metadata."""

    finding: AtomicFinding
    confidence: float
    source: IntakeSource
    source_task_type: str = ""
    generating_model: str = ""
    is_pattern_insight: bool = False


class IntakeResult(StrEnum):
    """Routing decision."""

    KNOWLEDGE = "knowledge"
    OBSERVATION = "observation"
    DISCARD = "discard"


@dataclass
class IntakeStats:
    """Observability counters for a single intake run."""

    source: str = ""
    atomization_path: str = ""  # "json_findings", "json_single", "markdown_split", "single_item"
    findings_count: int = 0
    routed_knowledge: int = 0
    routed_observation: int = 0
    routed_discard: int = 0
    scoring_skipped: bool = False
    scoring_model: str = ""
    errors: list[str] = field(default_factory=list)


# ── Atomization ──────────────────────────────────────────────────────────


def atomize(content: str, source_task_type: str) -> tuple[list[AtomicFinding], str]:
    """Split content into atomic findings.

    Returns (findings, atomization_path) where path is one of:
    - "json_findings": parsed JSON with findings array
    - "json_single": valid JSON but no findings key
    - "markdown_split": split on markdown headings/numbered items
    - "single_item": treated as one finding (fallback or single-output type)
    """
    if not content or not content.strip():
        return [], "empty"

    # Single-output task types skip atomization entirely.
    if source_task_type not in _MULTI_FINDING_TASK_TYPES:
        return [AtomicFinding(
            title=source_task_type.replace("_", " ").title(),
            content=content.strip(),
        )], "single_item"

    # Attempt 1: Parse as JSON with findings array.
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "findings" in data:
            findings_raw = data["findings"]
            if isinstance(findings_raw, list) and findings_raw:
                findings = []
                for item in findings_raw:
                    if isinstance(item, dict):
                        findings.append(AtomicFinding(
                            title=str(item.get("title", ""))[:200],
                            content=str(item.get("content", "")),
                            sources=[str(s) for s in item.get("sources", [])
                                     if isinstance(s, str)],
                            relevance=str(item.get("relevance", "")),
                        ))
                    elif isinstance(item, str):
                        findings.append(AtomicFinding(
                            title=item[:200],
                            content=item,
                        ))
                if findings:
                    return findings, "json_findings"
        # Valid JSON but no findings key — treat as single item.
        if isinstance(data, dict):
            return [AtomicFinding(
                title=str(data.get("title", source_task_type.replace("_", " ").title()))[:200],
                content=json.dumps(data, indent=2) if not isinstance(data, str) else data,
            )], "json_single"
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # Attempt 2: Markdown split on headings or numbered items.
    findings = _markdown_split(content)
    if findings and len(findings) > 1:
        return findings, "markdown_split"

    # Attempt 3: Fall back to single item.
    return [AtomicFinding(
        title=source_task_type.replace("_", " ").title(),
        content=content.strip(),
    )], "single_item"


# Markdown heading pattern: ## Title or ### Title
_HEADING_RE = re.compile(r"^#{2,3}\s+(.+)", re.MULTILINE)
# Numbered list: 1. / 1) or **1.** / **1)** (bold numbered items)
_NUMBERED_RE = re.compile(r"^(?:\*{2})?\d+[.)]\*{0,2}\s+(.+?)(?:\n|$)", re.MULTILINE)


def _markdown_split(content: str) -> list[AtomicFinding]:
    """Best-effort split on markdown structure."""
    # Try heading-delimited sections first.
    headings = list(_HEADING_RE.finditer(content))
    if len(headings) >= 2:
        findings = []
        for i, match in enumerate(headings):
            title = match.group(1).strip()
            start = match.end()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(content)
            body = content[start:end].strip()
            if body:
                findings.append(AtomicFinding(title=title[:200], content=body))
        return findings

    # Try numbered items.
    numbered = list(_NUMBERED_RE.finditer(content))
    if len(numbered) >= 2:
        findings = []
        for i, match in enumerate(numbered):
            start = match.start()
            end = numbered[i + 1].start() if i + 1 < len(numbered) else len(content)
            chunk = content[start:end].strip()
            # Use the first line as title.
            lines = chunk.split("\n", 1)
            title = re.sub(r"^(?:\*{2})?\d+[.)]\*{0,2}\s*", "", lines[0]).strip()
            body = lines[1].strip() if len(lines) > 1 else title
            if title:
                findings.append(AtomicFinding(title=title[:200], content=body or title))
        return findings

    return []


# ── Scoring ──────────────────────────────────────────────────────────────


def score_finding(
    finding: AtomicFinding,
    source: IntakeSource,
    source_task_type: str = "",
    generating_model: str = "",
) -> ScoredFinding:
    """Assign confidence from source tier (curated sources skip LLM scoring).

    For uncurated sources, the caller must run LLM scoring via score_batch_llm().
    This function provides the non-LLM fast path.
    """
    confidence = _CURATED_SOURCES.get(source, _UNCURATED_SOURCES.get(source, 0.5))
    return ScoredFinding(
        finding=finding,
        confidence=confidence,
        source=source,
        source_task_type=source_task_type,
        generating_model=generating_model,
    )


def needs_llm_scoring(source: IntakeSource) -> bool:
    """Return True if this source needs LLM scoring."""
    return source in _UNCURATED_SOURCES


_INTAKE_SCORE_PROMPT = """\
You are scoring intelligence findings for relevance and quality.

For each finding below, assign a score from 0.0 to 1.0 based on:
- Relevance to the user's projects and interests
- Actionability — can something be done with this information?
- Novelty — is this new information or already known?
- Quality — is this well-sourced and specific?

Also mark if the finding represents a pattern-level insight (recurring theme,
systemic issue, cross-domain connection) vs a single data point.

Respond with ONLY a JSON array:
```json
[
  {{"index": 0, "score": 0.7, "is_pattern": false, "reason": "..."}},
  {{"index": 1, "score": 0.3, "is_pattern": false, "reason": "..."}}
]
```

## Findings to score:

{findings_text}
"""


async def score_batch_llm(
    findings: list[AtomicFinding],
    source: IntakeSource,
    router: Router,
    source_task_type: str = "",
    generating_model: str = "",
) -> list[ScoredFinding]:
    """Score a batch of findings via LLM (for uncurated sources).

    Falls back to default source confidence if the LLM call fails.
    """
    if not findings:
        return []

    default_confidence = _UNCURATED_SOURCES.get(source, 0.5)

    # Build the findings text for the prompt.
    parts = []
    for i, f in enumerate(findings):
        parts.append(f"### Finding {i}: {f.title}\n{f.content[:500]}")
    findings_text = "\n\n".join(parts)

    prompt = _INTAKE_SCORE_PROMPT.format(findings_text=findings_text)

    try:
        result = await router.route_call(
            "45_intelligence_intake",
            messages=[
                {"role": "system", "content": "You are an intelligence quality scorer."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        if not result.success or not result.content:
            logger.warning("Intake LLM scoring returned empty — using defaults")
            return [
                ScoredFinding(
                    finding=f, confidence=default_confidence,
                    source=source, source_task_type=source_task_type,
                    generating_model=generating_model,
                )
                for f in findings
            ]

        # Parse LLM scores.
        scores = _parse_score_response(result.content)
        scored = []
        for i, f in enumerate(findings):
            score_data = scores.get(i, {})
            scored.append(ScoredFinding(
                finding=f,
                confidence=float(score_data.get("score", default_confidence)),
                source=source,
                source_task_type=source_task_type,
                generating_model=generating_model,
                is_pattern_insight=bool(score_data.get("is_pattern", False)),
            ))
        return scored

    except Exception:
        logger.warning("Intake LLM scoring failed — using default confidence", exc_info=True)
        return [
            ScoredFinding(
                finding=f, confidence=default_confidence,
                source=source, source_task_type=source_task_type,
                generating_model=generating_model,
            )
            for f in findings
        ]


def _parse_score_response(content: str) -> dict[int, dict]:
    """Parse LLM scoring response into {index: {score, is_pattern, reason}}."""
    # Strip markdown code fences.
    content = re.sub(r"```(?:json)?\s*", "", content)
    content = content.strip()

    try:
        data = json.loads(content)
        if isinstance(data, list):
            return {
                item.get("index", i): item
                for i, item in enumerate(data)
                if isinstance(item, dict)
            }
    except (json.JSONDecodeError, ValueError):
        pass
    return {}


# ── Routing ──────────────────────────────────────────────────────────────


def route(scored: ScoredFinding) -> IntakeResult:
    """Determine where a scored finding goes.

    Thresholds:
    - >= 0.5 → knowledge base
    - >= 0.7 AND pattern insight → also create observation
    - 0.4–0.5 → knowledge at low confidence
    - < 0.4 → discard
    """
    if scored.confidence >= 0.7 and scored.is_pattern_insight:
        return IntakeResult.OBSERVATION
    if scored.confidence >= 0.4:
        return IntakeResult.KNOWLEDGE
    return IntakeResult.DISCARD


# ── Pipeline orchestration ───────────────────────────────────────────────


async def run_intake(
    content: str,
    source: IntakeSource,
    source_task_type: str = "",
    generating_model: str = "",
    *,
    db: aiosqlite.Connection,
    store: MemoryStore | None = None,
    router: Router | None = None,
) -> IntakeStats:
    """Run the full intake pipeline: atomize → score → route.

    For curated sources (surplus, email_recon, etc.), scoring is skipped
    and findings go directly to the knowledge base at source-tier confidence.

    For uncurated sources (github_landscape, web_monitoring), LLM scoring
    is applied via the 45_intelligence_intake call site.

    Args:
        content: Raw content to process.
        source: The IntakeSource category.
        source_task_type: Original task type string.
        generating_model: Model that generated the content.
        db: Database connection.
        store: MemoryStore for knowledge ingestion (resolved from runtime if None).
        router: Router for LLM scoring (only needed for uncurated sources).

    Returns:
        IntakeStats with observability counters.
    """
    stats = IntakeStats(source=source.value)

    # Step 1: Atomize.
    findings, atom_path = atomize(content, source_task_type)
    stats.atomization_path = atom_path
    stats.findings_count = len(findings)

    if not findings:
        return stats

    # Step 2: Score (conditional).
    if needs_llm_scoring(source):
        if router is not None:
            scored_findings = await score_batch_llm(
                findings, source, router,
                source_task_type=source_task_type,
                generating_model=generating_model,
            )
            stats.scoring_skipped = False
        else:
            logger.warning("No router available for LLM scoring — using defaults")
            scored_findings = [
                score_finding(f, source, source_task_type, generating_model)
                for f in findings
            ]
            stats.scoring_skipped = True
    else:
        scored_findings = [
            score_finding(f, source, source_task_type, generating_model)
            for f in findings
        ]
        stats.scoring_skipped = True

    # Step 3: Route each finding.
    for scored in scored_findings:
        destination = route(scored)
        try:
            if destination == IntakeResult.KNOWLEDGE:
                await _route_to_knowledge(scored, db=db, store=store)
                stats.routed_knowledge += 1
            elif destination == IntakeResult.OBSERVATION:
                await _route_to_knowledge(scored, db=db, store=store)
                await _route_to_observation(scored, db=db)
                stats.routed_knowledge += 1
                stats.routed_observation += 1
            else:
                stats.routed_discard += 1
        except Exception as exc:
            logger.error(
                "Intake routing failed for finding '%s': %s",
                scored.finding.title[:50], exc, exc_info=True,
            )
            stats.errors.append(f"{scored.finding.title[:50]}: {exc}")

    # Log intake event for observability.
    logger.info(
        "Intake: source=%s, atomization=%s, findings=%d, "
        "knowledge=%d, observation=%d, discard=%d, scoring_skipped=%s",
        stats.source, stats.atomization_path, stats.findings_count,
        stats.routed_knowledge, stats.routed_observation,
        stats.routed_discard, stats.scoring_skipped,
    )

    # If ALL non-discard findings failed routing, raise so caller can fall back.
    expected_routes = stats.findings_count - stats.routed_discard
    actual_routes = stats.routed_knowledge + stats.routed_observation
    if expected_routes > 0 and actual_routes == 0 and stats.errors:
        raise RuntimeError(
            f"Intake routing failed for all {expected_routes} findings: "
            f"{stats.errors[0]}"
        )

    return stats


async def _route_to_knowledge(
    scored: ScoredFinding,
    *,
    db: aiosqlite.Connection,
    store: MemoryStore | None = None,
) -> None:
    """Store finding in the knowledge base."""
    # Resolve MemoryStore from runtime if not provided.
    if store is None:
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            store = rt._memory_store
        except Exception:
            logger.warning("Cannot resolve MemoryStore — falling back to memory_store MCP")
            store = None

    if store is None:
        raise RuntimeError("No MemoryStore available for knowledge ingestion")

    from genesis.memory.knowledge_ingest import ingest_knowledge_unit

    # Build provenance.
    provenance = {
        "source_doc": f"intake:{scored.source.value}",
        "ingested_at": datetime.now(UTC).isoformat(),
    }
    if scored.generating_model:
        provenance["generating_model"] = scored.generating_model

    # Determine domain from source.
    domain = _domain_for_source(scored.source, scored.source_task_type)

    sources_str = ""
    if scored.finding.sources:
        sources_str = "\n\nSources: " + ", ".join(scored.finding.sources)

    content_for_kb = (
        f"{scored.finding.title}\n\n"
        f"{scored.finding.content}"
        f"{sources_str}"
    )
    if scored.finding.relevance:
        content_for_kb += f"\n\nRelevance: {scored.finding.relevance}"

    await ingest_knowledge_unit(
        store=store,
        db=db,
        content=content_for_kb,
        project="genesis",
        domain=domain,
        authority=scored.source.value,
        provenance=provenance,
        memory_class="fact",
    )


async def _route_to_observation(
    scored: ScoredFinding,
    *,
    db: aiosqlite.Connection,
) -> None:
    """Create an observation for pattern-level insights."""
    from genesis.db.crud import observations

    obs_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    content = (
        f"{scored.finding.title}\n\n"
        f"{scored.finding.content}"
    )
    if scored.finding.relevance:
        content += f"\n\nRelevance: {scored.finding.relevance}"

    await observations.create(
        db,
        id=obs_id,
        source=f"intake:{scored.source.value}",
        type=scored.source_task_type or "intelligence_finding",
        content=content[:2000],
        priority="medium",
        created_at=now,
    )


def _domain_for_source(source: IntakeSource, task_type: str) -> str:
    """Map source/task_type to a knowledge domain string."""
    domain_map = {
        IntakeSource.EMAIL_RECON: "intelligence.email",
        IntakeSource.GITHUB_LANDSCAPE: "intelligence.github",
        IntakeSource.MODEL_INTELLIGENCE: "intelligence.models",
        IntakeSource.FREE_MODEL_INVENTORY: "intelligence.models",
        IntakeSource.WEB_MONITORING: "intelligence.web",
        IntakeSource.SOURCE_DISCOVERY: "intelligence.sources",
        IntakeSource.FOREGROUND_WEB: "research.web",
        IntakeSource.BACKGROUND_TASK: "research.background",
        IntakeSource.USER_DIRECTED: "research.user",
    }
    if source in domain_map:
        return domain_map[source]
    # Fall back based on task type.
    if "code" in task_type:
        return "intelligence.code"
    return "intelligence.surplus"


# ── Web search capture ───────────────────────────────────────────────────


async def capture_web_result(
    url: str,
    content_summary: str,
    query_context: str,
    session_type: str = "foreground",
    *,
    db: aiosqlite.Connection,
    store: MemoryStore | None = None,
) -> IntakeStats:
    """Capture a web search result through the intake pipeline.

    Called by sessions when a web search/fetch returns useful results.
    Not automatic — the session decides what's worth capturing.

    session_type: "foreground" (0.75), "background" (0.7), "surplus" (0.6)
    """
    source_map = {
        "foreground": IntakeSource.FOREGROUND_WEB,
        "background": IntakeSource.BACKGROUND_TASK,
        "surplus": IntakeSource.ANTICIPATORY_RESEARCH,
    }
    source = source_map.get(session_type, IntakeSource.FOREGROUND_WEB)

    content = (
        f"Web result: {url}\n\n"
        f"Query: {query_context}\n\n"
        f"{content_summary}"
    )

    return await run_intake(
        content=content,
        source=source,
        source_task_type="web_capture",
        db=db,
        store=store,
    )


# ── Helpers for callers ──────────────────────────────────────────────────


def source_for_task_type(task_type: str) -> IntakeSource:
    """Map a surplus task type string to its IntakeSource."""
    return _TASK_TYPE_TO_SOURCE.get(task_type, IntakeSource.ANTICIPATORY_RESEARCH)
