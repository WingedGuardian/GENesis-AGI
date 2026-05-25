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
from genesis.observability.session_context import set_session_id as _set_obs_session
from genesis.util.tasks import tracked_task

if TYPE_CHECKING:
    from genesis.cc import AgentProvider
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
    "NotebookEdit",
    "mcp__genesis-health__task_submit",
    "mcp__genesis-health__settings_update",
    "mcp__genesis-health__direct_session_run",  # No recursive spawn
    "mcp__genesis-health__module_call",
    # ── Vector store isolation ────────────────────────────────────
    # Background sessions MUST NOT write to Qdrant (episodic_memory
    # or knowledge_base). Episodic memory is exclusively for
    # foreground user interactions. Background findings belong in
    # the session transcript (the deliverable) or in SQLite tables
    # (observations, references, follow-ups) — never in vector stores.
    # Server-side code (ego corrections, reflection output) uses
    # MemoryStore directly and is unaffected by tool-level blocking.
    "mcp__genesis-memory__memory_store",
    "mcp__genesis-memory__memory_synthesize",
    "mcp__genesis-memory__memory_extract",
    # Knowledge ingestion requires explicit user authorization.
    "mcp__genesis-memory__knowledge_ingest",
    "mcp__genesis-memory__knowledge_ingest_batch",
    "mcp__genesis-memory__knowledge_ingest_source",
]

_NO_OUTREACH_SEND = [
    "mcp__genesis-outreach__outreach_send",
    "mcp__genesis-outreach__outreach_send_and_wait",
]

# Composable building blocks for profile disallow lists
_NO_BROWSER_INTERACTION = [
    "mcp__genesis-health__browser_click",
    "mcp__genesis-health__browser_fill",
    "mcp__genesis-health__browser_run_js",
    "mcp__genesis-health__browser_clear_domain",
    "mcp__genesis-health__browser_collaborate",
]

_NO_FILE_WRITE = [
    "Write",  # Moved out of _UNIVERSAL_DISALLOW so interact/research can use it
]

_NO_MEMORY_WRITES = [
    # memory_store/synthesize/extract + knowledge_ingest* are in
    # _UNIVERSAL_DISALLOW (vector store isolation).
    # This list covers SQLite-table writes blocked only for observe.
    "mcp__genesis-memory__observation_write",
    "mcp__genesis-memory__observation_resolve",
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
        _UNIVERSAL_DISALLOW + _NO_FILE_WRITE + _NO_OUTREACH_SEND
        + _NO_BROWSER_INTERACTION + _NO_MEMORY_WRITES + _NO_FOLLOW_UPS
        + _NO_OUTREACH_ENGAGEMENT + _NO_RECON_WRITES
    ),
    "interact": (
        _UNIVERSAL_DISALLOW + _NO_OUTREACH_ENGAGEMENT + _NO_RECON_WRITES
        + ["mcp__genesis-memory__evolution_propose"]
    ),
    "research": (
        _UNIVERSAL_DISALLOW + _NO_OUTREACH_SEND + _NO_BROWSER_INTERACTION
    ),
}

VALID_PROFILES = frozenset(PROFILES.keys())


# ---------------------------------------------------------------------------
# Profile addendum + skill auto-injection
# ---------------------------------------------------------------------------

_MISSION_INJECTION = (
    "If it's digital, it's within reach. Adapt and overcome. "
    "Don't conclude \"impossible\" — conclude \"blocked by X\" and identify what would unblock it."
)

_PROFILE_ADDENDA: dict[str, str] = {
    "interact": f"""

## Session Profile: interact

You have: Write, browser MCP tools, memory MCP tools, outreach send.
You do NOT have: Edit, Bash, NotebookEdit.
Your final message IS your deliverable. Write files to `~/.genesis/output/`.

{_MISSION_INJECTION}
""",
    "research": f"""

## Session Profile: research

You have: Write, memory MCP tools, web tools (web_search, web_fetch).
You do NOT have: Edit, Bash, NotebookEdit, browser tools.
Your final message IS your deliverable. Write files to `~/.genesis/output/`.

{_MISSION_INJECTION}
""",
    "observe": f"""

## Session Profile: observe

You have: memory MCP tools (read-only).
You do NOT have: Write, Edit, Bash, NotebookEdit, browser tools.
Your final message IS your deliverable.

{_MISSION_INJECTION}
""",
}

# Skills auto-injected by profile (always loaded for that profile)
_PROFILE_SKILLS: dict[str, list[str]] = {
    "interact": ["stealth-browser"],
    "research": [],
    "observe": [],
}

# Keyword triggers for content-related skills (scanned against prompt)
_CONTENT_SKILL_TRIGGERS: list[tuple[list[str], list[str]]] = [
    (
        ["publish", "article", "medium", "content", "post", "draft", "blog"],
        ["content-publish", "voice-master"],
    ),
]


def _build_profile_addendum(profile: str) -> str:
    """Return the profile constraint addendum for background sessions."""
    return _PROFILE_ADDENDA.get(profile, _PROFILE_ADDENDA["observe"])


