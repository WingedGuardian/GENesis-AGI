"""Surplus executors — stub and LLM-based implementations.

Surplus tasks use the Router directly for LLM calls.  They produce
surplus-specific insights (not reflection observations).  The reflection
engine is used ONLY by the awareness loop — surplus and reflection are
separate pipelines.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.db.crud import observations
from genesis.surplus.types import ExecutorResult, SurplusTask, TaskType

if TYPE_CHECKING:
    import aiosqlite

    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# Low-information patterns: surplus outputs matching these contribute no value.
_LOW_INFO_PATTERNS = re.compile(
    r"no significant (changes?|activity|events?|issues?)"
    r"|system (is )?(operating|running) (normally|as expected)"
    r"|nothing (notable|significant|unusual|new)"
    r"|all (systems?|metrics?) (are )?(within|normal|stable|healthy)"
    r"|no action (required|needed|necessary)",
    re.IGNORECASE,
)

_QUERY_LINE_RE = re.compile(r"^(?:\d+[.)]\s*|[-*]\s*)")


def _parse_search_queries(llm_output: str, max_queries: int = 5) -> list[str]:
    """Parse search queries from numbered/bulleted LLM output."""
    queries: list[str] = []
    for line in llm_output.strip().splitlines():
        line = _QUERY_LINE_RE.sub("", line).strip()
        if line and len(line) > 5:
            queries.append(line)
            if len(queries) >= max_queries:
                break
    return queries


async def _fetch_search_results(queries: list[str]) -> str:
    """Fetch web search results for parsed queries.

    Prefers TinyFish (free, fast, no quota) when API_KEY_TINYFISH is set;
    falls back to genesis.web.WebSearcher (Brave) otherwise.
    """
    import os

    use_tinyfish = bool(os.environ.get("API_KEY_TINYFISH"))

    parts: list[str] = []
    for i, query in enumerate(queries, 1):
        if use_tinyfish:
            try:
                from genesis.providers import tinyfish_client

                response = await tinyfish_client.search(query)
                results = response.get("results", []) or []
                if not results:
                    parts.append(f"### Query {i}: {query}\n(No results)")
                    continue
                parts.append(f"### Query {i}: {query}")
                for r in results[:5]:
                    title = r.get("title", "")
                    url = r.get("url", "")
                    snippet = (r.get("snippet") or "")[:300]
                    parts.append(f"- **{title}**\n  URL: {url}\n  {snippet}")
                continue
            except Exception as exc:
                logger.debug("TinyFish search failed for %r, falling back: %s", query, exc)

        # Fallback path — WebSearcher (Brave/SearXNG)
        try:
            from genesis.web import _get_searcher

            searcher = _get_searcher()
            response = await searcher.search(query, max_results=5)
            if response.error:
                parts.append(f"### Query {i}: {query}\n(Search failed: {response.error})")
                continue
            if not response.results:
                parts.append(f"### Query {i}: {query}\n(No results)")
                continue
            parts.append(f"### Query {i}: {query}")
            for r in response.results:
                parts.append(f"- **{r.title}**\n  URL: {r.url}\n  {r.snippet[:300]}")
        except Exception as exc:
            parts.append(f"### Query {i}: {query}\n(Error: {exc})")
    return "\n\n".join(parts)


# ── Cognitive task context helpers ─────────────────────────────────

# Task types that benefit from essential knowledge + user model context.
_COGNITIVE_TASK_TYPES = frozenset({
    TaskType.BRAINSTORM_USER, TaskType.BRAINSTORM_SELF,
    TaskType.RESEARCH_QUERY_GEN, TaskType.GAP_CLUSTERING,
    TaskType.SELF_UNBLOCK, TaskType.MEMORY_AUDIT, TaskType.PROCEDURE_AUDIT,
})

# Fields extracted from user model — subset of ego's priority_keys
# (ego/user_context.py), excluding UI-oriented fields like
# communication_preferences and autonomy_preferences.
_USER_MODEL_PRIORITY_KEYS = [
    "active_projects", "current_focus", "priorities",
    "goals", "professional_role", "expertise_areas",
    "interests", "active_investigations", "binding_constraints",
]

_EK_PATH = Path.home() / ".genesis" / "essential_knowledge.md"


def _read_essential_knowledge(*, max_chars: int = 2000) -> str | None:
    """Read essential knowledge file, returning content truncated to max_chars."""
    try:
        if not _EK_PATH.exists():
            return None
        content = _EK_PATH.read_text().strip()
        if not content:
            return None
        return content[:max_chars]
    except Exception:
        logger.debug("Failed to read essential knowledge", exc_info=True)
        return None


async def _read_user_model_compact(
    db: aiosqlite.Connection, *, max_chars: int = 1500,
) -> str | None:
    """Read compact user model from DB, extracting priority fields."""
    try:
        cursor = await db.execute(
            "SELECT model_json FROM user_model_cache WHERE id = 'current'"
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return None

        model = json.loads(row[0])
        if not model:
            return None

        lines: list[str] = []
        total = 0
        for key in _USER_MODEL_PRIORITY_KEYS:
            if key not in model:
                continue
            val = model[key]
            if isinstance(val, str):
                val_str = val[:200] + ("..." if len(val) > 200 else "")
            elif isinstance(val, (list, dict)):
                raw = json.dumps(val, default=str)
                val_str = raw[:200] + ("..." if len(raw) > 200 else "")
            else:
                raw = str(val)
                val_str = raw[:200] + ("..." if len(raw) > 200 else "")
            line = f"- {key}: {val_str}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)

        return "\n".join(lines) if lines else None
    except Exception:
        logger.debug("Failed to read user model", exc_info=True)
        return None


class StubExecutor:
    """Stub executor — generates structured placeholders.

    Used as fallback when the Router is unavailable.
    """

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        """Generate a placeholder insight for the given surplus task."""
        content = (
            f"[Stub] Surplus task {task.task_type} completed. "
            f"Drive: {task.drive_alignment}. "
            f"No LLM executor available — using placeholder."
        )
        insight = {
            "content": content,
            "source_task_type": task.task_type,
            "generating_model": "stub",
            "drive_alignment": task.drive_alignment,
            "confidence": 0.0,
        }
        return ExecutorResult(
            success=True,
            content=content,
            insights=[insight],
        )


# Call site mapping for surplus tasks.  Tasks with their own call sites
# get routed to dedicated provider chains; others use the generic surplus
# analysis call site.
_CALL_SITES: dict[TaskType, str] = {
    TaskType.BRAINSTORM_USER: "12_surplus_brainstorm",
    TaskType.BRAINSTORM_SELF: "12_surplus_brainstorm",
    TaskType.META_BRAINSTORM: "12_surplus_brainstorm",
    TaskType.INFRASTRUCTURE_MONITOR: "37_infrastructure_monitor",
}

# Fallback call site for analytical tasks without a dedicated entry.
_DEFAULT_CALL_SITE = "12_surplus_brainstorm"


# ── Task-specific prompt templates ──────────────────────────────────

_TASK_PROMPTS: dict[TaskType, str] = {
    TaskType.INFRASTRUCTURE_MONITOR: (
        "You are monitoring infrastructure for an autonomous AI system.\n\n"
        "## Recent Signal Data\n{signals}\n\n"
        "## Task\n"
        "Assess the current infrastructure state.  Report ONLY if something "
        "needs attention — resource pressure, degraded services, anomalous "
        "patterns, or trends that could become problems.\n\n"
        "If everything is operating normally with no concerns, respond with "
        "exactly the word NOMINAL and nothing else.\n\n"
        "Respond in plain text (2-4 sentences for concerns, or just NOMINAL).\n\n"
        "Do not reference specific system commands, service names, or file "
        "paths unless you have verified they exist.  Report what you observe, "
        "not what you assume the fix would be."
    ),
    TaskType.BRAINSTORM_USER: (
        "You are brainstorming ways to create value for your user.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Generate 2-3 concrete, actionable ideas for how the system could "
        "better serve the user based on the system context and user profile "
        "above.  Each idea should be specific enough to act on.\n\n"
        "Respond with ONLY a JSON object in this exact format:\n"
        '```json\n'
        '{{\n'
        '  "findings": [\n'
        '    {{\n'
        '      "title": "Short idea title",\n'
        '      "content": "Detailed description of the idea",\n'
        '      "sources": [],\n'
        '      "relevance": "Why this would create value"\n'
        '    }}\n'
        '  ]\n'
        '}}\n'
        '```\n'
    ),
    TaskType.BRAINSTORM_SELF: (
        "You are brainstorming ways to improve your own capabilities.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Identify 2-3 concrete improvements to your own processes, skills, "
        "or knowledge that would make you more effective.  Focus on gaps "
        "exposed by recent work.\n\n"
        "Respond with ONLY a JSON object in this exact format:\n"
        '```json\n'
        '{{\n'
        '  "findings": [\n'
        '    {{\n'
        '      "title": "Short improvement title",\n'
        '      "content": "What to improve and how",\n'
        '      "sources": [],\n'
        '      "relevance": "Why this improvement matters"\n'
        '    }}\n'
        '  ]\n'
        '}}\n'
        '```\n'
    ),
    TaskType.META_BRAINSTORM: (
        "You are reviewing the quality of recent brainstorm outputs.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Assess whether recent brainstorms have been useful or repetitive.  "
        "Suggest adjustments to brainstorm focus areas.\n\n"
        "Respond in plain text (2-4 sentences)."
    ),
    TaskType.MEMORY_AUDIT: (
        "You are auditing the memory system for an autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Identify memory quality issues: contradictions, stale entries, "
        "duplicate information, or gaps.  Suggest specific cleanup actions.\n\n"
        "Respond in plain text with bullet points."
    ),
    TaskType.PROCEDURE_AUDIT: (
        "You are auditing learned procedures for an autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Review recent procedures for accuracy and relevance.  Flag any "
        "that are outdated, low-confidence, or contradictory.\n\n"
        "Respond in plain text with bullet points."
    ),
    TaskType.GAP_CLUSTERING: (
        "You are analyzing observation patterns for an autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Cluster recent unresolved observations into themes.  Identify "
        "recurring patterns that suggest systemic issues rather than "
        "one-off events.\n\n"
        "Respond with ONLY a JSON object in this exact format:\n"
        '```json\n'
        '{{\n'
        '  "findings": [\n'
        '    {{\n'
        '      "title": "Theme/pattern name",\n'
        '      "content": "Description of the pattern and evidence",\n'
        '      "sources": [],\n'
        '      "relevance": "Why this pattern matters"\n'
        '    }}\n'
        '  ]\n'
        '}}\n'
        '```\n'
    ),
    TaskType.SELF_UNBLOCK: (
        "You are helping an autonomous AI system get unstuck.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Identify what's blocking progress and suggest concrete unblocking "
        "actions.  Focus on the highest-leverage intervention.\n\n"
        "Respond in plain text (2-4 sentences)."
    ),
    TaskType.RESEARCH_QUERY_GEN: (
        "You are generating web search queries for an autonomous AI's "
        "research pipeline.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Based on the context above, generate 3-5 specific web search queries "
        "that would help fill knowledge gaps or answer open questions.\n\n"
        "Rules:\n"
        "- Each query should be a concrete search engine query (not a topic)\n"
        "- Ground queries in the user's actual projects and interests\n"
        "- Focus on gaps where web research would add real value\n"
        "- Avoid queries about the system itself — focus on external knowledge\n\n"
        "Respond with ONLY the queries, one per line, numbered:\n"
        "1. first query\n"
        "2. second query\n"
        "..."
    ),
    TaskType.ANTICIPATORY_RESEARCH: (
        "You are synthesizing web research findings for an autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Synthesize the search results above into 1-3 actionable findings.\n\n"
        "If no useful search results were found, respond with a plain text "
        "message explaining that and suggesting better queries.\n\n"
        "Otherwise, respond with ONLY a JSON object in this exact format:\n"
        '```json\n'
        '{{\n'
        '  "findings": [\n'
        '    {{\n'
        '      "title": "Short descriptive title",\n'
        '      "content": "The insight, explained clearly",\n'
        '      "sources": ["https://..."],\n'
        '      "relevance": "Why this matters for the user or system"\n'
        '    }}\n'
        '  ]\n'
        '}}\n'
        '```\n'
    ),
    TaskType.PROMPT_REVIEW_CATALOG: (
        "You are cataloging LLM call site activity for an autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Review the call site activity data above.  For each active call "
        "site, summarize: what it does, how often it fires, which model it "
        "uses, its cost efficiency (tokens in vs out), and success rate.\n\n"
        "Flag any concerning patterns: silent sites (not firing when they "
        "should), expensive sites (high token usage for low value), or "
        "failing sites (low success rate).\n\n"
        "If no call site data is available, state that no data was found "
        "and produce no findings.\n\n"
        "Respond in plain text with a numbered list of call sites and a "
        "summary section for flagged concerns."
    ),
    TaskType.PROMPT_REVIEW_SAMPLE: (
        "You are sampling and scoring recent LLM outputs for an "
        "autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Review the recent LLM outputs above.  For each task type or call "
        "site represented, score the outputs on three dimensions:\n"
        "- **Relevance**: Does the output address the intended purpose?\n"
        "- **Actionability**: Are recommendations specific enough to act on?\n"
        "- **Specificity**: Does it reference real data, or is it generic?\n\n"
        "Use a simple Low/Medium/High scale.  Identify which task types "
        "produce consistently good vs poor results.\n\n"
        "Respond in plain text with a scored summary per task type."
    ),
    TaskType.PROMPT_EFFECTIVENESS_REVIEW: (
        "You are synthesizing a prompt effectiveness review for an "
        "autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Using the call site activity catalog and output quality scores "
        "in '## Previous Step Output' above (if present), identify the "
        "2-3 highest-leverage improvements to LLM prompts or call sites.\n\n"
        "For each recommendation:\n"
        "1. Name the call site or task type\n"
        "2. Describe the failure pattern (low relevance, generic output, etc.)\n"
        "3. Suggest a concrete prompt improvement\n\n"
        "If no prior analysis is present, assess general prompt quality "
        "from the context above.\n\n"
        "Respond in plain text with numbered recommendations."
    ),
}


class SurplusLLMExecutor:
    """Executes surplus analytical tasks via direct Router calls.

    Surplus tasks produce surplus insights (stored in surplus_staging),
    NOT reflection observations.  The reflection engine is not involved.
    """

    def __init__(self, router: Router, *, db: aiosqlite.Connection) -> None:
        self._router = router
        self._db = db
        self._topic_manager = None

    def set_topic_manager(self, manager) -> None:
        """Set TopicManager for posting surplus insights to Telegram."""
        self._topic_manager = manager

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        prompt = await self._build_prompt(task)
        call_site = _CALL_SITES.get(task.task_type, _DEFAULT_CALL_SITE)

        try:
            result = await self._router.route_call(
                call_site,
                [{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            logger.error(
                "Surplus LLM call failed for %s: %s", task.task_type, exc, exc_info=True,
            )
            return ExecutorResult(success=False, error=f"{type(exc).__name__}: {exc}")

        if not result.success or not result.content:
            error = result.error or "LLM returned empty response"
            return ExecutorResult(success=False, error=error)

        content = result.content.strip()

        # Quality gate: NOMINAL means nothing noteworthy — skip insight + Telegram
        if content.upper().startswith("NOMINAL") and len(content) < 40:
            logger.info("Surplus task %s reported NOMINAL — skipping insight", task.task_type)
            return ExecutorResult(success=True, content="", insights=[])

        # Low-information gate: skip outputs that say nothing actionable
        if _LOW_INFO_PATTERNS.search(content) and len(content) < 200:
            logger.info("Surplus task %s produced low-information output — skipping", task.task_type)
            return ExecutorResult(success=True, content="", insights=[])

        # Post to Telegram surplus topic
        if self._topic_manager and content:
            await self._post_to_telegram(task, content)

        model = result.model_id or result.provider_used or "unknown"
        return ExecutorResult(
            success=True,
            content=content,
            insights=[{
                "content": content,
                "source_task_type": task.task_type,
                "generating_model": model,
                "drive_alignment": task.drive_alignment,
                "confidence": 0.5,
            }],
        )

    async def _build_prompt(self, task: SurplusTask) -> str:
        """Build task-specific prompt with relevant context."""
        template = _TASK_PROMPTS.get(task.task_type)
        if template is None:
            # Generic fallback for unmapped task types
            return (
                f"You are performing a '{task.task_type}' analysis for an "
                f"autonomous AI system.\n\n"
                f"Provide a brief, actionable assessment.  "
                f"Respond in plain text (2-4 sentences)."
            )

        context = await self._gather_context(task)

        # Inject previous pipeline step output into context if present
        if task.payload and task.task_type != TaskType.ANTICIPATORY_RESEARCH:
            try:
                payload_data = json.loads(task.payload)
                prev = payload_data.get("previous_output")
                if prev:
                    context = (
                        f"## Previous Step Output\n{prev}\n\n"
                        f"## Additional Context\n{context}"
                    )
            except (ValueError, TypeError):
                pass

        if task.task_type == TaskType.INFRASTRUCTURE_MONITOR:
            return template.format(signals=context)
        return template.format(context=context)

    async def _gather_context(self, task: SurplusTask) -> str:
        """Gather relevant context for the task type."""
        parts: list[str] = []

        # For infrastructure monitor: get latest signals
        if task.task_type == TaskType.INFRASTRUCTURE_MONITOR:
            try:
                from genesis.db.crud import awareness_ticks
                last_tick = await awareness_ticks.last_tick(self._db)
                if last_tick and last_tick.get("signals_json"):
                    signals = json.loads(last_tick["signals_json"])
                    for s in signals:
                        name = s.get("name", "?")
                        value = s.get("value", "?")
                        parts.append(f"- {name}: {value}")
            except Exception:
                parts.append("(Signal data unavailable)")
            return "\n".join(parts) if parts else "(No recent signals)"

        # Pipeline step 1: call site activity + cost aggregation
        if task.task_type == TaskType.PROMPT_REVIEW_CATALOG:
            try:
                cursor = await self._db.execute(
                    "SELECT call_site_id, last_run_at, provider_used, "
                    "model_id, input_tokens, output_tokens, success "
                    "FROM call_site_last_run "
                    "ORDER BY last_run_at DESC LIMIT 20"
                )
                rows = await cursor.fetchall()
                if rows:
                    parts.append("## Call Site Activity (most recent run per site)")
                    for r in rows:
                        cs = r[0] or "?"
                        last = (r[1] or "?")[:19]
                        provider = r[2] or "?"
                        model = r[3] or "?"
                        in_tok = r[4] or 0
                        out_tok = r[5] or 0
                        ok = "OK" if r[6] else "FAIL"
                        parts.append(
                            f"- {cs}: {last} | {provider}/{model} | "
                            f"in={in_tok} out={out_tok} | {ok}"
                        )
            except Exception:
                logger.warning(
                    "_gather_context failed for %s (call_site_last_run)",
                    task.task_type, exc_info=True,
                )
                parts.append("(Call site activity data unavailable)")

            try:
                cursor = await self._db.execute(
                    "SELECT json_extract(metadata, '$.call_site') as call_site, "
                    "COUNT(*) as cnt, SUM(cost_usd) as total_cost, "
                    "AVG(input_tokens) as avg_in, AVG(output_tokens) as avg_out "
                    "FROM cost_events "
                    "WHERE created_at > datetime('now', '-7 days') "
                    "AND json_extract(metadata, '$.call_site') IS NOT NULL "
                    "GROUP BY json_extract(metadata, '$.call_site') "
                    "ORDER BY cnt DESC LIMIT 15"
                )
                rows = await cursor.fetchall()
                if rows:
                    parts.append("\n## Cost Summary (last 7 days, by call site)")
                    for r in rows:
                        cs = r[0] or "?"
                        cnt = r[1]
                        cost = r[2] or 0.0
                        avg_in = int(r[3] or 0)
                        avg_out = int(r[4] or 0)
                        parts.append(
                            f"- {cs}: {cnt} calls, ${cost:.4f}, "
                            f"avg_in={avg_in}, avg_out={avg_out}"
                        )
            except Exception:
                logger.warning(
                    "_gather_context failed for %s (cost_events)",
                    task.task_type, exc_info=True,
                )
                parts.append("(Cost data unavailable)")

            return "\n".join(parts) if parts else "(No call site data available)"

        # Pipeline step 2: recent surplus insight samples
        if task.task_type == TaskType.PROMPT_REVIEW_SAMPLE:
            try:
                cursor = await self._db.execute(
                    "SELECT source_task_type, content, generating_model, "
                    "confidence, promotion_status, created_at "
                    "FROM surplus_insights "
                    "WHERE created_at > datetime('now', '-7 days') "
                    "ORDER BY created_at DESC LIMIT 15"
                )
                rows = await cursor.fetchall()
                if rows:
                    parts.append("## Recent Surplus Outputs (last 7 days)")
                    for r in rows:
                        task_type = r[0] or "?"
                        content = (r[1] or "")[:300]
                        model = r[2] or "?"
                        conf = r[3] or 0.0
                        status = r[4] or "?"
                        parts.append(
                            f"- [{task_type}] model={model} conf={conf:.2f} "
                            f"status={status}\n  {content}"
                        )
            except Exception:
                logger.warning(
                    "_gather_context failed for %s (surplus_insights)",
                    task.task_type, exc_info=True,
                )
                parts.append("(Surplus output data unavailable)")
            # Fall through to also gather standard observations below

        # Research pipeline step 2: fetch web results from step 1 queries
        if task.task_type == TaskType.ANTICIPATORY_RESEARCH and task.payload:
            try:
                payload_data = json.loads(task.payload)
                prev_output = payload_data.get("previous_output", "")
                if prev_output:
                    queries = _parse_search_queries(prev_output)
                    if queries:
                        search_results = await _fetch_search_results(queries)
                        if search_results:
                            parts.append("## Web Search Results")
                            parts.append(search_results)
                        else:
                            parts.append("## Web Search Results\n(No results found)")
            except Exception:
                logger.warning("Research web fetch failed", exc_info=True)
                parts.append("(Web search unavailable)")
            # Fall through to also gather standard observations

        # Cognitive tasks: inject essential knowledge + user model
        if task.task_type in _COGNITIVE_TASK_TYPES:
            ek = _read_essential_knowledge()
            if ek:
                parts.append("## System Context (current state & priorities)")
                parts.append(ek)

            if task.task_type in (TaskType.BRAINSTORM_USER, TaskType.RESEARCH_QUERY_GEN):
                user_model = await _read_user_model_compact(self._db)
                if user_model:
                    parts.append("\n## User Profile")
                    parts.append(user_model)

        # For analytical tasks: recent observations + basic stats
        try:
            recent_obs = await observations.query(
                self._db, resolved=False, limit=10,
            )
            if recent_obs:
                parts.append("## Recent Unresolved Observations")
                for obs in recent_obs[:7]:
                    content = obs.get("content", "")[:200]
                    parts.append(
                        f"- [{obs.get('type', '?')}] {obs.get('created_at', '?')}: {content}"
                    )
        except Exception:
            pass

        # Prior findings for this specific task type (dedup context)
        try:
            past_findings = await observations.query(
                self._db, type=task.task_type, resolved=False, limit=5,
            )
            if past_findings:
                parts.append("\n## Previous Findings (avoid re-discovering)")
                for obs in past_findings:
                    parts.append(f"- {obs.get('content', '')[:150]}")
        except Exception:
            pass

        return "\n".join(parts) if parts else "(No additional context available)"

    async def _post_to_telegram(self, task: SurplusTask, content: str) -> None:
        """Post surplus insight to the surplus Telegram topic."""
        try:
            from html import escape

            label = task.task_type.replace("_", " ").title()
            text = (
                f"<b>Surplus: {escape(label)}</b>\n\n"
                f"{escape(content[:2000])}"
            )
            await self._topic_manager.send_to_category("surplus", text)
            logger.info("Posted surplus insight to Telegram (task=%s)", task.task_type)
        except Exception:
            logger.warning("Failed to post surplus insight to Telegram", exc_info=True)
