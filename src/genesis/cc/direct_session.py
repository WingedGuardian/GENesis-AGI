"""DirectSessionRunner — directed background CC session spawner.

"Run this prompt, report back." No task decomposition, no adversarial
review, no surplus routing. Profile-constrained CC session with
tool-level safety enforcement. Results delivered via Telegram.

Profiles restrict tool access via CC's ``disallowed_tools`` mechanism,
which removes tools from the model's view entirely (validated: CC strips
them from the init tools list).

Design notes (from architect review):
- Uses a *dedicated* CCInvoker instance (not the shared one) because
  ``_active_proc`` is not concurrency-safe under Semaphore(2).
- Accesses ``outreach_pipeline`` lazily via runtime ref (not injected at
  init time) because outreach init runs after cc_relay in bootstrap.
- Never calls ``should_throttle`` — this is user-invoked work, not
  autonomous background compute. Cost control is the user's decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from genesis.cc.types import (
    CCInvocation,
    CCModel,
    CCOutput,
    EffortLevel,
    SessionType,
    StreamEvent,
    background_session_dir,
)
from genesis.util.tasks import tracked_task

if TYPE_CHECKING:
    from genesis.cc.invoker import CCInvoker
    from genesis.cc.session_config import SessionConfigBuilder
    from genesis.cc.session_manager import SessionManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool profiles — disallowed tools per profile
# ---------------------------------------------------------------------------
# CC removes disallowed tools from the model's view. The model literally
# cannot see or call them. Validated empirically: the init event's tools
# list shrinks by the disallowed count.

_UNIVERSAL_DISALLOW = [
    "Bash",
    "Edit",
    "Write",
    "NotebookEdit",
    "mcp__genesis-health__task_submit",
    "mcp__genesis-health__settings_update",
    "mcp__genesis-health__direct_session_run",  # No recursive spawn
    "mcp__genesis-outreach__outreach_send",
    "mcp__genesis-outreach__outreach_send_and_wait",
    "mcp__genesis-health__module_call",
]

# Composable building blocks for profile disallow lists
_NO_BROWSER_INTERACTION = [
    "mcp__genesis-health__browser_click",
    "mcp__genesis-health__browser_fill",
    "mcp__genesis-health__browser_run_js",
    "mcp__genesis-health__browser_clear_domain",
    "mcp__genesis-health__browser_collaborate",
]

_NO_MEMORY_WRITES = [
    "mcp__genesis-memory__memory_store",
    "mcp__genesis-memory__memory_synthesize",
    "mcp__genesis-memory__memory_extract",
    "mcp__genesis-memory__observation_write",
    "mcp__genesis-memory__observation_resolve",
    "mcp__genesis-memory__knowledge_ingest",
    "mcp__genesis-memory__knowledge_ingest_batch",
    "mcp__genesis-memory__knowledge_ingest_source",
    "mcp__genesis-memory__procedure_store",
    "mcp__genesis-memory__reference_store",
    "mcp__genesis-memory__reference_delete",
    "mcp__genesis-memory__evolution_propose",
]

_NO_FOLLOW_UPS = [
    "mcp__genesis-health__follow_up_create",
]

_NO_OUTREACH_ENGAGEMENT = [
    "mcp__genesis-outreach__outreach_engagement",
    "mcp__genesis-outreach__outreach_preferences",
    "mcp__genesis-outreach__outreach_queue",
]

_NO_RECON_WRITES = [
    "mcp__genesis-recon__recon_store_finding",
    "mcp__genesis-recon__recon_run_model_intelligence",
]

PROFILES: dict[str, list[str]] = {
    "observe": (
        _UNIVERSAL_DISALLOW + _NO_BROWSER_INTERACTION + _NO_MEMORY_WRITES
        + _NO_FOLLOW_UPS + _NO_OUTREACH_ENGAGEMENT + _NO_RECON_WRITES
    ),
    "interact": (
        _UNIVERSAL_DISALLOW + _NO_MEMORY_WRITES
        + _NO_FOLLOW_UPS + _NO_OUTREACH_ENGAGEMENT + _NO_RECON_WRITES
    ),
    "research": (
        _UNIVERSAL_DISALLOW + _NO_BROWSER_INTERACTION
        + _NO_OUTREACH_ENGAGEMENT
    ),
}

VALID_PROFILES = frozenset(PROFILES.keys())


# ---------------------------------------------------------------------------
# Request / Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirectSessionRequest:
    """What to run in the background session."""

    prompt: str
    profile: str = "observe"
    model: CCModel = CCModel.SONNET
    effort: EffortLevel = EffortLevel.HIGH
    system_prompt: str | None = None  # None = SOUL.md identity
    timeout_s: int = 900  # 15 min default
    notify: bool = True
    notify_on_failure_only: bool = False
    source_tag: str = "direct_session"
    caller_context: str | None = None  # "follow_up:<id>", "schedule:<id>"

    def __post_init__(self) -> None:
        if self.profile not in VALID_PROFILES:
            raise ValueError(
                f"Invalid profile {self.profile!r}. "
                f"Must be one of: {', '.join(sorted(VALID_PROFILES))}"
            )


@dataclass
class DirectSessionResult:
    """What the background session produced."""

    session_id: str
    cc_session_id: str = ""
    success: bool = False
    output_text: str = ""
    error: str | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    tools_called: list[dict] = field(default_factory=list)
    model_used: str = ""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class DirectSessionRunner:
    """Spawns profile-constrained background CC sessions.

    Parameters
    ----------
    invoker:
        A *dedicated* CCInvoker instance (not the shared runtime one).
        This avoids the ``_active_proc`` concurrency race under
        ``Semaphore(2)`` (architect finding #5).
    session_manager:
        Shared SessionManager for tracking.
    config_builder:
        Builds system prompts and MCP configs.
    runtime:
        GenesisRuntime reference — used to lazily access
        ``outreach_pipeline`` (which isn't available at init time
        because outreach bootstraps after cc_relay).
    """

    _MAX_CONCURRENT = 2

    def __init__(
        self,
        *,
        invoker: CCInvoker,
        session_manager: SessionManager,
        config_builder: SessionConfigBuilder,
        runtime: object,
    ) -> None:
        self._invoker = invoker
        self._session_manager = session_manager
        self._config_builder = config_builder
        self._rt = runtime
        self._semaphore = asyncio.Semaphore(self._MAX_CONCURRENT)
        self._active: dict[str, asyncio.Task] = {}

    # -- Public API --------------------------------------------------------

    async def spawn(self, request: DirectSessionRequest) -> str:
        """Fire-and-forget. Returns genesis session_id immediately."""
        session = await self._session_manager.create_background(
            session_type=SessionType.BACKGROUND_TASK,
            model=request.model,
            effort=request.effort,
            source_tag=request.source_tag,
            dispatch_mode="direct",
        )
        session_id = session["id"]

        task = tracked_task(
            self._run_session(request, session_id),
            name=f"direct-session-{session_id[:8]}",
        )
        self._active[session_id] = task
        task.add_done_callback(lambda _t: self._active.pop(session_id, None))
        return session_id

    def active_count(self) -> int:
        return len(self._active)

    @staticmethod
    def _summarize_tools(tools_called: list[dict]) -> dict[str, int]:
        """Aggregate tool calls into {name: count} dict."""
        counts: dict[str, int] = {}
        for t in tools_called[:100]:
            name = t.get("name", "unknown")
            counts[name] = counts.get(name, 0) + 1
        return counts

    # -- Internal ----------------------------------------------------------

    async def _run_session(
        self,
        request: DirectSessionRequest,
        session_id: str,
    ) -> DirectSessionResult:
        """Execute a single CC session. Called inside tracked_task."""
        telemetry: list[dict] = []
        start = time.monotonic()

        async def on_event(event: StreamEvent) -> None:
            if event.event_type == "tool_use" and event.tool_name:
                telemetry.append({
                    "name": event.tool_name,
                    "input_preview": (
                        str(event.tool_input)[:200]
                        if event.tool_input else ""
                    ),
                })

        try:
            async with self._semaphore:
                invocation = self._build_invocation(request)
                output: CCOutput = await self._invoker.run_streaming(
                    invocation, on_event=on_event,
                )

            elapsed = time.monotonic() - start
            result = DirectSessionResult(
                session_id=session_id,
                cc_session_id=output.session_id,
                success=not output.is_error,
                output_text=output.text,
                error=output.error_message if output.is_error else None,
                cost_usd=output.cost_usd,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
                duration_s=round(elapsed, 1),
                tools_called=telemetry,
                model_used=output.model_used,
            )

            # Persist result in session metadata (merge, don't overwrite)
            await self._store_result(session_id, request, result)

            await self._session_manager.complete(
                session_id,
                cost_usd=output.cost_usd,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
            )

            if request.notify and not request.notify_on_failure_only:
                await self._notify(request, result, success=True)

            logger.info(
                "Direct session %s completed: %.1fs, $%.4f, %d tools",
                session_id[:8], elapsed, output.cost_usd, len(telemetry),
            )
            return result

        except Exception as exc:
            elapsed = time.monotonic() - start
            error_result = DirectSessionResult(
                session_id=session_id,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_s=round(elapsed, 1),
                tools_called=telemetry,
            )

            # Best-effort: persist failure and notify
            try:
                await self._store_result(session_id, request, error_result)
                await self._session_manager.fail(
                    session_id, reason=str(exc)[:500],
                )
            except Exception:
                logger.error(
                    "Failed to record session %s failure", session_id[:8],
                    exc_info=True,
                )

            if request.notify:
                try:
                    await self._notify(request, error_result, success=False)
                except Exception:
                    logger.error(
                        "Failed to send failure notification for %s",
                        session_id[:8], exc_info=True,
                    )

            logger.error(
                "Direct session %s failed after %.1fs: %s",
                session_id[:8], elapsed, exc,
            )
            raise

    def _build_invocation(self, request: DirectSessionRequest) -> CCInvocation:
        system_prompt = request.system_prompt
        if system_prompt is None:
            # Use the surplus config's system prompt (which loads SOUL.md)
            surplus_config = self._config_builder.build_surplus_config()
            system_prompt = surplus_config.get("system_prompt", "")
        disallowed = PROFILES.get(request.profile, PROFILES["observe"])

        return CCInvocation(
            prompt=request.prompt,
            model=request.model,
            effort=request.effort,
            system_prompt=system_prompt,
            append_system_prompt=True,
            timeout_s=request.timeout_s,
            skip_permissions=True,
            disallowed_tools=disallowed,
            working_dir=background_session_dir(),
        )

    async def _store_result(
        self,
        session_id: str,
        request: DirectSessionRequest,
        result: DirectSessionResult,
    ) -> None:
        """Merge result data into cc_sessions.metadata (read-merge-write)."""
        from genesis.db.crud import cc_sessions

        db = getattr(self._rt, "_db", None)
        if db is None:
            return

        row = await cc_sessions.get_by_id(db, session_id)
        existing = {}
        if row and row.get("metadata"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                existing = json.loads(row["metadata"])

        tool_counts = self._summarize_tools(result.tools_called)

        existing.update({
            "profile": request.profile,
            "caller_context": request.caller_context,
            "output_text": result.output_text[:5000],
            "tools_summary": tool_counts,
            "cc_session_id": result.cc_session_id,
            "error": result.error,
            "model_used": result.model_used,
            "duration_s": result.duration_s,
        })

        await db.execute(
            "UPDATE cc_sessions SET metadata = ? WHERE id = ?",
            (json.dumps(existing), session_id),
        )
        await db.commit()

    async def _notify(
        self,
        request: DirectSessionRequest,
        result: DirectSessionResult,
        *,
        success: bool,
    ) -> None:
        """Send Telegram notification via outreach pipeline."""
        pipeline = getattr(self._rt, "_outreach_pipeline", None)
        if pipeline is None:
            logger.debug("Outreach pipeline not available, skipping notification")
            return

        tool_counts = self._summarize_tools(result.tools_called)
        tools_str = ", ".join(
            f"{n} ({c})" for n, c in sorted(
                tool_counts.items(), key=lambda x: -x[1],
            )[:8]
        ) or "none"

        if success:
            title = "Direct Session Complete"
            preview = result.output_text[:400] if result.output_text else "(no output)"
            body = (
                f"<b>{title}</b>\n\n"
                f"Profile: {request.profile} | "
                f"Model: {result.model_used or request.model} | "
                f"Duration: {result.duration_s:.0f}s | "
                f"Cost: ${result.cost_usd:.4f}\n\n"
                f"{preview}\n\n"
                f"Tools: {tools_str}"
            )
        else:
            title = "Direct Session FAILED"
            body = (
                f"<b>{title}</b>\n\n"
                f"Profile: {request.profile} | "
                f"Model: {request.model} | "
                f"Duration: {result.duration_s:.0f}s\n\n"
                f"Error: {result.error or 'unknown'}\n\n"
                f"Tools before failure: {tools_str}"
            )

        try:
            from genesis.outreach.types import OutreachCategory, OutreachRequest

            await pipeline.submit(OutreachRequest(
                category=OutreachCategory.ALERT,
                topic=f"direct_session_{'ok' if success else 'fail'}",
                context=body,
                salience_score=0.9 if not success else 0.7,
            ))
        except Exception:
            logger.error("Outreach submit failed", exc_info=True)
