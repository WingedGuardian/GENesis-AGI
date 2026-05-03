"""Deep research module for task executor blocker resolution.

Provides two layers of investigation when a task step fails:

1. **Inline due diligence** — quick parallel web search + memory recall.
   The "pull out your phone" step: fast, lightweight, direct API calls.
   Returns context string if relevant results found, None otherwise.

2. **Research session** — full CC session invocation with research profile.
   Iterates on the problem with web search, memory, documentation reading.
   Returns a ResearchResult with either an approach or concrete blockers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from genesis.autonomy.executor.types import ResearchResult
from genesis.cc.types import CCInvocation, CCModel, CCOutput, EffortLevel

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_RESEARCH_PROMPT = _PROMPT_DIR / "research_session.md"

# Call site for the due diligence triage LLM call
_DD_CALL_SITE = "autonomous_executor_reasoning"


class DeepResearcherImpl:
    """Two-layer research: inline due diligence + full CC research session."""

    def __init__(
        self,
        *,
        db: Any,
        retriever: Any | None = None,
        router: Any | None = None,
        invoker: Any | None = None,
        event_bus: Any | None = None,
        web_searcher: Any | None = None,
    ) -> None:
        self._db = db
        self._retriever = retriever
        self._router = router
        self._invoker = invoker
        self._event_bus = event_bus
        self._web_searcher = web_searcher

    # ─── Layer 1: Inline Due Diligence ───────────────────��──────────────────

    async def inline_due_diligence(self, step: dict, error: str) -> str | None:
        """Quick web+memory check. Returns context string or None.

        This is the 'pull out your phone' step ��� fast, lightweight,
        runs BEFORE dispatching a full research session.
        """
        if not self._router:
            logger.debug("Due diligence skipped: no router available")
            return None

        query = self._build_search_query(step, error)
        if not query:
            return None

        # Parallel: web search + memory recall
        web_task = self._web_search(query)
        memory_task = self._memory_recall(query)
        web_results, memory_results = await asyncio.gather(
            web_task, memory_task, return_exceptions=True,
        )

        # Collect whatever succeeded
        context_parts: list[str] = []

        if isinstance(web_results, str) and web_results:
            context_parts.append(f"**Web search results:**\n{web_results}")
        elif isinstance(web_results, BaseException):
            logger.debug("Due diligence web search failed: %s", web_results)

        if isinstance(memory_results, str) and memory_results:
            context_parts.append(f"**Memory recall:**\n{memory_results}")
        elif isinstance(memory_results, BaseException):
            logger.debug("Due diligence memory recall failed: %s", memory_results)

        if not context_parts:
            return None

        # Triage: ask LLM if any of these results are relevant
        combined = "\n\n".join(context_parts)
        triage_result = await self._triage_relevance(step, error, combined)
        return triage_result

    # ─── Layer 2: Full Research Session ────────────────���────────────────────

    async def research(
        self,
        step: dict,
        error: str,
        prior_attempts: list[str],
        due_diligence_results: str | None = None,
    ) -> ResearchResult | None:
        """Dispatch a full research CC session.

        Uses CCInvoker.run() (synchronous subprocess) so the executor
        blocks until research completes. The research session has web
        search and memory tools but cannot execute code.
        """
        if not self._invoker:
            logger.warning("Research skipped: no CC invoker available")
            return None

        prompt = self._build_research_prompt(step, error, prior_attempts, due_diligence_results)

        # Build MCP config so the research session has access to Genesis
        # MCP servers (memory recall, web search tools).
        mcp_config = self._build_mcp_config()

        invocation = CCInvocation(
            prompt=prompt,
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            system_prompt=None,  # Uses default SOUL.md identity
            append_system_prompt=True,
            mcp_config=mcp_config,
            timeout_s=1800,  # 30 min max for research
            skip_permissions=True,
            disallowed_tools=[
                "Bash", "Edit", "Write", "NotebookEdit",
                "mcp__genesis-health__task_submit",
                "mcp__genesis-health__settings_update",
                "mcp__genesis-health__direct_session_run",
                "mcp__genesis-outreach__outreach_send",
                "mcp__genesis-outreach__outreach_send_and_wait",
                "mcp__genesis-health__module_call",
            ],
        )

        logger.info(
            "Research session dispatching for step %s (error: %s...)",
            step.get("idx", "?"), error[:80],
        )

        try:
            output: CCOutput = await self._invoker.run(invocation)
        except Exception:
            logger.exception("Research session failed")
            return ResearchResult(
                found=False,
                clues="Research session crashed before producing results",
                concrete_blockers=["Research infrastructure failure"],
            )

        return self._parse_research_output(output)

    def _build_mcp_config(self) -> str | None:
        """Build MCP config for research sessions (reflection profile)."""
        try:
            from genesis.cc.session_config import SessionConfigBuilder

            builder = SessionConfigBuilder()
            return builder.build_mcp_config(profile="reflection")
        except Exception:
            logger.debug("Could not build MCP config, session will use project defaults")
            return None

    # ─── Private Helpers ───────────────────────────────────────��────────────

    def _build_search_query(self, step: dict, error: str) -> str:
        """Build a concise search query from step context and error."""
        parts: list[str] = []
        desc = step.get("description", step.get("title", ""))
        if desc:
            parts.append(desc[:100])

        # Extract the most informative part of the error
        error_lines = error.strip().splitlines()
        if error_lines:
            # Use last meaningful line (usually the actual error message)
            for line in reversed(error_lines):
                stripped = line.strip()
                if stripped and len(stripped) > 10:
                    parts.append(stripped[:150])
                    break

        return " ".join(parts)[:250] if parts else ""

    async def _web_search(self, query: str) -> str:
        """Run web search and format results."""
        if not self._web_searcher:
            return ""

        try:
            response = await self._web_searcher.search(query, max_results=5)
            if response.error or not response.results:
                return ""
            lines = []
            for r in response.results[:5]:
                lines.append(f"- [{r.title}]({r.url}): {r.snippet[:200]}")
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("Web search error in due diligence: %s", exc)
            return ""

    async def _memory_recall(self, query: str) -> str:
        """Run memory recall and format results."""
        if not self._retriever:
            return ""

        try:
            results = await self._retriever.recall(
                query, limit=5, wing="learning",
            )
            if not results:
                return ""
            lines = []
            for r in results[:5]:
                content = r.content[:200] if hasattr(r, "content") else str(r)[:200]
                lines.append(f"- {content}")
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("Memory recall error in due diligence: %s", exc)
            return ""

    async def _triage_relevance(
        self, step: dict, error: str, combined_results: str,
    ) -> str | None:
        """Ask the LLM: are these search results relevant to this error?"""
        desc = step.get("description", step.get("title", "unknown step"))
        prompt = (
            f"A task step failed. Step: {desc}\n"
            f"Error: {error[:500]}\n\n"
            f"The following information was found via quick search:\n\n"
            f"{combined_results}\n\n"
            f"Are any of these results relevant to resolving this error? "
            f"If yes, summarize the key insight in 2-3 sentences that would "
            f"help resolve the error. If nothing is relevant, respond with "
            f"exactly: NOT_RELEVANT"
        )

        try:
            result = await self._router.route_call(
                _DD_CALL_SITE,
                [{"role": "user", "content": prompt}],
            )
            text = (result.content if hasattr(result, "content") else str(result)) or ""
            if not text or "NOT_RELEVANT" in text:
                return None
            return text.strip()[:2000]
        except Exception:
            logger.exception("Due diligence triage LLM call failed")
            return None

    def _build_research_prompt(
        self,
        step: dict,
        error: str,
        prior_attempts: list[str],
        due_diligence_results: str | None,
    ) -> str:
        """Build the full research session prompt from template."""
        try:
            template = _RESEARCH_PROMPT.read_text()
        except FileNotFoundError:
            template = (
                "Investigate this blocker and produce a JSON result.\n"
                "Step: {{step_description}}\nError: {{error_text}}\n"
                "Prior attempts: {{prior_attempts}}\n"
                "Due diligence: {{due_diligence_results}}"
            )

        desc = step.get("description", step.get("title", "unknown step"))
        attempts_text = "\n".join(
            f"- {a}" for a in prior_attempts
        ) if prior_attempts else "None"
        dd_text = due_diligence_results or "No prior research results"

        return (
            template
            .replace("{{step_description}}", desc)
            .replace("{{error_text}}", error[:3000])
            .replace("{{prior_attempts}}", attempts_text)
            .replace("{{due_diligence_results}}", dd_text)
        )

    def _parse_research_output(self, output: CCOutput) -> ResearchResult:
        """Parse CC session output into a ResearchResult."""
        text = output.text if hasattr(output, "text") else ""
        if not text:
            text = output.result if hasattr(output, "result") else str(output)

        session_id = getattr(output, "session_id", None)

        # Try to find JSON block in output
        parsed = self._extract_json_from_text(text)
        if parsed:
            return ResearchResult(
                found=bool(parsed.get("found", False)),
                approach=parsed.get("approach"),
                sources=parsed.get("sources", []),
                clues=parsed.get("clues"),
                concrete_blockers=parsed.get("concrete_blockers", []),
                session_id=session_id,
            )

        # Fallback: treat entire output as clues
        logger.warning("Research session did not produce parseable JSON output")
        return ResearchResult(
            found=False,
            clues=text[:2000],
            concrete_blockers=["Research session output was not parseable"],
            session_id=session_id,
        )

    @staticmethod
    def _extract_json_from_text(text: str) -> dict | None:
        """Extract JSON object from text, searching for ```json blocks first."""
        # Try fenced JSON block
        import re

        json_block = re.search(r"```json\s*\n(.*?)\n\s*```", text, re.DOTALL)
        if json_block:
            try:
                return json.loads(json_block.group(1))
            except json.JSONDecodeError:
                pass

        # Try reverse search for last JSON object
        for i in range(len(text) - 1, -1, -1):
            if text[i] == "}":
                # Find matching opening brace
                depth = 0
                for j in range(i, -1, -1):
                    if text[j] == "}":
                        depth += 1
                    elif text[j] == "{":
                        depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[j : i + 1])
                        except json.JSONDecodeError:
                            break
                break

        return None
