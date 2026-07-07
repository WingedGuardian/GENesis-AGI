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
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from genesis.cc import AgentProvider

logger = logging.getLogger(__name__)

# 27_pre_execution_assessment — sanity-checks proposed task execution plans.
# Also used in autonomy/executor/review.py:30 (_CALL_SITE_PLAN).
_CALL_SITE = "27_pre_execution_assessment"

_VALID_STEP_TYPES = frozenset({
    "research", "code", "analysis", "synthesis", "verification", "external",
    "bash", "test", "git",  # deterministic — run shell commands, no CC session
})
_VALID_COMPLEXITIES = frozenset({"low", "medium", "high"})
_MAX_STEPS = 8

_DECOMPOSE_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "identity" / "TASK_DECOMPOSE.md"
)

# --- Deliverable-builder v2: deterministic terminal-step append --------------
# When a task plan declares a "## Deliverable Frame" section, the executor must
# render the result through the deliverable-builder skill. The LLM decomposer is
# hinted to add that step (TASK_DECOMPOSE.md), but we GUARANTEE it in code so a
# framed task can never silently ship raw output if the model omits it.
_DELIVERABLE_SKILL = "deliverable-builder"
# Matches a markdown heading "Deliverable Frame" at any level (not prose mentions).
_FRAME_RE = re.compile(r"(?im)^#{1,6}\s*deliverable\s+frame\b")
# Exact text _validate_steps uses for its auto-appended generic verification step.
_AUTO_VERIFY_DESC = "Verify deliverable against success criteria"
_DELIVERABLE_STEP_DESC = (
    "Produce the final send-ready deliverable using the deliverable-builder skill. "
    "The skill copy injected into your resources is TRUNCATED, and the `Skill` tool will "
    "NOT find it (this step runs outside the project). You MUST first Read the COMPLETE "
    "skill — SKILL.md and its references/ — from the absolute skill directory shown in your "
    "injected resources (the '### Skill: deliverable-builder (full skill dir: ...)' line). "
    "Read the '## Deliverable Frame' section of the task plan for the format, visual_style, "
    "authenticity_target, audience, and acceptance criteria. Run the full pipeline "
    "(structure -> voice -> anti-slop -> render to the framed format). The `Task` subagent "
    "tool is NOT available here, so run Gate-2 IN-SESSION: re-open your rendered file (Bash "
    "pdftotext/pdfinfo, or `python -m fitz`) and verify it against the acceptance criteria "
    "yourself. Emit two artifacts: the rendered deliverable file and a qa_summary.md "
    "capturing the Gate-2 verdict + any assumptions. Return a compact result: the rendered "
    "file path plus a one-line PASS/FAIL. Apply anti-slop in-session before rendering (do not "
    "rely on loading other skills): the artifact MUST have zero spaced em-dashes (' — ') — the "
    "#1 AI tell.\n\n"
    "The FULL task plan is appended below as your source of truth (step prompts do not "
    "otherwise include it): read its '## Deliverable Frame' for format / visual_style / "
    "authenticity_target / audience / acceptance, and its '## Requirements' + "
    "'## Success Criteria' for the substance to produce."
)


# Shell constructs an exec-only runner cannot execute. Operators/expansions
# are substring-matched; builtins and env prefixes are matched on the first
# token. Interpreter prefixes (bash/sh/python/...) are pulled from the
# runner's own blocklist (lazy import — executor package imports back into
# this module via engine.py, so a top-level import would cycle).
_SHELL_OPERATORS: tuple[str, ...] = (
    "&&", "||", ";", "|", ">", "<", "$(", "`", "\n",
)
_SHELL_BUILTINS: frozenset[str] = frozenset({
    "source", ".", "cd", "export", "set", "unset", "alias", "eval", "exec",
})


def _shell_incompatible(command: str) -> str | None:
    """Return why *command* cannot run under the exec-only deterministic
    runner, or None if it can.

    The runner uses ``create_subprocess_exec`` + ``shlex.split`` — no shell.
    Shell operators, builtins (``source``), ``VAR=x`` env prefixes, and
    interpreter invocations (which the runner blocks at execution time
    anyway) all need a CC session instead.
    """
    from genesis.autonomy.executor.deterministic import _INTERPRETER_PREFIXES

    for op in _SHELL_OPERATORS:
        if op in command:
            return f"shell operator {op!r}"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "unparseable quoting"
    if not tokens:
        return "empty command"
    head = tokens[0]
    if head in _SHELL_BUILTINS:
        return f"shell builtin {head!r}"
    if "=" in head:
        return f"env-var prefix {head!r}"
    if head.rsplit("/", 1)[-1] in _INTERPRETER_PREFIXES:
        return f"interpreter {head!r} (blocked by the runner)"
    return None