def _resolve_skills(request: DirectSessionRequest) -> list[str]:
    """Determine which skills to inject: explicit > profile + auto-detect."""
    if request.skills is not None:
        return request.skills

    # Start with profile-bound skills
    skills = list(_PROFILE_SKILLS.get(request.profile, []))

    # Scan prompt for keyword triggers
    prompt_lower = request.prompt.lower()
    for keywords, skill_names in _CONTENT_SKILL_TRIGGERS:
        if any(kw in prompt_lower for kw in keywords):
            for name in skill_names:
                if name not in skills:
                    skills.append(name)

    return skills


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
    timeout_s: int = 3600  # 1 hour — model decides when to stop
    notify: bool = True
    notify_on_failure_only: bool = False
    source_tag: str = "direct_session"
    caller_context: str | None = None  # "follow_up:<id>", "schedule:<id>"
    planning_instruction: str | None = None  # opt-in: prepended to prompt
    skills: list[str] | None = None  # explicit skill injection (overrides auto-detect)

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
        invoker: AgentProvider,
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
            profile=request.profile,
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
        _set_obs_session(session_id)
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

            # Feed outcome back to ego proposal if this was a proposal dispatch
            await self._record_proposal_outcome(request, result)

            await self._session_manager.complete(
                session_id,
                cost_usd=output.cost_usd,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
            )

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
                await self._record_proposal_outcome(request, error_result)
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

    async def _record_proposal_outcome(
        self,
        request: DirectSessionRequest,
        result: DirectSessionResult,
    ) -> None:
        """Feed session outcome back to ego proposal for feedback loop."""
        if not request.caller_context or not request.caller_context.startswith("ego_proposal:"):
            return
        proposal_id = request.caller_context.split(":", 1)[1]
        try:
            from genesis.db.crud.ego import update_proposal_outcome

            db = getattr(self._rt, "_db", None)
            if db is None:
                return
            summary = (result.output_text or result.error or "")[:1000]
            await update_proposal_outcome(
                db, proposal_id, success=result.success, summary=summary,
            )
            # On failure: create observation so ego sees it next cycle
            if not result.success:
                try:
                    store = getattr(self._rt, "_memory_store", None)
                    if store is not None:
                        await store.store(
                            content=(
                                f"Ego dispatch FAILED for proposal {proposal_id}: {summary}"
                            ),
                            source="ego_dispatch_outcome",
                            tags=["ego", "dispatch_failure"],
                            memory_type="episodic",
                            wing="autonomy",
                            room="ego",
                        )
                except Exception:
                    logger.debug("Failed to store failure observation", exc_info=True)
        except Exception:
            logger.warning(
                "Failed to record proposal outcome for %s",
                proposal_id,
                exc_info=True,
            )

    def _build_invocation(self, request: DirectSessionRequest) -> CCInvocation:
        system_prompt = request.system_prompt
        if system_prompt is None:
            # Use the surplus config's system prompt (which loads SOUL.md)
            surplus_config = self._config_builder.build_surplus_config()
            system_prompt = surplus_config.get("system_prompt", "")

        # Inject profile addendum (tells session its constraints upfront)
        system_prompt += _build_profile_addendum(request.profile)

        # Inject skills (explicit from request, auto-detected from prompt keywords)
        skill_names = _resolve_skills(request)
        if skill_names:
            from genesis.learning.skills.wiring import load_skill

            for name in skill_names:
                content = load_skill(name)
                if content:
                    system_prompt += f"\n\n## Skill: {name}\n{content}"

        disallowed = PROFILES.get(request.profile, PROFILES["observe"])

        # Give background sessions access to Genesis MCP servers (health + memory).
        # Without this, the spawned CC process has no MCP tools (no browser, no
        # memory_store, no observation_write).
        mcp_config = self._config_builder.build_mcp_config(profile="reflection")

        # Prepend planning instruction if the caller opted in.
        prompt = request.prompt
        if request.planning_instruction:
            prompt = f"{request.planning_instruction}\n\n{prompt}"

        # Interact profile requires Opus — browser reasoning is complex and
        # ATS anti-bot detection demands higher capability.
        model = request.model
        if request.profile == "interact" and model != CCModel.OPUS:
            logger.info("interact profile: upgrading model %s → opus", model)
            model = CCModel.OPUS

        return CCInvocation(
            prompt=prompt,
            model=model,
            effort=request.effort,
            system_prompt=system_prompt,
            append_system_prompt=True,
            timeout_s=request.timeout_s,
            skip_permissions=True,
            disallowed_tools=disallowed,
            working_dir=background_session_dir(),
            mcp_config=mcp_config,
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

        # Derive transcript path from CC's project-key convention:
        # working_dir ~/.genesis/background-sessions → project key
        # -home-ubuntu--genesis-background-sessions → transcript .jsonl
        transcript_path = ""
        if result.cc_session_id:
            from pathlib import Path

            project_key = (
                background_session_dir()
                .replace("/", "-")
                .lstrip("-")
            )
            transcript_path = str(
                Path.home() / ".claude" / "projects"
                / f"-{project_key}" / f"{result.cc_session_id}.jsonl"
            )

        existing.update({
            "profile": request.profile,
            "caller_context": request.caller_context,
            "output_text": result.output_text[:20000],
            "tools_summary": tool_counts,
            "cc_session_id": result.cc_session_id,
            "transcript_path": transcript_path,
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
        """Send Telegram notification via outreach pipeline.

        Only failures are sent — successes are logged to the DB and visible
        via direct_session_list MCP tool and the dashboard.  Sending success
        notifications as ALERT was noise: "Direct Session Complete" is not
        an alert, and in DM-only installs it drowns real alerts.
        """
        if success:
            return

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

        body = (
            f"<b>Direct Session FAILED</b>\n\n"
            f"Profile: {request.profile} | "
            f"Model: {result.model_used or request.model} | "
            f"Duration: {result.duration_s:.0f}s\n\n"
            f"Error: {result.error or 'unknown'}\n\n"
            f"Tools before failure: {tools_str}"
        )

        try:
            from genesis.outreach.types import OutreachCategory, OutreachRequest

            await pipeline.submit(OutreachRequest(
                category=OutreachCategory.ALERT,
                topic="direct_session_fail",
                context=body,
                salience_score=0.9,
            ))
        except Exception:
            logger.error("Outreach submit failed", exc_info=True)
