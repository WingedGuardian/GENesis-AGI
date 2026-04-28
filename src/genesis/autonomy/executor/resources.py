"""Pre-execution resource discovery for the task executor.

Searches Genesis's full knowledge base before task decomposition:
past executions, procedures, skills, observations. The decomposer LLM
sees this context and assigns relevant resources to each step. The step
dispatcher then loads full content for assigned resources.

Two entry points:
- gather_resource_inventory() — deep search, called before decomposition
- load_step_resources()       — full content, called before step execution
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

_CATALOG_PATH = Path.home() / ".genesis" / "skill_catalog.json"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Budget caps to prevent prompt bloat
_MAX_PAST_EXECUTIONS = 5
_MAX_PROCEDURES = 10
_MAX_OBSERVATIONS = 5
_MAX_CHARS_PER_RESULT = 300
_MAX_SKILL_CONTENT_CHARS = 2000


def _extract_keywords(text: str, max_keywords: int = 15) -> list[str]:
    """Extract meaningful keywords from task description for search."""
    stop_words = frozenset({
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "shall", "can",
        "for", "and", "nor", "but", "or", "yet", "so", "at", "by",
        "in", "of", "on", "to", "up", "out", "off", "over", "under",
        "with", "from", "into", "about", "that", "this", "it", "its",
        "not", "no", "all", "each", "every", "both", "few", "more",
        "most", "other", "some", "such", "than", "too", "very",
    })
    words = re.findall(r"[a-zA-Z_]{3,}", text.lower())
    seen: set[str] = set()
    keywords: list[str] = []
    for w in words:
        if w not in stop_words and w not in seen:
            seen.add(w)
            keywords.append(w)
            if len(keywords) >= max_keywords:
                break
    return keywords


async def gather_resource_inventory(
    db: aiosqlite.Connection | None,
    memory_store: Any | None,
    retriever: Any | None,
    task_description: str,
) -> str:
    """Build rich resource context for the decomposer.

    Searches Genesis's memory system for everything relevant to the task:
    past executions, procedures, skills, observations. Returns formatted
    markdown for injection into the decomposition prompt.

    Never raises — each section degrades independently.
    """
    sections: list[str] = []
    keywords = _extract_keywords(task_description)

    # 1. Past task executions — what has Genesis done like this before?
    try:
        past = await _search_past_executions(retriever, task_description)
        if past:
            sections.append(past)
    except Exception:
        logger.debug("Resource discovery: past executions search failed", exc_info=True)

    # 2. Relevant procedures — distilled lessons from past experience
    try:
        procs = await _search_procedures(db, keywords)
        if procs:
            sections.append(procs)
    except Exception:
        logger.debug("Resource discovery: procedure search failed", exc_info=True)

    # 3. Available skills — specialized workflows
    try:
        skills = _load_skill_catalog()
        if skills:
            sections.append(skills)
    except Exception:
        logger.debug("Resource discovery: skill catalog load failed", exc_info=True)

    # 4. Relevant observations — one-off learnings not yet promoted
    try:
        obs = await _search_observations(retriever, task_description)
        if obs:
            sections.append(obs)
    except Exception:
        logger.debug("Resource discovery: observation search failed", exc_info=True)

    # 5. MCP tool categories — what the step sessions can access
    sections.append(
        "### MCP Tool Categories\n"
        "Step sessions have access to: memory (store/recall), "
        "health monitoring, outreach, recon, code analysis (Serena), "
        "browser automation, and standard CC tools "
        "(Read, Write, Edit, Bash, Grep, Glob, WebSearch, WebFetch)."
    )

    return "\n\n".join(sections) if sections else ""


async def _search_past_executions(
    retriever: Any | None,
    task_description: str,
) -> str | None:
    """Search episodic memory for past task executions."""
    if retriever is None:
        return None

    results = await retriever.recall(
        f"task execution: {task_description}",
        source="episodic",
        limit=_MAX_PAST_EXECUTIONS * 2,  # over-fetch, then filter
    )

    # Post-filter for task executor traces
    filtered = []
    for r in results:
        payload = getattr(r, "payload", {}) or {}
        if payload.get("source") == "task_executor":
            filtered.append(r)
            if len(filtered) >= _MAX_PAST_EXECUTIONS:
                break

    if not filtered:
        return None

    lines = ["### Past Task Executions"]
    for r in filtered:
        content = getattr(r, "content", "") or ""
        lines.append(f"- {content[:_MAX_CHARS_PER_RESULT]}")
    return "\n".join(lines)


async def _search_procedures(
    db: aiosqlite.Connection | None,
    keywords: list[str],
) -> str | None:
    """Search procedural memory for relevant procedures."""
    if db is None or not keywords:
        return None

    from genesis.learning.procedural.matcher import find_relevant

    matches = await find_relevant(
        db, keywords, min_confidence=0.2, limit=_MAX_PROCEDURES,
    )

    if not matches:
        return None

    lines = ["### Relevant Procedures"]
    for m in matches:
        steps_str = ""
        if m.steps:
            steps_str = f" Steps: {', '.join(m.steps[:3])}"
            if len(m.steps) > 3:
                steps_str += f" (+{len(m.steps) - 3} more)"
        failure_str = ""
        if m.failure_modes:
            failure_str = f" Known failures: {len(m.failure_modes)}"
        lines.append(
            f"- **{m.task_type}** ({m.confidence:.0%}): "
            f"{m.principle or 'no description'}{steps_str}{failure_str}"
        )
    return "\n".join(lines)


async def _search_observations(
    retriever: Any | None,
    task_description: str,
) -> str | None:
    """Search for relevant observations and learnings."""
    if retriever is None:
        return None

    results = await retriever.recall(
        task_description,
        source="episodic",
        limit=_MAX_OBSERVATIONS,
    )

    # Filter for observation-type memories (not task traces)
    filtered = []
    for r in results:
        payload = getattr(r, "payload", {}) or {}
        source = payload.get("source", "")
        if source and source != "task_executor":
            filtered.append(r)
            if len(filtered) >= _MAX_OBSERVATIONS:
                break

    if not filtered:
        return None

    lines = ["### Relevant Observations & Learnings"]
    for r in filtered:
        content = getattr(r, "content", "") or ""
        lines.append(f"- {content[:_MAX_CHARS_PER_RESULT]}")
    return "\n".join(lines)


def _load_skill_catalog() -> str | None:
    """Load skill catalog and format as resource list."""
    if not _CATALOG_PATH.exists():
        return None

    try:
        data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    all_skills = data.get("tier1", []) + data.get("tier2", [])
    if not all_skills:
        return None

    lines = ["### Available Skills"]
    for skill in all_skills:
        name = skill.get("name", "unknown")
        desc = skill.get("description", "")
        tier = skill.get("tier", "?")
        tier_label = f"T{tier}" if tier in (1, 2) else ""
        lines.append(f"- **{name}** [{tier_label}]: {desc[:120]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step-level resource loading (full content for assigned resources)
# ---------------------------------------------------------------------------

_skill_catalog_cache: dict | None = None


def _get_skill_catalog() -> dict:
    """Load and cache skill catalog."""
    global _skill_catalog_cache
    if _skill_catalog_cache is not None:
        return _skill_catalog_cache
    if _CATALOG_PATH.exists():
        try:
            _skill_catalog_cache = json.loads(
                _CATALOG_PATH.read_text(encoding="utf-8"),
            )
            return _skill_catalog_cache
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _find_skill_path(name: str) -> Path | None:
    """Find the filesystem path for a skill by name."""
    catalog = _get_skill_catalog()
    for skill in catalog.get("tier1", []) + catalog.get("tier2", []):
        if skill.get("name", "").lower() == name.lower():
            path_str = skill.get("path", "")
            if path_str:
                # Paths in catalog are relative to repo root
                full = (_REPO_ROOT / path_str).resolve()
                # Guard against path traversal via poisoned catalog
                if full.is_relative_to(_REPO_ROOT.resolve()) and full.is_dir():
                    return full
    return None


async def load_step_resources(
    db: aiosqlite.Connection | None,
    step: dict,
) -> str | None:
    """Load full resource content for a step's assigned resources.

    Reads SKILL.md files and procedure details for resources the
    decomposer assigned to this step. Returns formatted markdown
    or None if no resources assigned.
    """
    parts: list[str] = []

    # Load assigned skills
    for skill_name in step.get("skills", []):
        if not isinstance(skill_name, str):
            continue
        skill_path = _find_skill_path(skill_name)
        if skill_path is None:
            logger.debug("Skill '%s' not found in catalog", skill_name)
            continue
        for md_name in ("SKILL.md", "skill.md", "README.md"):
            md_file = skill_path / md_name
            if md_file.exists():
                try:
                    content = md_file.read_text(encoding="utf-8", errors="replace")
                    if len(content) > _MAX_SKILL_CONTENT_CHARS:
                        content = content[:_MAX_SKILL_CONTENT_CHARS] + "\n[truncated]"
                    parts.append(f"### Skill: {skill_name}\n\n{content}")
                except OSError:
                    logger.debug("Failed to read skill file %s", md_file)
                break

    # Load assigned procedures
    for proc_type in step.get("procedures", []):
        if not isinstance(proc_type, str) or db is None:
            continue
        try:
            from genesis.learning.procedural.matcher import find_relevant

            matches = await find_relevant(db, [proc_type], min_confidence=0.1, limit=1)
            if matches:
                m = matches[0]
                steps_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(m.steps or []))
                parts.append(
                    f"### Procedure: {m.task_type}\n"
                    f"**Principle:** {m.principle}\n"
                    f"**Confidence:** {m.confidence:.0%}\n"
                    f"**Steps:**\n{steps_text}"
                )
        except Exception:
            logger.debug("Failed to load procedure '%s'", proc_type, exc_info=True)

    if not parts:
        return None
    return "\n\n".join(parts)
