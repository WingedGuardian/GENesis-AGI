"""Task decomposer --- breaks a plan document into executable steps.

Uses CC invoker (Sonnet) as primary path for decomposition, falling back
to call site 27 (pre_execution_assessment) via the router. Follows the
same pattern as TaskReviewer: CC invoker primary, route_call fallback.

Falls back to a single-step plan if the LLM response is unparseable.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from genesis.cc.invoker import CCInvoker

logger = logging.getLogger(__name__)

# 27_pre_execution_assessment — sanity-checks proposed task execution plans.
# Also used in autonomy/executor/review.py:30 (_CALL_SITE_PLAN).
_CALL_SITE = "27_pre_execution_assessment"

_VALID_STEP_TYPES = frozenset({
    "research", "code", "analysis", "synthesis", "verification", "external",
})
_VALID_COMPLEXITIES = frozenset({"low", "medium", "high"})
_MAX_STEPS = 8

_DECOMPOSE_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "identity" / "TASK_DECOMPOSE.md"
)


class _Router(Protocol):
    async def route_call(
        self, call_site_id: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Any: ...


class TaskDecomposer:
    """Decompose a task plan into executable steps via LLM."""

    def __init__(
        self,
        *,
        router: _Router,
        invoker: CCInvoker | None = None,
        db: Any | None = None,
        memory_store: Any | None = None,
        retriever: Any | None = None,
    ) -> None:
        self._router = router
        self._invoker = invoker
        self._db = db
        self._memory_store = memory_store
        self._retriever = retriever

    async def decompose(
        self,
        plan_content: str,
        task_description: str,
    ) -> list[dict]:
        """Decompose *plan_content* into a list of step dicts.

        Returns a list of validated step dicts, each with keys:
        idx, type, description, required_tools, complexity, dependencies.

        Prefers CC invoker (Sonnet) for reliable auth. Falls back to
        route_call if invoker unavailable or fails, then to a single
        verification step on total failure.
        """
        # Gather resource inventory for the decomposer
        resource_appendix = ""
        if any([self._db, self._retriever, self._memory_store]):
            try:
                from genesis.autonomy.executor.resources import (
                    gather_resource_inventory,
                )

                resource_appendix = await gather_resource_inventory(
                    self._db, self._memory_store, self._retriever,
                    task_description,
                )
            except Exception:
                logger.debug(
                    "Resource inventory gathering failed, proceeding without",
                    exc_info=True,
                )

        prompt = self._build_prompt(plan_content, task_description, resource_appendix)

        # Primary path: CC invoker (Sonnet)
        if self._invoker is not None:
            try:
                content = await self._decompose_via_invoker(prompt)
                if content is not None:
                    steps = self._parse_response(content)
                    if steps:
                        return self._validate_steps(steps)
                    logger.warning(
                        "CC invoker decomposition returned unparseable response, "
                        "falling back to route_call",
                    )
            except Exception:
                logger.warning(
                    "CC invoker decomposition failed, falling back to route_call",
                    exc_info=True,
                )

        # Fallback: route_call via call site 27
        messages = [{"role": "user", "content": prompt}]
        try:
            result = await self._router.route_call(_CALL_SITE, messages)
        except Exception:
            logger.warning(
                "Decomposer route_call raised, falling back to single step",
                exc_info=True,
            )
            return self._single_step_fallback(task_description)

        if not result.success or not result.content:
            logger.warning(
                "Decomposer route_call failed (success=%s), falling back to single step",
                getattr(result, "success", None),
            )
            return self._single_step_fallback(task_description)

        steps = self._parse_response(result.content)
        if not steps:
            logger.warning("Decomposer could not parse response, falling back to single step")
            return self._single_step_fallback(task_description)

        return self._validate_steps(steps)

    async def _decompose_via_invoker(self, prompt: str) -> str | None:
        """Run decomposition via CC invoker (Sonnet). Returns text or None."""
        from genesis.cc.types import CCInvocation, CCModel, EffortLevel

        invocation = CCInvocation(
            prompt=prompt,
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            timeout_s=300,
            skip_permissions=True,
        )
        output = await self._invoker.run(invocation)
        if output.is_error:
            logger.warning(
                "CC invoker decomposition returned error: %s",
                output.error_message or output.text[:200],
            )
            return None
        return output.text

    def _build_prompt(
        self,
        plan_content: str,
        task_description: str,
        resource_appendix: str = "",
    ) -> str:
        """Build the decomposition prompt from the identity template + plan."""
        template = ""
        try:
            template = _DECOMPOSE_PROMPT_PATH.read_text(encoding="utf-8")
        except OSError:
            logger.error(
                "Failed to load decomposition prompt from %s",
                _DECOMPOSE_PROMPT_PATH,
                exc_info=True,
            )

        parts = []
        if template:
            parts.append(template)
            parts.append("")

        parts.extend([
            "## Task Description",
            task_description,
            "",
            "## Plan Document",
            plan_content,
            "",
        ])

        if resource_appendix:
            parts.extend([
                "## Available Resources",
                resource_appendix,
                "",
            ])

        parts.append("Respond with ONLY the JSON array of steps. No other text.")
        return "\n".join(parts)

    def _parse_response(self, content: str) -> list[dict] | None:
        """Parse the LLM response into a list of step dicts."""
        text = content.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first and last fence lines
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        data: Any = None
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            data = json.loads(text)

        if not isinstance(data, list) or not data:
            return None

        # Each element must be a dict with at least idx and type
        for item in data:
            if not isinstance(item, dict):
                return None
            if "idx" not in item or "type" not in item:
                return None

        return data

    def _validate_steps(self, steps: list[dict]) -> list[dict]:
        """Validate and normalize step dicts. Clamp to MAX_STEPS."""
        validated: list[dict] = []

        for i, step in enumerate(steps[:_MAX_STEPS]):
            step_type = str(step.get("type", "code")).lower()
            if step_type not in _VALID_STEP_TYPES:
                step_type = "code"

            complexity = str(step.get("complexity", "medium")).lower()
            if complexity not in _VALID_COMPLEXITIES:
                complexity = "medium"

            deps = step.get("dependencies", [])
            if not isinstance(deps, list):
                deps = []
            # Filter deps to valid prior indices only (acyclic)
            deps = [d for d in deps if isinstance(d, int) and 0 <= d < i]

            validated.append({
                "idx": i,
                "type": step_type,
                "description": str(step.get("description", f"Step {i}")),
                "required_tools": (
                    step.get("required_tools")
                    if isinstance(step.get("required_tools"), list)
                    else []
                ),
                "complexity": complexity,
                "dependencies": deps,
                "skills": (
                    step.get("skills")
                    if isinstance(step.get("skills"), list)
                    else []
                ),
                "procedures": (
                    step.get("procedures")
                    if isinstance(step.get("procedures"), list)
                    else []
                ),
                "mcp_guidance": (
                    step.get("mcp_guidance")
                    if isinstance(step.get("mcp_guidance"), list)
                    else []
                ),
            })

        # Ensure last step is verification (append if not)
        if (
            validated
            and validated[-1]["type"] != "verification"
            and len(validated) < _MAX_STEPS
        ):
            validated.append({
                "idx": len(validated),
                "type": "verification",
                "description": "Verify deliverable against success criteria",
                "required_tools": [],
                "complexity": "medium",
                "dependencies": [len(validated) - 1],
                "skills": [],
                "procedures": [],
                "mcp_guidance": [],
            })

        return validated

    def _single_step_fallback(self, task_description: str) -> list[dict]:
        """Return a single verification step as fallback."""
        return [{
            "idx": 0,
            "type": "verification",
            "description": (
                f"Execute and verify the task: {task_description}. "
                "The plan could not be decomposed into individual steps."
            ),
            "required_tools": [],
            "complexity": "high",
            "dependencies": [],
        }]
