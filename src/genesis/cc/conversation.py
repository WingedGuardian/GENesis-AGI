"""ConversationLoop — orchestrates user ↔ CC message flow."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from genesis.cc.context_injector import ContextInjector
from genesis.cc.exceptions import (
    CCError,
    CCMCPError,
    CCQuotaExhaustedError,
    CCRateLimitError,
    CCTimeoutError,
)
from genesis.cc.formatter import ResponseFormatter
from genesis.cc.intent import IntentParser
from genesis.cc.session_manager import SessionManager
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.cc.types import CCInvocation, CCModel, ChannelType, EffortLevel, StreamEvent
from genesis.db.crud import cc_sessions
from genesis.observability.call_site_recorder import record_last_run

if TYPE_CHECKING:
    from genesis.cc.contingency import CCContingencyDispatcher
    from genesis.cc.protocol import AgentProvider

logger = logging.getLogger(__name__)


class ConversationLoop:
    """Channel-agnostic conversation orchestrator.

    Handles: intent parsing, session management, CC invocation,
    response formatting. Used by terminal (GL-2) and Telegram (GL-3).
    """

    def __init__(
        self,
        *,
        db,
        invoker: AgentProvider,
        assembler: SystemPromptAssembler,
        day_boundary_hour: int = 0,
        triage_pipeline: Callable[..., Coroutine[Any, Any, None]] | None = None,
        context_injector: ContextInjector | None = None,
        session_manager: SessionManager | None = None,
        contingency: CCContingencyDispatcher | None = None,
        failure_detector: object | None = None,
        default_model: CCModel = CCModel.SONNET,
        default_effort: EffortLevel = EffortLevel.MEDIUM,
    ):
        self._db = db
        self._invoker = invoker
        self._assembler = assembler
        self._session_mgr = session_manager or SessionManager(
            db=db, day_boundary_hour=day_boundary_hour,
        )
        self._intent_parser = IntentParser()
        self._formatter = ResponseFormatter()
        self._day_boundary_hour = day_boundary_hour
        self._triage_pipeline = triage_pipeline
        self._context_injector = context_injector
        self._contingency = contingency
        self._failure_detector = failure_detector
        self._default_model = default_model
        self._default_effort = default_effort
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def interrupt(self) -> None:
        """Send interrupt (SIGINT) to the active CC subprocess, if any."""
        await self._invoker.interrupt()

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        """Return (or create) the per-session serialization lock.

        No eviction — Lock objects are tiny (~200 bytes). Evicting unlocked
        entries races with coroutines that hold a reference but haven't
        entered ``async with`` yet, causing two coroutines to hold different
        locks for the same session.  Explicit cleanup happens in
        ``_should_reset`` and ``_recover_stale_resume`` via dict pop.
        """
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    async def handle_message(
        self,
        text: str,
        *,
        user_id: str,
        channel: ChannelType,
        thread_id: str | None = None,
    ) -> str:
        """Process a user message and return the response text."""
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            if rt.idle_detector:
                rt.idle_detector.mark_active()
        except Exception:
            pass  # Don't let idle tracking break conversation

        # Inline failure detection: scan user input for correction patterns
        self._fire_user_correction_scan(text)

        intent = self._intent_parser.parse(text)
        prompt_text = intent.cleaned_text or intent.raw_text

        if intent.task_requested:
            try:
                import uuid as _uuid
                from datetime import UTC, datetime

                from genesis.db.crud import observations
                await observations.create(
                    self._db,
                    id=str(_uuid.uuid4()),
                    source="conversation_intent",
                    type="task_detected",
                    content=prompt_text,
                    priority="medium",
                    created_at=datetime.now(UTC).isoformat(),
                    skip_if_duplicate=True,
                )
            except Exception:
                logger.error("Could not emit task_detected observation", exc_info=True)

        # Check for morning reset — complete stale sessions from previous day
        session = await cc_sessions.get_active_foreground(
            self._db, user_id=user_id, channel=str(channel),
            thread_id=thread_id,
        )
        if session and self._should_reset(session):
            self._session_locks.pop(session["id"], None)
            await self._session_mgr.complete(session["id"])
            session = None

        # Resolve model/effort: explicit override > session stored > config default
        model = intent.model_override or (
            CCModel(session["model"]) if session and session.get("model") else self._default_model
        )
        effort = intent.effort_override or (
            EffortLevel(session["effort"]) if session and session.get("effort") else self._default_effort
        )

        # Get or create session, persist any model/effort changes
        session = await self._session_mgr.get_or_create_foreground(
            user_id=user_id, channel=channel, model=model, effort=effort,
            thread_id=thread_id,
        )

        async with self._get_lock(session["id"]):
            await self._persist_overrides(session, model, effort)

            # First message: full system prompt, no resume
            # Subsequent: resume with cc_session_id, no system prompt
            cc_sid = session.get("cc_session_id")
            if cc_sid:
                system_prompt = None
                resume_id = cc_sid
            else:
                system_prompt = await self._assembler.assemble(
                    db=self._db, model=str(model), effort=str(effort),
                    session_id=session["id"],
                )
                system_prompt = await self._enrich_with_context(
                    system_prompt, prompt_text,
                )
                resume_id = None

            invocation = CCInvocation(
                prompt=prompt_text,
                model=model,
                effort=effort,
                system_prompt=system_prompt,
                resume_session_id=resume_id,
                skip_permissions=True,
                append_system_prompt=True,
            )

            try:
                output, session = await self._try_invoke(
                    invocation, session=session, was_resume=bool(cc_sid),
                    prompt_text=prompt_text, model=model, effort=effort,
                    user_id=user_id, channel=channel, thread_id=thread_id,
                )
            except CCTimeoutError:
                self._fire_failure_detection("timeout")
                try:
                    await self._session_mgr.fail(
                        session["id"], reason="cc_timeout",
                    )
                except Exception:
                    logger.error(
                        "Failed to mark session %s as failed after timeout",
                        session["id"][:8], exc_info=True,
                    )
                return "[Genesis timed out — try a simpler request]"
            except (CCQuotaExhaustedError, CCRateLimitError) as e:
                self._fire_failure_detection("rate_limited")
                # Record rate limit event
                try:
                    from datetime import UTC, datetime
                    await cc_sessions.update_rate_limit(
                        self._db, session["id"],
                        rate_limited_at=datetime.now(UTC).isoformat(),
                    )
                except Exception:
                    logger.error("Failed to record rate limit", exc_info=True)
                fallback = await self._try_contingency(
                    prompt_text, system_prompt, channel,
                    session_id=session["id"],
                )
                if fallback is not None:
                    return fallback
                logger.error(
                    "Contingency fallback failed after rate limit: %s", e,
                    exc_info=True,
                )
                return (
                    "[Rate limit reached — Genesis is temporarily running in reduced mode. "
                    "Background tasks are queued and will resume automatically.]"
                )
            except CCMCPError as e:
                self._fire_failure_detection("mcp_error")
                server = f" ({e.server_name})" if e.server_name else ""
                return f"[MCP error{server} — try again]"
            except CCError as e:
                self._fire_failure_detection("generic_error")
                return f"[Genesis error: {e}]"

            # Store cc_session_id from first response
            if not session.get("cc_session_id") and output.session_id:
                await cc_sessions.update_cc_session_id(
                    self._db, session["id"], cc_session_id=output.session_id,
                )

            await self._session_mgr.update_activity(session["id"])

            # Record cost incrementally (session stays active)
            if output.cost_usd or output.input_tokens or output.output_tokens:
                try:
                    await cc_sessions.increment_cost(
                        self._db, session["id"],
                        cost_usd=output.cost_usd or 0.0,
                        input_tokens=output.input_tokens or 0,
                        output_tokens=output.output_tokens or 0,
                    )
                except Exception:
                    logger.debug("Failed to record foreground cost", exc_info=True)

            # Record last run for neural monitor
            await record_last_run(
                self._db, "cc_foreground",
                provider="cc", model_id=output.model_used or str(model),
                response_text=output.text,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
            )

            parts = self._formatter.format(output.text, channel=channel)

            if self._triage_pipeline is not None:
                from genesis.observability.types import Subsystem
                from genesis.util.tasks import tracked_task

                tracked_task(
                    self._fire_triage(output, text, str(channel)),
                    name="triage-pipeline",
                    subsystem=Subsystem.LEARNING,
                )

            return "\n".join(parts)

    async def handle_message_streaming(
        self,
        text: str,
        *,
        user_id: str,
        channel: ChannelType,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Like handle_message but uses streaming for live progress."""
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            if rt.idle_detector:
                rt.idle_detector.mark_active()
        except Exception:
            pass  # Don't let idle tracking break conversation

        # Inline failure detection: scan user input for correction patterns
        self._fire_user_correction_scan(text)

        intent = self._intent_parser.parse(text)
        prompt_text = intent.cleaned_text or intent.raw_text

        if intent.task_requested:
            try:
                import uuid as _uuid
                from datetime import UTC, datetime

                from genesis.db.crud import observations
                await observations.create(
                    self._db,
                    id=str(_uuid.uuid4()),
                    source="conversation_intent",
                    type="task_detected",
                    content=prompt_text,
                    priority="medium",
                    created_at=datetime.now(UTC).isoformat(),
                    skip_if_duplicate=True,
                )
            except Exception:
                logger.error("Could not emit task_detected observation", exc_info=True)

        session = await cc_sessions.get_active_foreground(
            self._db, user_id=user_id, channel=str(channel),
            thread_id=thread_id,
        )
        session_was_reset = False
        if session and self._should_reset(session):
            self._session_locks.pop(session["id"], None)
            await self._session_mgr.complete(session["id"])
            session_was_reset = True
            session = None

        model = intent.model_override or (
            CCModel(session["model"]) if session and session.get("model") else self._default_model
        )
        effort = intent.effort_override or (
            EffortLevel(session["effort"]) if session and session.get("effort") else self._default_effort
        )

        session = await self._session_mgr.get_or_create_foreground(
            user_id=user_id, channel=channel, model=model, effort=effort,
            thread_id=thread_id,
        )

        async with self._get_lock(session["id"]):
            # Capture old values before persisting overrides (for change feedback)
            old_model = session.get("model")
            old_effort = session.get("effort")
            await self._persist_overrides(session, model, effort)

            # Emit immediate feedback on model/effort changes
            if on_event:
                if str(model) != old_model:
                    await on_event(StreamEvent(
                        event_type="system_notice",
                        text=f"Switching to {model.value.title()}...",
                    ))
                if str(effort) != old_effort:
                    await on_event(StreamEvent(
                        event_type="system_notice",
                        text=f"Thinking effort: {effort.value}",
                    ))

            # Layer A: intent-only messages (e.g. "switch to sonnet" with
            # no remaining text) — persist overrides and return confirmation
            # without invoking CC subprocess.
            if intent.intent_only:
                parts = []
                if str(model) != old_model:
                    parts.append(f"Model: {model.value.title()}")
                if str(effort) != old_effort:
                    parts.append(f"Effort: {effort.value}")
                return " | ".join(parts) if parts else "Settings unchanged."

            # Session recovery detection: if session was reset or this is
            # a fresh session (no cc_session_id) with recent message history,
            # notify the user and inject conversation context.
            cc_sid = session.get("cc_session_id")
            recovery_context = ""
            if not cc_sid and (session_was_reset or not session.get("message_count")):
                recovery_context = await self._build_recovery_context(
                    user_id, channel, thread_id,
                )
                if recovery_context and on_event:
                    await on_event(StreamEvent(
                        event_type="system_notice",
                        text="Session restarted — injecting recent context.",
                    ))

            if cc_sid:
                system_prompt = None
                resume_id = cc_sid
            else:
                system_prompt = await self._assembler.assemble(
                    db=self._db, model=str(model), effort=str(effort),
                    session_id=session["id"],
                )
                system_prompt = await self._enrich_with_context(
                    system_prompt, prompt_text,
                )
                if recovery_context:
                    system_prompt += (
                        "\n\n## Recent conversation (session recovered)\n"
                        + recovery_context
                    )

                # Topic-aware context: inject proposal board when in ego_proposals
                if thread_id:
                    topic_ctx = await self._build_topic_context(thread_id)
                    if topic_ctx:
                        system_prompt += topic_ctx

                resume_id = None

            invocation = CCInvocation(
                prompt=prompt_text,
                model=model,
                effort=effort,
                system_prompt=system_prompt,
                resume_session_id=resume_id,
                skip_permissions=True,
                append_system_prompt=True,
            )

            try:
                output, session = await self._try_invoke_streaming(
                    invocation, session=session, was_resume=bool(cc_sid),
                    prompt_text=prompt_text, model=model, effort=effort,
                    user_id=user_id, channel=channel, thread_id=thread_id,
                    on_event=on_event,
                )
            except CCTimeoutError:
                self._fire_failure_detection("timeout")
                try:
                    await self._session_mgr.fail(
                        session["id"], reason="cc_timeout",
                    )
                except Exception:
                    logger.error(
                        "Failed to mark session %s as failed after timeout",
                        session["id"][:8], exc_info=True,
                    )
                return "[Genesis timed out — try a simpler request]"
            except (CCQuotaExhaustedError, CCRateLimitError) as e:
                self._fire_failure_detection("rate_limited")
                # Record rate limit event
                try:
                    from datetime import UTC, datetime
                    await cc_sessions.update_rate_limit(
                        self._db, session["id"],
                        rate_limited_at=datetime.now(UTC).isoformat(),
                    )
                except Exception:
                    logger.error("Failed to record rate limit", exc_info=True)
                fallback = await self._try_contingency(
                    prompt_text, system_prompt, channel,
                    session_id=session["id"],
                )
                if fallback is not None:
                    return fallback
                logger.error(
                    "Contingency fallback failed after rate limit: %s", e,
                    exc_info=True,
                )
                return (
                    "[Rate limit reached — Genesis is temporarily running in reduced mode. "
                    "Background tasks are queued and will resume automatically.]"
                )
            except CCMCPError as e:
                self._fire_failure_detection("mcp_error")
                server = f" ({e.server_name})" if e.server_name else ""
                return f"[MCP error{server} — try again]"
            except CCError as e:
                self._fire_failure_detection("generic_error")
                return f"[Genesis error: {e}]"

            if not session.get("cc_session_id") and output.session_id:
                await cc_sessions.update_cc_session_id(
                    self._db, session["id"], cc_session_id=output.session_id,
                )

            await self._session_mgr.update_activity(session["id"])

            # Record cost incrementally (session stays active)
            if output.cost_usd or output.input_tokens or output.output_tokens:
                try:
                    await cc_sessions.increment_cost(
                        self._db, session["id"],
                        cost_usd=output.cost_usd or 0.0,
                        input_tokens=output.input_tokens or 0,
                        output_tokens=output.output_tokens or 0,
                    )
                except Exception:
                    logger.debug("Failed to record foreground cost", exc_info=True)

            # Record last run for neural monitor
            await record_last_run(
                self._db, "cc_foreground",
                provider="cc", model_id=output.model_used or str(model),
                response_text=output.text,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
            )

            parts = self._formatter.format(output.text, channel=channel)

            if self._triage_pipeline is not None:
                from genesis.observability.types import Subsystem
                from genesis.util.tasks import tracked_task

                tracked_task(
                    self._fire_triage(output, text, str(channel)),
                    name="triage-pipeline",
                    subsystem=Subsystem.LEARNING,
                )

            return "\n".join(parts)

    async def _try_invoke(
        self,
        invocation: CCInvocation,
        *,
        session: dict,
        was_resume: bool,
        prompt_text: str,
        model: CCModel,
        effort: EffortLevel,
        user_id: str,
        channel: ChannelType,
        thread_id: str | None,
    ) -> tuple[Any, dict]:
        """Invoke CC with resume-failure recovery.

        If the invocation was a resume and it raises a CCError, clears the
        stale cc_session_id, fails the old session, creates a fresh one,
        and retries once without resume.

        Returns (output, session) — session may be a new one after recovery.
        Raises CCError subclasses if the (retry) invocation fails.
        """
        try:
            output = await self._invoker.run(invocation)
            return output, session
        except (CCRateLimitError, CCQuotaExhaustedError):
            # Rate limits are account-wide — retrying fresh won't help.
            # Let the caller's contingency handler deal with it.
            raise
        except CCError:
            if not was_resume:
                raise
            # Resume failed — recover and retry fresh
            session = await self._recover_stale_resume(
                session, user_id=user_id, channel=channel,
                thread_id=thread_id, model=model, effort=effort,
            )
            fresh_inv = await self._build_fresh_invocation(
                prompt_text, model=model, effort=effort,
                session_id=session["id"],
            )
            # Retry — if this also fails, the exception propagates to caller
            output = await self._invoker.run(fresh_inv)
            return output, session

    async def _try_invoke_streaming(
        self,
        invocation: CCInvocation,
        *,
        session: dict,
        was_resume: bool,
        prompt_text: str,
        model: CCModel,
        effort: EffortLevel,
        user_id: str,
        channel: ChannelType,
        thread_id: str | None,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None,
    ) -> tuple[Any, dict]:
        """Streaming variant of _try_invoke with resume-failure recovery."""
        try:
            output = await self._invoker.run_streaming(invocation, on_event=on_event)
            return output, session
        except (CCRateLimitError, CCQuotaExhaustedError):
            raise  # Account-wide — retrying fresh won't help
        except CCError:
            if not was_resume:
                raise
            session = await self._recover_stale_resume(
                session, user_id=user_id, channel=channel,
                thread_id=thread_id, model=model, effort=effort,
            )
            fresh_inv = await self._build_fresh_invocation(
                prompt_text, model=model, effort=effort,
                session_id=session["id"],
            )
            output = await self._invoker.run_streaming(fresh_inv, on_event=on_event)
            return output, session

    async def _build_fresh_invocation(
        self,
        prompt_text: str,
        *,
        model: CCModel,
        effort: EffortLevel,
        session_id: str | None = None,
    ) -> CCInvocation:
        """Build a fresh invocation (with system prompt, no resume)."""
        system_prompt = await self._assembler.assemble(
            db=self._db, model=str(model), effort=str(effort),
            session_id=session_id,
        )
        system_prompt = await self._enrich_with_context(system_prompt, prompt_text)
        return CCInvocation(
            prompt=prompt_text,
            model=model,
            effort=effort,
            system_prompt=system_prompt,
            resume_session_id=None,
            skip_permissions=True,
            append_system_prompt=True,
        )

    async def _recover_stale_resume(
        self,
        old_session: dict,
        *,
        user_id: str,
        channel: ChannelType,
        thread_id: str | None,
        model: CCModel,
        effort: EffortLevel,
    ) -> dict:
        """Clear stale cc_session_id, fail old session, create fresh one."""
        old_id = old_session["id"]
        self._session_locks.pop(old_id, None)
        old_cc_sid = old_session.get("cc_session_id", "?")
        logger.warning(
            "CC resume failed for session %s (cc_session_id=%s), retrying fresh",
            old_id[:8], old_cc_sid,
        )
        await cc_sessions.clear_cc_session_id(self._db, old_id)
        await self._session_mgr.fail(old_id, reason="stale resume")
        new_session = await self._session_mgr.get_or_create_foreground(
            user_id=user_id, channel=channel, model=model, effort=effort,
            thread_id=thread_id,
        )
        return new_session

    async def _persist_overrides(
        self, session: dict, model: CCModel, effort: EffortLevel,
    ) -> None:
        """If model or effort changed from what the session stores, update DB."""
        new_model = str(model) if str(model) != session.get("model") else None
        new_effort = str(effort) if str(effort) != session.get("effort") else None
        if new_model or new_effort:
            await cc_sessions.update_model_effort(
                self._db, session["id"], model=new_model, effort=new_effort,
            )
            logger.info(
                "Session %s updated: model=%s effort=%s",
                session["id"][:8], model, effort,
            )

    async def _build_topic_context(self, thread_id: str) -> str | None:
        """Build topic-specific context for the conversation system prompt.

        When the user is messaging in the ego_proposals topic, inject the
        pending proposal board so the CC session can discuss and resolve them.
        """
        if self._db is None:
            return None
        try:
            # Look up which topic this thread_id belongs to
            async with self._db.execute(
                "SELECT category FROM telegram_topics WHERE thread_id = ?",
                (int(thread_id),),
            ) as cur:
                row = await cur.fetchone()
            if not row or row[0] != "ego_proposals":
                return None

            # Fetch pending proposals
            from genesis.db.crud import ego as ego_crud

            pending = await ego_crud.list_proposals(
                self._db, status="pending", limit=10,
            )
            if not pending:
                return "\n\n## Ego Proposals Topic\n\nNo pending proposals.\n"

            lines = ["\n\n## You Are in the Ego Proposals Topic\n"]
            lines.append(
                "The user communicates with you here to review, approve, reject, "
                "or discuss ego proposals. When the user indicates approval "
                "(e.g., 'do it', 'yes', 'go ahead', 'approve 1'), resolve the "
                "proposal. When they reject, mark it rejected with their reason. "
                "When unclear, ask for clarification.\n"
            )
            lines.append("### Pending Proposals:\n")
            for i, p in enumerate(pending, 1):
                cat = p.get("action_category", "unknown")
                content = (p.get("content") or "")[:120]
                pid = p["id"]
                lines.append(f"{i}. **[{cat}]** {content}")
                lines.append(f"   ID: `{pid}`\n")

            lines.append(
                "\n### To resolve a proposal:\n"
                "Use Bash to run the genesis CLI:\n"
                "```bash\n"
                "source ~/genesis/.venv/bin/activate && python -c \"\n"
                "import asyncio, aiosqlite\n"
                "async def go():\n"
                "    async with aiosqlite.connect("
                "'$HOME/genesis/data/genesis.db') as db:\n"
                "        await db.execute(\n"
                "            'UPDATE ego_proposals SET status=?, "
                "resolved_at=datetime(\"now\") WHERE id=? AND status=\"pending\"',\n"
                "            ('approved', '<PROPOSAL_ID>'),\n"
                "        )\n"
                "        await db.commit()\n"
                "asyncio.run(go())\n"
                "\"\n"
                "```\n"
                "For rejection: use 'rejected' instead of 'approved'.\n"
                "\n### Important:\n"
                "- If the user gives guidance or corrections (not just approve/reject),\n"
                "  store it via `memory_store` MCP so the ego sees it in future cycles.\n"
                "- Always confirm what you did: 'Approved proposal 1: [content]'\n"
            )
            return "\n".join(lines)
        except Exception:
            logger.debug("Failed to build topic context", exc_info=True)
            return None

    async def _enrich_with_context(
        self, system_prompt: str | None, query: str,
    ) -> str | None:
        """Append relevant prior experience to system prompt."""
        if not system_prompt or not self._context_injector:
            return system_prompt
        try:
            ctx = await asyncio.wait_for(
                self._context_injector.inject(query, limit=5),
                timeout=3.0,
            )
            if ctx:
                return system_prompt + "\n\n" + ctx
        except Exception:
            logger.warning("Context injection skipped", exc_info=True)
        return system_prompt

    async def _build_recovery_context(
        self,
        user_id: str,
        channel: ChannelType,
        thread_id: str | None,
    ) -> str:
        """Load recent messages for session recovery context injection.

        Returns a formatted string of recent conversation, or "" if none.
        """
        if str(channel) != "telegram":
            return ""
        try:
            from genesis.db.crud.telegram_messages import query_recent

            # Extract numeric chat_id from user_id (tg-<id>)
            chat_id_str = user_id.replace("tg-", "")
            if not chat_id_str.isdigit():
                return ""
            chat_id = int(chat_id_str)

            messages = await query_recent(
                self._db,
                chat_id,
                thread_id=int(thread_id) if thread_id else None,
                limit=10,
            )
            if not messages:
                return ""

            lines = []
            for m in messages:
                sender = m.get("sender", "?")
                content = m.get("content", "")
                if content:
                    prefix = "User" if sender == "user" else "Genesis"
                    # Truncate long messages
                    if len(content) > 300:
                        content = content[:300] + "..."
                    lines.append(f"{prefix}: {content}")

            if not lines:
                return ""
            return "\n".join(lines)
        except Exception:
            logger.warning("Failed to load recovery context", exc_info=True)
            return ""

    async def _try_contingency(
        self,
        prompt_text: str,
        system_prompt: str | None,
        channel: ChannelType,
        session_id: str | None = None,
    ) -> str | None:
        """Attempt to route through API contingency dispatcher.

        Returns formatted response string on success, None on failure.
        """
        if self._contingency is None:
            return None

        # Rebuild system prompt if it was None (resume case)
        if system_prompt is None:
            try:
                system_prompt = await self._assembler.assemble(
                    db=self._db, model="sonnet", effort="medium",
                    session_id=session_id,
                )
            except Exception:
                logger.error("Failed to assemble system prompt for contingency", exc_info=True)
                return None

        messages = [{"role": "user", "content": prompt_text}]

        try:
            result = await self._contingency.dispatch_conversation(
                messages, system_prompt,
            )
        except Exception:
            logger.error("Contingency dispatch failed", exc_info=True)
            return None

        if not result.success:
            logger.warning("Contingency dispatch unsuccessful: %s", result.reason)
            return None

        model_note = f" via {result.model}" if result.model else ""
        parts = self._formatter.format(result.content, channel=channel)
        response = "\n".join(parts)
        logger.info("Contingency response%s (%d chars)", model_note, len(response))
        return f"[Contingency mode{model_note} — CC limits reached]\n\n{response}"

    async def _fire_triage(self, output: Any, user_text: str, channel: str) -> None:
        """Fire-and-forget triage pipeline. Never crashes the main flow."""
        try:
            await self._triage_pipeline(output, user_text, channel)
        except Exception:
            logger.exception("triage pipeline failed (background learning)")

    def _fire_failure_detection(self, error_type: str) -> None:
        """Fire-and-forget failure detection from CC error handlers."""
        if getattr(self, "_failure_detector", None) is None:
            return
        try:
            from genesis.observability.types import Subsystem
            from genesis.util.tasks import tracked_task

            tracked_task(
                self._failure_detector.record_cc_error(self._db, error_type),
                name="failure-detector",
                subsystem=Subsystem.LEARNING,
            )
        except Exception:
            logger.debug("Failure detection dispatch failed", exc_info=True)

    def _fire_user_correction_scan(self, user_text: str) -> None:
        """Scan user input for correction patterns, fire-and-forget."""
        if getattr(self, "_failure_detector", None) is None:
            return
        try:
            failure_type = self._failure_detector.scan_user_input(user_text)
            if failure_type is None:
                return
            from genesis.observability.types import Subsystem
            from genesis.util.tasks import tracked_task

            tracked_task(
                self._failure_detector.record_failure(self._db, failure_type),
                name="failure-detector-user",
                subsystem=Subsystem.LEARNING,
            )
        except Exception:
            logger.debug("User correction scan failed", exc_info=True)

    def _should_reset(self, session: dict) -> bool:
        """Check if session is from a previous day boundary.

        Supergroup topic sessions (thread_id set) are persistent — they
        only compact when CC context limits are hit, never by day boundary.
        """
        # Supergroup topic sessions are persistent — no daily reset
        if session.get("thread_id"):
            return False
        started = session.get("started_at")
        if not started:
            return False
        started_dt = datetime.fromisoformat(started)
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        boundary = now.replace(
            hour=self._day_boundary_hour, minute=0, second=0, microsecond=0,
        )
        if now < boundary:
            boundary -= timedelta(days=1)
        return started_dt < boundary
