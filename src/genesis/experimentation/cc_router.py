"""CC-CLI router — run the experiment harness's generation/judging through the
``claude`` CLI (the Claude Code subscription) instead of a litellm API provider.

Why: the offline harness routes via litellm, but OpenRouter (which fronts the
Claude providers) is out of credits and the direct Anthropic API is off-limits.
The ``claude`` CLI uses the subscription and is the live path Genesis already
uses for deep reflection — so it's both available and representative.

Drop-in: ``route_call(call_site_id, messages) -> StandaloneRoutingResult`` — the
same shape ``StandaloneLiteLLMRouter`` returns, so it works as both the
generation router and the judge's router (``LLMJudgeScorer(router=...)``).
Single-completion only (``-p``); no tools, no session resume.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from genesis.cc.types import (
    VALID_EFFORT_NAMES,
    VALID_MODEL_NAMES,
    CCModel,
    model_supports_effort,
)
from genesis.experimentation.standalone_router import StandaloneRoutingResult

logger = logging.getLogger(__name__)

_VALID_MODELS = VALID_MODEL_NAMES
_VALID_EFFORTS = VALID_EFFORT_NAMES


class CCCliRouter:
    """Invoke ``claude -p`` per call as a single-shot completion provider."""

    def __init__(
        self, model: str = "haiku", *, effort: str | None = None, timeout_s: float = 180.0,
    ):
        m = model.lower().removeprefix("cc-")
        if m not in _VALID_MODELS:
            raise ValueError(f"CCCliRouter model must be one of {_VALID_MODELS}, got {model!r}")
        if effort is not None and effort not in _VALID_EFFORTS:
            raise ValueError(
                f"CCCliRouter effort must be one of {_VALID_EFFORTS} or None, got {effort!r}"
            )
        self._model = m
        self._effort = effort
        self._timeout_s = timeout_s

    async def route_call(
        self, call_site_id: str, messages: list[dict], **_kwargs,
    ) -> StandaloneRoutingResult:
        # **_kwargs: tolerate API-shaped args (temperature, etc.) the judge/harness
        # may pass — the CLI doesn't take them.
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system").strip()
        user = "\n\n".join(m["content"] for m in messages if m.get("role") == "user").strip() or " "

        args = [
            "claude", "-p",
            "--model", self._model,
            "--output-format", "text",
            "--dangerously-skip-permissions",  # non-interactive, no permission prompts
            # Force a PURE single-turn completion: with tools enabled, claude -p
            # goes agentic (explores logs/files) instead of reflecting on the
            # given text — slow + off-task. Disable the built-in tools.
            "--disallowedTools",
            "Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS,WebFetch,WebSearch,Task,"
            "TodoWrite,NotebookEdit,NotebookRead,ExitPlanMode",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',  # no MCP tools
            # Suppress SessionStart/UserPromptSubmit hooks (gstack/genesis inject
            # skill-lists + preambles that contaminate a bare completion).
            "--settings", '{"hooks":{}}',
        ]
        # Effort is opt-in here. Only emit --effort when requested AND the model
        # uses it — Haiku (the default) does not use an effort setting.
        if self._effort and model_supports_effort(CCModel(self._model)):
            args += ["--effort", self._effort]
        if system:
            # Replace (not append) — the variant's system prompt IS the system.
            args += ["--system-prompt", system]

        env = dict(os.environ)
        # Avoid CC-in-CC nesting confusion (mirrors CCInvoker._build_env).
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        # Mark as a Genesis-dispatched session so the SessionStart hooks skip
        # identity/context injection — we want a clean completion on the given
        # text, not Genesis's project context bleeding into the reflection.
        env["GENESIS_CC_SESSION"] = "1"

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            out, err = await asyncio.wait_for(
                proc.communicate(input=user.encode()), timeout=self._timeout_s,
            )
        except TimeoutError:
            logger.warning("cc-cli %s timed out after %ss", self._model, self._timeout_s)
            # Reap the orphaned claude process (else it keeps running + holds a
            # subscription slot). Mirrors CCInvoker's timeout handling.
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return StandaloneRoutingResult(
                success=False, content=None, model_id=self._model,
                provider_used="cc-cli", error="timeout",
            )
        except Exception as exc:  # noqa: BLE001 — subprocess spawn failure → failed result
            logger.warning("cc-cli %s invocation failed", self._model, exc_info=True)
            return StandaloneRoutingResult(
                success=False, content=None, model_id=self._model,
                provider_used="cc-cli", error=str(exc),
            )

        content = out.decode(errors="replace").strip()
        ok = proc.returncode == 0 and bool(content)
        return StandaloneRoutingResult(
            success=ok,
            content=content if ok else None,
            model_id=self._model,
            provider_used="cc-cli",
            error=None if ok else (err.decode(errors="replace")[:300] or f"exit {proc.returncode}"),
        )

    async def close(self) -> None:  # interface parity with StandaloneLiteLLMRouter
        return None