def has_deliverable_frame(plan_content: str) -> bool:
    """True if the plan declares a ``## Deliverable Frame`` heading.

    This is the single v2 trigger — used by the decomposer to attach the
    deliverable-builder step AND by the executor to route delivery. It keys on
    ``plan_content`` (always available) so it survives task resume, where the
    reconstructed step dicts lack the ``skills`` key.
    """
    return bool(_FRAME_RE.search(plan_content or ""))


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
        invoker: AgentProvider | None = None,
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
        """Decompose the plan into steps, guaranteeing a terminal
        deliverable-builder step when the plan declares a Deliverable Frame.

        The LLM decomposition (``_decompose_raw``) is the primary path; the
        deterministic append (``_ensure_deliverable_step``) is a backstop that
        runs on EVERY decomposition outcome — validated, single-step fallback,
        or otherwise — so a framed task can never ship raw output because the
        model forgot the render step. No-op for non-deliverable tasks.
        """
        steps = await self._decompose_raw(plan_content, task_description)
        return self._ensure_deliverable_step(steps, plan_content)

    async def _decompose_raw(
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
            expect_output=True,  # silent-cap detection (decomposition needs a step list)
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
        _DETERMINISTIC_TYPES = frozenset({"bash", "test", "git"})

        for i, step in enumerate(steps[:_MAX_STEPS]):
            step_type = str(step.get("type", "code")).lower()
            if step_type not in _VALID_STEP_TYPES:
                step_type = "code"

            # Deterministic steps MUST have a command field;
            # fall back to "code" if missing so the CC session can
            # figure out what to do from the description.
            command = step.get("command", "")
            if step_type in _DETERMINISTIC_TYPES and not command:
                logger.warning(
                    "Step %d has deterministic type %r but no command — "
                    "falling back to 'code'",
                    i, step_type,
                )
                step_type = "code"

            # The deterministic runner is exec-only (create_subprocess_exec,
            # no shell): shell operators, builtins, env prefixes, and
            # interpreter invocations are guaranteed to fail at runtime and
            # burn a recovery cycle (V0 canary: 'source venv && pytest' and
            # 'git add X && git commit' both died instantly and were rerouted
            # to paid CC sessions). Downgrade them to 'code' upfront.
            if step_type in _DETERMINISTIC_TYPES and command:
                reason = _shell_incompatible(str(command))
                if reason is not None:
                    logger.warning(
                        "Step %d command needs a shell (%s) — the "
                        "deterministic runner is exec-only; falling back "
                        "to 'code': %r",
                        i, reason, command,
                    )
                    step_type = "code"

            complexity = str(step.get("complexity", "medium")).lower()
            if complexity not in _VALID_COMPLEXITIES:
                complexity = "medium"

            deps = step.get("dependencies", [])
            if not isinstance(deps, list):
                deps = []
            # Filter deps to valid prior indices only (acyclic)
            deps = [d for d in deps if isinstance(d, int) and 0 <= d < i]

            entry: dict = {
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
            }

            # Preserve command field for deterministic steps
            if command and step_type in _DETERMINISTIC_TYPES:
                entry["command"] = str(command)

            validated.append(entry)

        # Ensure last step is verification (append if not)
        if (
            validated
            and validated[-1]["type"] != "verification"
            and len(validated) < _MAX_STEPS
        ):
            validated.append({
                "idx": len(validated),
                "type": "verification",
                # Same constant _ensure_deliverable_step strips by — keep coupled.
                "description": _AUTO_VERIFY_DESC,
                "required_tools": [],
                "complexity": "medium",
                "dependencies": [len(validated) - 1],
                "skills": [],
                "procedures": [],
                "mcp_guidance": [],
            })

        return validated

    def _ensure_deliverable_step(
        self, steps: list[dict], plan_content: str
    ) -> list[dict]:
        """Guarantee a terminal deliverable-builder step for framed tasks.

        No-op unless the plan declares a ``## Deliverable Frame`` heading. When
        it does, ensure the LAST step is a deliverable-builder synthesis step —
        its artifact IS the deliverable and it runs the skill's own Gate-2, so
        nothing may run after it. Idempotent: if the LLM already placed the step
        last, leave it; otherwise strip the generic auto-verification tail (which
        ``_validate_steps`` adds) and append the canonical step.
        """
        if not has_deliverable_frame(plan_content):
            return steps

        result = list(steps)

        # Strip a trailing generic auto-verification so it can't sit AFTER the
        # deliverable step (which must be terminal).
        if (
            result
            and result[-1].get("type") == "verification"
            and result[-1].get("description") == _AUTO_VERIFY_DESC
        ):
            result = result[:-1]

        # Idempotent: if a deliverable-builder step already exists ANYWHERE,
        # don't append a second (which would run the skill twice). If the LLM
        # mis-placed it (not terminal), leave it — its artifact is still
        # captured at synthesis — but warn, since terminal is the invariant.
        if any(_DELIVERABLE_SKILL in (s.get("skills") or []) for s in result):
            if _DELIVERABLE_SKILL not in (result[-1].get("skills") or []):
                logger.warning(
                    "Plan has a deliverable-builder step that is not terminal; "
                    "leaving as-is (artifact still captured at synthesis)",
                )
            return result

        new_idx = len(result)
        if new_idx >= _MAX_STEPS:
            # The deliverable step is essential; allow one over the soft cap
            # rather than dropping real work. Warn so it's visible in ops.
            logger.warning(
                "Appending deliverable-builder step beyond _MAX_STEPS=%d "
                "(deliverable frame present)",
                _MAX_STEPS,
            )

        # Embed the full plan in the step description: build_step_prompt does
        # NOT pass the plan to steps, so this is the only way the frame +
        # requirements reach the deliverable session.
        description = _DELIVERABLE_STEP_DESC
        if plan_content:
            description = (
                f"{_DELIVERABLE_STEP_DESC}\n\n"
                f"## Full task plan (your source of truth)\n\n{plan_content}"
            )
        result.append({
            "idx": new_idx,
            "type": "synthesis",
            "description": description,
            "required_tools": [],
            "complexity": "high",
            "dependencies": [new_idx - 1] if new_idx > 0 else [],
            "skills": [_DELIVERABLE_SKILL],
            "procedures": [],
            "mcp_guidance": [],
        })
        return result

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
            "skills": [],
            "procedures": [],
            "mcp_guidance": [],
        }]
