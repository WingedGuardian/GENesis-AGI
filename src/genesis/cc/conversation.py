"""ConversationLoop — orchestrates user ↔ CC message flow."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from genesis.cc import peer_availability, rate_limit_park, roster
from genesis.cc.context_injector import ContextInjector
from genesis.cc.exceptions import (
    CCError,
    CCMCPError,
    CCNetworkOfflineError,
    CCQuotaExhaustedError,
    CCRateLimitError,
    CCTimeoutError,
)
from genesis.cc.formatter import ResponseFormatter
from genesis.cc.intent import IntentParser
from genesis.cc.session_manager import SessionManager
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.cc.types import (
    CCInvocation,
    CCModel,
    ChannelType,
    EffortLevel,
    StreamEvent,
    is_owner_attended_channel,
    origin_delivery_supported,
    session_origin_for_channel,
    task_detected_origin,
)
from genesis.db.crud import cc_sessions
from genesis.observability.call_site_recorder import record_last_run
from genesis.util import tz

if TYPE_CHECKING:
    from genesis.cc.contingency import CCContingencyDispatcher
    from genesis.cc.protocol import AgentProvider

logger = logging.getLogger(__name__)

# Appended to a delivered reply when the CLI truncated dispatched background work
# at its wait ceiling (CCOutput.bg_truncated). Surfaces the 2026-07-20 silent-death
# class to the user instead of shipping a partial answer as if it were complete.
_BG_TRUNCATION_NOTICE = (
    "\n\n⚠️ Heads up: some background work hit a time limit and was cut off "
    "before finishing, so this reply may be incomplete. For long research, ask me "
    "to run it as a background task so it can finish and report back."
)


def _bg_notice(output) -> str:
    """The truncation notice when a reply's background work was cut off, else ''."""
    return _BG_TRUNCATION_NOTICE if getattr(output, "bg_truncated", False) else ""


# Nudge for dispatched, delivery-addressable (Telegram) channels: route long research/bg work
# durable direct_session lane instead of an inline Workflow, which the CC bg-wait ceiling
# kills after ~10min with nothing left to report back (the 2026-07-20 silent-death class).
# Pairs with the merged delivery model (PR #1192): deliver_to_origin=true sends the
# finished outcome back to THIS conversation, so the "I'll report back" promise is kept
# instead of a successful background run going silent (the deferral condition in d7aedfdf).
_BG_RESEARCH_ROUTING = (
    "\n\n## Dispatching long-running work from this channel\n"
    "Your turn here ends after you reply, and any deep-research or Workflow you run "
    "inline is force-killed after about 10 minutes with only a partial result, with no "
    "live session left to report back. So when a request needs deep or multi-source "
    "research, or background work likely to run more than a few minutes, do NOT run it "
    "inline. Call the `mcp__genesis-health__direct_session_run` tool "
    '(profile="research", deliver_to_origin=true) with a clear task prompt, then reply '
    "that it is running in the background and will report back with results when done. "
    "That background session runs to completion and delivers the finished outcome — "
    "success or failure — back to this exact conversation. Keep quick answers and short "
    "tool use inline as usual."
)


def _apply_research_routing(system_prompt: str | None, channel) -> str | None:
    """Append the long-research routing nudge for channels the delivery model can
    actually report back to.

    The nudge tells the model to hand long research to the background lane with
    ``deliver_to_origin=true`` and promise "I'll report back to this conversation."
    That promise is only keepable where ``direct_session`` can resolve an origin
    target — i.e. Telegram (see ``origin_delivery_supported``, the single source of
    truth shared with ``DirectSessionRunner._resolve_origin_target``). On any other
    channel (WEB/OpenClaw, WhatsApp, VOICE) the result would silently fall back to the
    owner surface, so the nudge is withheld rather than promise a report-back the
    delivery model cannot keep. Terminal is interactive anyway (user present), so
    inline work is fine there.
    """
    if not origin_delivery_supported(channel):
        return system_prompt
    return (system_prompt + _BG_RESEARCH_ROUTING) if system_prompt else _BG_RESEARCH_ROUTING


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

    async def interrupt(self, key: str | None = None) -> None:
        """Send interrupt (SIGINT) to a session's CC subprocess, if any.

        With ``key``, targets that session's proc (so `/stop` hits the user's
        session, not a concurrent background one); without it, the invoker
        targets the most-recently-spawned live proc (back-compat).
        """
        await self._invoker.interrupt(key)

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
        chat_id: str | None = None,
        intent_text: str | None = None,
    ) -> str:
        """Process a user message and return the response text.

        ``intent_text`` (WS-3): the OWNER-authored text to scan for slash intents
        (/task, /model, /effort, /resume) when *text* is a composite the caller
        built (e.g. a Telegram quote-reply = quoted bot message + owner reply).
        Quoted bot text can relay external content (inbox digests, recon
        findings), so scanning it would let that content forge an owner-authorized
        /task or flip the model. When set, control tokens + task-intent content
        come ONLY from ``intent_text`` while *text* stays the LLM prompt (so the
        quoted context is preserved). ``None`` → scan *text* itself (unchanged).
        """
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            if rt.idle_detector:
                rt.idle_detector.mark_active()
        except Exception:
            pass  # Don't let idle tracking break conversation

        scan_text = intent_text if intent_text is not None else text
        # Inline failure detection: scan owner-authored input for correction patterns
        self._fire_user_correction_scan(scan_text)

        intent = self._intent_parser.parse(scan_text)
        if intent_text is not None:
            # Composite: keep full context for the LLM; task content is owner-only.
            prompt_text = text
            task_content = intent.cleaned_text or intent_text
        else:
            prompt_text = intent.cleaned_text or intent.raw_text
            task_content = prompt_text

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
                    # WS-3: owner-authored content only (never the quoted composite)
                    content=task_content,
                    priority="medium",
                    created_at=datetime.now(UTC).isoformat(),
                    # WS-3: source is channel-agnostic (conversation_intent), so
                    # stamp origin by channel — owner-attended (terminal/Telegram)
                    # carries dispatch authority; gateway channels → external.
                    origin_class=task_detected_origin(channel),
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
            thread_id=thread_id, chat_id=chat_id,
        )

        # Set session context so downstream code (CCInvoker, eval hooks)
        # can attribute work to this session without explicit threading.
        # Uses set/clear rather than session_scope() to avoid re-indenting
        # the entire lock block; follows the pattern in direct_session.py.
        from genesis.observability.session_context import set_session_id
        set_session_id(session["id"])

        async with self._get_lock(session["id"]):
            await self._persist_overrides(session, model, effort)

            # First message: full system prompt, no resume
            # Subsequent: resume with cc_session_id, no system prompt
            cc_sid = session.get("cc_session_id")
            # Roster resume continuity: if this session was created on a routed
            # (non-Anthropic) endpoint, reconstruct those overrides so it resumes
            # on the SAME endpoint. If reconstruction fails (token gone), degrade
            # to a fresh session — never resume a routed session on native Claude.
            resume_overrides: dict = {}
            if cc_sid:
                resume_overrides, cc_sid = self._reconstruct_resume(session, cc_sid)
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

            # Non-terminal (dispatched) channels end the turn after replying, so long
            # inline work is killed at the CC bg-wait ceiling with nothing left to report
            # back — nudge routing to the durable background lane (delivers back via
            # deliver_to_origin). Applied on resume too (append_system_prompt=True carries
            # it into the resumed session). See _apply_research_routing.
            system_prompt = _apply_research_routing(system_prompt, channel)

            invocation = CCInvocation(
                prompt=prompt_text,
                model=model,
                effort=effort,
                system_prompt=system_prompt,
                resume_session_id=resume_id,
                skip_permissions=True,
                append_system_prompt=True,
                roster_eligible=True,
                # WS-3 B4/gate-4: spare the pushed-surfaces enforce drop ONLY for
                # owner-attended channels (terminal/Telegram). A gateway
                # conversation (web/OpenClaw, WhatsApp, voice) is NOT owner-
                # authenticated, so it stays unsupervised — fail-closed toward
                # dropping wrapped-external pushed content, never injecting it.
                supervised=is_owner_attended_channel(channel),
                # WS-3 gate-4 producer half: gateway → external_untrusted so the
                # session's own memory/observation_write calls are stamped
                # untrusted (owner-attended → None → first_party coalesce).
                origin=session_origin_for_channel(channel),
                # Owner-attended interactive session: keep the full user-scoped
                # MCP toolset. Opt OUT of secure-by-default strict scoping
                # (see CCInvocation.strict_mcp_config).
                strict_mcp_config=False,
                **resume_overrides,
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
                # Phase 3: real CC failover to a roster peer (full tools) BEFORE
                # the degraded contingency path. None → fall through to contingency.
                roster_reply = await self._try_roster_failover(
                    invocation, session=session, channel=channel,
                    model=model, effort=effort, prompt_text=prompt_text,
                )
                if roster_reply is not None:
                    return roster_reply
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
                # Both fallbacks failed → the user got no answer. Park the turn
                # durably so it auto-resumes when capacity returns (rate_limit_park
                # owns the reset parse, the cc_sessions resume-time write, and the
                # mode-aware copy — replacing the old sentence nothing backed).
                outcome = await rate_limit_park.park_conversation(
                    self._db,
                    prompt=prompt_text,
                    origin_session_id=session["id"],
                    exc=e,
                    model=model,
                    effort=effort,
                )
                return outcome.copy
            except CCMCPError as e:
                self._fire_failure_detection("mcp_error")
                server = f" ({e.server_name})" if e.server_name else ""
                return f"[MCP error{server} — try again]"
            except CCError as e:
                self._fire_failure_detection("generic_error")
                return f"[Genesis error: {e}]"

            # Store cc_session_id from first response (non-critical — next
            # turn re-checks the guard so a transient DB lock just delays
            # session resume by one turn).
            if not session.get("cc_session_id") and output.session_id:
                try:
                    await cc_sessions.update_cc_session_id(
                        self._db, session["id"], cc_session_id=output.session_id,
                    )
                    await self._persist_roster_endpoint(session["id"], output)
                except Exception:
                    logger.warning("Failed to store cc_session_id", exc_info=True)

            # Phase 3: reaching here means the HOME model succeeded — if we were in
            # a fallback, that's recovery. Clear the account-wide flag + this
            # session's sticky peer session (failover returns early, never here).
            await self._maybe_clear_fallback(session)

            # Activity timestamp — non-critical, but the stale-session reaper
            # (SessionManager.cleanup_stale) keys on it so persistent
            # failures deserve monitoring (WARNING, not debug).
            try:
                await self._session_mgr.update_activity(session["id"])
            except Exception:
                logger.warning("Failed to update session activity", exc_info=True)

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

            parts = self._formatter.format(output.text + _bg_notice(output), channel=channel)

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
        session_key: str | None = None,
        chat_id: str | None = None,
        intent_text: str | None = None,
    ) -> str:
        """Like handle_message but uses streaming for live progress.

        ``session_key`` (opaque) is stamped on the CC invocation so a caller's
        interrupt (Telegram /stop) targets this session's subprocess (cc-loop-01).
        ``intent_text`` (WS-3): owner-authored text to scan for slash intents when
        *text* is a composite (quote-reply) — see :meth:`handle_message`.
        """
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            if rt.idle_detector:
                rt.idle_detector.mark_active()
        except Exception:
            pass  # Don't let idle tracking break conversation

        scan_text = intent_text if intent_text is not None else text
        # Inline failure detection: scan owner-authored input for correction patterns
        self._fire_user_correction_scan(scan_text)

        intent = self._intent_parser.parse(scan_text)
        if intent_text is not None:
            # Composite: keep full context for the LLM; task content is owner-only.
            prompt_text = text
            task_content = intent.cleaned_text or intent_text
        else:
            prompt_text = intent.cleaned_text or intent.raw_text
            task_content = prompt_text

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
                    # WS-3: owner-authored content only (never the quoted composite)
                    content=task_content,
                    priority="medium",
                    created_at=datetime.now(UTC).isoformat(),
                    # WS-3: source is channel-agnostic (conversation_intent), so
                    # stamp origin by channel — owner-attended (terminal/Telegram)
                    # carries dispatch authority; gateway channels → external.
                    origin_class=task_detected_origin(channel),
                    skip_if_duplicate=True,
                )
            except Exception:
                logger.error("Could not emit task_detected observation", exc_info=True)

        session = await cc_sessions.get_active_foreground(
            self._db, user_id=user_id, channel=str(channel),
            thread_id=thread_id,
        )
        if session and self._should_reset(session):
            self._session_locks.pop(session["id"], None)
            await self._session_mgr.complete(session["id"])
            session = None

        model = intent.model_override or (
            CCModel(session["model"]) if session and session.get("model") else self._default_model
        )
        effort = intent.effort_override or (
            EffortLevel(session["effort"]) if session and session.get("effort") else self._default_effort
        )

        session = await self._session_mgr.get_or_create_foreground(
            user_id=user_id, channel=channel, model=model, effort=effort,
            thread_id=thread_id, chat_id=chat_id,
        )

        # Set session context for eval attribution (same as handle_message).
        from genesis.observability.session_context import set_session_id as _set_sid
        _set_sid(session["id"])

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
            # Every fresh (non-resumed) CC session gets the recovery recap —
            # the old `or not session.get("message_count")` clause read a
            # column that does not exist (always falsy), so this HAS always
            # fired on fresh sessions; the condition now says so honestly.
            if not cc_sid:
                recovery_context = await self._build_recovery_context(
                    str(chat_id) if chat_id else user_id.replace("tg-", ""),
                    channel,
                    thread_id,
                )
                if recovery_context and on_event:
                    await on_event(StreamEvent(
                        event_type="system_notice",
                        text="Session restarted — injecting recent context.",
                    ))

            resume_overrides: dict = {}
            if cc_sid:
                resume_overrides, cc_sid = self._reconstruct_resume(session, cc_sid)
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
                # Always tell a fresh telegram session which chat it is in
                # (enables the scoped conversation_history scroll-up). Use the
                # REAL chat id (correct in groups); fall back to the DM
                # convention (user id == chat id in private chats).
                system_prompt += self._conversation_identity_block(
                    str(chat_id) if chat_id else user_id.replace("tg-", ""),
                    channel,
                    thread_id,
                )
                if recovery_context:
                    system_prompt += (
                        "\n\n## Recent conversation (session recovered)\n"
                        + recovery_context
                    )
                resume_id = None

            # Topic-aware context: inject for BOTH new and resumed sessions.
            # For new sessions, this adds to the system prompt directly.
            # For resumed sessions, append_system_prompt=True means CC CLI
            # appends this via --append-system-prompt alongside --resume,
            # giving the LLM fresh proposal state and thread history on
            # every message regardless of session age.
            if thread_id:
                topic_ctx = await self._build_topic_context(thread_id)
                if topic_ctx:
                    if system_prompt:
                        system_prompt += topic_ctx
                    else:
                        system_prompt = topic_ctx

            # Route long research off this turn to the durable background lane
            # (dispatched channels end the turn). See _apply_research_routing.
            system_prompt = _apply_research_routing(system_prompt, channel)

            invocation = CCInvocation(
                prompt=prompt_text,
                model=model,
                effort=effort,
                system_prompt=system_prompt,
                resume_session_id=resume_id,
                skip_permissions=True,
                append_system_prompt=True,
                session_key=session_key,
                roster_eligible=True,
                # WS-3 B4/gate-4: spare the pushed-surfaces enforce drop ONLY for
                # owner-attended channels (terminal/Telegram). A gateway
                # conversation (web/OpenClaw, WhatsApp, voice) is NOT owner-
                # authenticated, so it stays unsupervised — fail-closed toward
                # dropping wrapped-external pushed content, never injecting it.
                supervised=is_owner_attended_channel(channel),
                # WS-3 gate-4 producer half: gateway → external_untrusted so the
                # session's own memory/observation_write calls are stamped
                # untrusted (owner-attended → None → first_party coalesce).
                origin=session_origin_for_channel(channel),
                # Owner-attended interactive session: keep the full user-scoped
                # MCP toolset. Opt OUT of secure-by-default strict scoping
                # (see CCInvocation.strict_mcp_config).
                strict_mcp_config=False,
                **resume_overrides,
            )

            # Phase 3: track what this turn actually DID. Two different stakes:
            #
            #   text  — answer text reached the user, so failing over would
            #           double-output. Cosmetic-but-confusing.
            #   tools — the peer executed MCP tools, so re-running the prompt on
            #           another peer can REPEAT the effect: an outreach send, a
            #           database write. These invocations carry the full
            #           user-scoped toolset with permission checks skipped, so
            #           there is nothing downstream to catch a duplicate.
            #
            # Only `text` was tracked before, and a comment here asserted
            # "tool_use progress is fine before a failover". That was true while
            # failover happened solely on an exception; it stopped being true when
            # an empty non-error return also advanced to the next peer.
            streamed = {"text": False, "tools": False}

            async def _failover_tracked(ev: StreamEvent) -> None:
                if ev.event_type == "text" and ev.text:
                    streamed["text"] = True
                if on_event:
                    await on_event(ev)

            try:
                output, session = await self._try_invoke_streaming(
                    invocation, session=session, was_resume=bool(cc_sid),
                    prompt_text=prompt_text, model=model, effort=effort,
                    user_id=user_id, channel=channel, thread_id=thread_id,
                    on_event=_failover_tracked,
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
                # Phase 3: failover to a roster peer (full tools) BEFORE contingency
                # — but only if NO answer text streamed yet (else re-streaming the
                # peer's reply would double-output to the user).
                if not streamed["text"]:
                    roster_reply = await self._try_roster_failover(
                        invocation, session=session, channel=channel,
                        model=model, effort=effort, prompt_text=prompt_text,
                        on_event=_failover_tracked, streamed=streamed,
                    )
                    if roster_reply is not None:
                        return roster_reply
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
                # Both fallbacks failed → the user got no answer. Park the turn
                # durably so it auto-resumes when capacity returns (rate_limit_park
                # owns the reset parse, the cc_sessions resume-time write, and the
                # mode-aware copy — replacing the old sentence nothing backed).
                outcome = await rate_limit_park.park_conversation(
                    self._db,
                    prompt=prompt_text,
                    origin_session_id=session["id"],
                    exc=e,
                    model=model,
                    effort=effort,
                )
                return outcome.copy
            except CCMCPError as e:
                self._fire_failure_detection("mcp_error")
                server = f" ({e.server_name})" if e.server_name else ""
                return f"[MCP error{server} — try again]"
            except CCError as e:
                self._fire_failure_detection("generic_error")
                return f"[Genesis error: {e}]"

            # Store cc_session_id from first response (non-critical — next
            # turn re-checks the guard so a transient DB lock just delays
            # session resume by one turn).
            if not session.get("cc_session_id") and output.session_id:
                try:
                    await cc_sessions.update_cc_session_id(
                        self._db, session["id"], cc_session_id=output.session_id,
                    )
                    await self._persist_roster_endpoint(session["id"], output)
                except Exception:
                    logger.warning("Failed to store cc_session_id", exc_info=True)

            # Phase 3: reaching here means the HOME model succeeded — if we were in
            # a fallback, that's recovery. Clear the account-wide flag + this
            # session's sticky peer session (failover returns early, never here).
            await self._maybe_clear_fallback(session)

            # Activity timestamp — non-critical, but the stale-session reaper
            # (SessionManager.cleanup_stale) keys on it so persistent
            # failures deserve monitoring (WARNING, not debug).
            try:
                await self._session_mgr.update_activity(session["id"])
            except Exception:
                logger.warning("Failed to update session activity", exc_info=True)

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

            parts = self._formatter.format(output.text + _bg_notice(output), channel=channel)

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
        except (
            CCRateLimitError,
            CCQuotaExhaustedError,
            CCTimeoutError,
            CCNetworkOfflineError,
        ):
            # Rate limits are account-wide, and a timeout is NOT a stale-resume
            # failure — retrying fresh won't help. A timeout retry just burns a
            # second full window (the 2026-06-30 DM double-timeout). Likewise a
            # network-offline preflight (PR-3): the resume session is fine, the
            # internet is down — DON'T fail the live session as stale-resume and
            # retry fresh (which would also just re-raise offline). Let the
            # caller's terminal handler deal with it.
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
                session_id=session["id"], session_key=invocation.session_key,
                channel=channel,
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
        except (
            CCRateLimitError,
            CCQuotaExhaustedError,
            CCTimeoutError,
            CCNetworkOfflineError,
        ):
            # Account-wide (rate/quota) or a timeout — retrying fresh won't help;
            # a timeout retry just burns a second full window (2026-06-30 DM). A
            # network-offline preflight (PR-3) likewise must NOT fail the live
            # session as a stale resume — the internet is down, not the session.
            raise
        except CCError:
            if not was_resume:
                raise
            session = await self._recover_stale_resume(
                session, user_id=user_id, channel=channel,
                thread_id=thread_id, model=model, effort=effort,
            )
            fresh_inv = await self._build_fresh_invocation(
                prompt_text, model=model, effort=effort,
                session_id=session["id"], session_key=invocation.session_key,
                channel=channel,
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
        session_key: str | None = None,
        channel: ChannelType | None = None,
    ) -> CCInvocation:
        """Build a fresh invocation (with system prompt, no resume)."""
        system_prompt = await self._assembler.assemble(
            db=self._db, model=str(model), effort=str(effort),
            session_id=session_id,
        )
        system_prompt = await self._enrich_with_context(system_prompt, prompt_text)
        # A stale-resume retry rebuilds the prompt from scratch — re-apply the
        # dispatched-channel research routing so the nudge isn't lost on recovery.
        system_prompt = _apply_research_routing(system_prompt, channel)
        return CCInvocation(
            prompt=prompt_text,
            model=model,
            effort=effort,
            system_prompt=system_prompt,
            resume_session_id=None,
            skip_permissions=True,
            append_system_prompt=True,
            session_key=session_key,  # cc-loop-01: keep /stop working on retry
            roster_eligible=True,  # fresh retry stays roster-routable (no resume)
            # WS-3 B4/gate-4 (fresh retry): supervised ONLY for owner-attended
            # channels (terminal/Telegram); gateway conversations stay
            # unsupervised. Mirrors the primary invocation sites above.
            supervised=is_owner_attended_channel(channel),
            origin=session_origin_for_channel(channel),  # WS-3 gate-4 producer half
            # Owner-attended interactive session: keep the full user-scoped MCP
            # toolset. Opt OUT of secure-by-default strict scoping
            # (see CCInvocation.strict_mcp_config).
            strict_mcp_config=False,
        )

    @staticmethod
    def _parse_session_metadata(session: dict) -> dict:
        """Parse a session row's JSON ``metadata`` to a dict ({} on missing/corrupt)."""
        raw = session.get("metadata")
        if not raw:
            return {}
        try:
            md = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return {}
        return md if isinstance(md, dict) else {}

    @classmethod
    def _session_roster_endpoint(cls, session: dict) -> dict | None:
        """Parse a persisted ``roster_endpoint`` payload from a session's JSON
        metadata, or None (native session / no payload / corrupt)."""
        ep = cls._parse_session_metadata(session).get("roster_endpoint")
        return ep if isinstance(ep, dict) else None

    @classmethod
    def _session_fallback_session(cls, session: dict) -> dict | None:
        """Parse the per-session STICKY peer-session payload (``fallback_session``)
        from JSON metadata, or None. Holds ``{cc_session_id, roster_model}`` for the
        peer continuation resumed across consecutive outage turns."""
        fs = cls._parse_session_metadata(session).get("fallback_session")
        return fs if isinstance(fs, dict) else None

    def _reconstruct_resume(
        self, session: dict, cc_sid: str,
    ) -> tuple[dict, str | None]:
        """Rebuild roster overrides for resuming a routed session on its ORIGINAL
        endpoint. Returns (override_kwargs, cc_sid) — cc_sid is set to None (force
        fresh) if the session was routed but its endpoint can't be reconstructed,
        so we never resume a routed session on native Claude (corruption)."""
        ep = self._session_roster_endpoint(session)
        if ep is None:
            return {}, cc_sid  # native session — resume as-is
        try:
            return roster.overrides_from_persisted(ep), cc_sid
        except roster.RosterError:
            logger.error(
                "Cannot reconstruct routed endpoint for session %s — starting "
                "fresh (refusing native resume of a routed session)",
                session["id"][:8], exc_info=True,
            )
            return {}, None

    async def _persist_roster_endpoint(self, session_id: str, output: Any) -> None:
        """Persist the endpoint a ROUTED session ran on, so it resumes on the same
        provider. Keyed off CCOutput.roster_model (the NAME the chokepoint actually
        selected — ground truth), NOT the provider's self-reported model_used which
        may be a variant string or empty. No-op for native Claude. Token is never
        stored — only the auth-env NAME (see roster.endpoint_payload)."""
        rm = getattr(output, "roster_model", "") or ""
        if not rm or rm == roster.CLAUDE:
            return
        payload = roster.endpoint_payload(rm)
        if payload:
            await cc_sessions.merge_metadata(
                self._db, session_id, {"roster_endpoint": payload},
            )

    # ---- Phase 3: conversation failover (STICKY) ------------------------------

    async def _merge_session_metadata(self, session_id: str, patch: dict) -> None:
        """Shallow-merge a patch into a session's JSON metadata (best-effort)."""
        try:
            await cc_sessions.merge_metadata(self._db, session_id, patch)
        except Exception:
            logger.warning("Failed to merge session metadata", exc_info=True)

    async def _invoke_peer(
        self,
        inv: CCInvocation,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None,
    ) -> Any:
        """Invoke a failover peer — streaming when the turn is streaming, else not."""
        if on_event is not None:
            return await self._invoker.run_streaming(inv, on_event=on_event)
        return await self._invoker.run(inv)

    async def _run_failover_peer(
        self,
        peer_name: str,
        peer_inv: CCInvocation,
        *,
        sticky: dict | None,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None,
        streamed: dict | None = None,
    ) -> Any:
        """Run one peer turn. If this conversation has a STICKY session for THIS
        peer, resume it for continuity; on a stale resume (non-rate-limit CCError)
        retry once FRESH on the same peer — UNLESS answer text already streamed (a
        fresh retry would re-stream and double-output). Rate-limit/quota propagate
        to the caller (which moves to the next peer)."""
        inv = peer_inv  # fresh by default (failover_invocations set resume=None)
        if (
            sticky
            and sticky.get("roster_model") == peer_name
            and sticky.get("cc_session_id")
        ):
            inv = replace(peer_inv, resume_session_id=sticky["cc_session_id"])
        try:
            return await self._invoke_peer(inv, on_event)
        except (CCRateLimitError, CCQuotaExhaustedError, CCNetworkOfflineError):
            # Offline joins the fast-re-raise (same class as CAVEAT A): a dead
            # network is not a stale peer resume — retrying fresh won't help and
            # must not mark the sticky peer session stale.
            raise
        except CCError as exc:
            # Don't re-run: nothing to recover if already fresh, and never once
            # answer text has reached the user (would double-output) — this is a
            # full re-run of the same prompt on the same peer.
            #
            # And never for a provider refusal. A DRAINED prepaid account arrives
            # here as a generic CCProcessError rather than CCQuotaExhaustedError,
            # because the invoker's global classifier deliberately does not know
            # the balance phrases (teaching it would let a drained BACKUP report
            # the primary as down — see peer_availability._BALANCE_REFUSALS). So
            # it lands on this branch and buys a full second invocation of the
            # same dead peer before the caller ever classifies it. A fresh
            # session cannot refill an empty balance.
            #
            # Module-level import, deliberately: a DEFERRED import here would sit
            # inside the `except` block, and an ImportError from it is not a
            # CCError — it would skip both handlers in the peer loop, land on the
            # outer `except Exception`, and abandon every remaining peer. That is
            # the same outage-amplifier shape the never-raises guard exists to
            # prevent. `peer_availability` imports only stdlib and `genesis.env`,
            # so there is no cycle to work around.
            if (
                inv.resume_session_id is None
                or (streamed and streamed.get("text"))
                or peer_availability.is_provider_refusal(exc)
            ):
                raise
            logger.warning(
                "failover peer %s sticky resume failed — retrying fresh", peer_name,
            )
            return await self._invoke_peer(peer_inv, on_event)

    async def _try_roster_failover(
        self,
        base_inv: CCInvocation,
        *,
        session: dict,
        channel: ChannelType,
        model: CCModel,
        effort: EffortLevel,
        prompt_text: str,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None = None,
        streamed: dict | None = None,
    ) -> str | None:
        """STICKY conversation failover. During an account-wide home-model outage,
        run the turn on a roster peer (full tools) BEFORE the degraded contingency
        path. Returns the formatted reply on success, or None to fall through to
        contingency. Never raises (failover must not break the turn)."""
        try:
            from genesis.cc import fallback_state
            home = roster.active_model()
            # A resume turn carries no system prompt (identity lives in the home CC
            # session being resumed). The peer runs a FRESH session, so re-assemble
            # the identity/context — otherwise the peer answers with no Genesis
            # persona/instructions.
            if base_inv.system_prompt is None:
                system_prompt = await self._assembler.assemble(
                    db=self._db, model=str(model), effort=str(effort),
                    session_id=session["id"],
                )
                system_prompt = await self._enrich_with_context(
                    system_prompt, prompt_text,
                )
                base_inv = replace(base_inv, system_prompt=system_prompt)
            peers = roster.failover_invocations(home, base_inv)
            if not peers:
                # Say so. This is the one branch that degrades SILENTLY at the
                # exact moment the fallback exists for — the subscription has
                # capped and there is nothing to fail over to. The turn goes on
                # to contingency and rate_limit_park, which do surface something
                # to the user, but nothing anywhere names the actual cause: no
                # usable peer is configured. `failover_chain` also drops any
                # peer whose auth_env is unset, so "declared but keyless" lands
                # here too and looks identical to "none declared".
                logger.warning(
                    "CC failover: no usable roster peer for home=%r — degrading "
                    "to contingency. Declare one in "
                    "~/.genesis/config/cc_roster.local.yaml (a peer whose "
                    "auth_env key is unset is skipped).",
                    home,
                )
                return None
            async def _record_peer(fn, *args) -> bool:
                """Run a peer_availability recorder OFF the event loop, safely.

                The recorder itself is exhaustively guarded against raising,
                because a raise here escapes into the peer loop and abandons
                every REMAINING peer. Moving it to a worker thread put that
                guarantee back at risk: `asyncio.to_thread` raises RuntimeError
                once the default executor is shut down, which the outer handler
                catches by returning None — abandoning the whole failover, which
                is strictly worse than the lost row it was protecting. Advisory
                bookkeeping must never decide whether the user gets an answer.

                CancelledError is deliberately re-raised: it is a BaseException
                and means the turn itself is going away.
                """
                try:
                    return await asyncio.to_thread(fn, *args)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("peer availability record failed", exc_info=True)
                    return False

            async def _remember_peer_session(out) -> None:
                """Persist this peer's session id so the turn can be continued.

                Only with a real session id: an empty one cannot resume anything.
                One named writer rather than an inline block, so any future path
                that ends the turn on a peer has exactly one thing to call.
                """
                if not getattr(out, "session_id", ""):
                    return
                await self._merge_session_metadata(
                    session["id"],
                    {"fallback_session": {
                        "cc_session_id": out.session_id,
                        "roster_model": peer_name,
                    }},
                )

            sticky = self._session_fallback_session(session)
            for peer_name, peer_inv in peers:
                if streamed and streamed.get("text"):
                    break  # a prior peer already streamed answer text — can't fail
                    # over to another without double-output; degrade instead.
                try:
                    output = await self._run_failover_peer(
                        peer_name, peer_inv, sticky=sticky,
                        on_event=on_event, streamed=streamed,
                    )
                except (CCRateLimitError, CCQuotaExhaustedError) as exc:
                    # The prose lives HERE, not in the availability record. This
                    # handler previously logged nothing at all, while the record
                    # persisted the provider's text into a file that is read into
                    # every health snapshot and JSON-dumped into an LLM prompt.
                    # The log is both the better home for it and outside that
                    # exposure path, so the record carries no free text.
                    logger.warning(
                        "failover peer %s refused: %s", peer_name, exc, exc_info=True,
                    )
                    # Provider refused on a usage ceiling — real evidence about the
                    # peer. Recording is advisory and never changes which peers are
                    # tried; it exists so a blocked standby is VISIBLE, since the
                    # roster admits a peer on credential presence alone.
                    # to_thread: the recorder takes a lock with bounded retry
                    # sleeps, then a tempfile write, fsync and replace. Run inline
                    # it blocks the event loop — stalling every other conversation
                    # during the very outage it exists to observe.
                    # This branch is no longer refusals-only: since the MCP
                    # exclusion, a tool's own 429 arrives HERE typed as a
                    # rate-limit error and is correctly DECLINED as evidence.
                    # So the declined-plus-streamed cleanup below applies on
                    # this branch too — without it, a previously blocked peer
                    # that just SERVED text stayed falsely blocked for days
                    # because its clearing lived only on the generic branch.
                    declined = not peer_availability.is_provider_refusal(exc)
                    await _record_peer(peer_availability.note_failure, peer_name, exc)
                    if streamed and streamed.get("text"):
                        if declined:
                            await _record_peer(
                                peer_availability.note_success, peer_name,
                            )
                        # Text already reached the user this turn. Returning ""
                        # (not None) stops the caller running contingency, which
                        # would stack a SECOND answer on the first.
                        return ""
                    continue  # this peer is also down → try the next one
                except CCError as exc:
                    logger.warning("failover peer %s failed", peer_name, exc_info=True)
                    # Routed through the SAME classifier on purpose: a local fault
                    # (offline — which never left the box — our own timeout, an MCP
                    # server crash, a stale sticky session) is a CCError here too,
                    # but is not evidence about the peer. note_failure declines it,
                    # so one local blip can't mark the whole standby fleet down.
                    # Classified SEPARATELY from recording: `note_failure` returns
                    # False for four different reasons, so reading its return as
                    # "declined" let a transient write failure flip a refusal into
                    # a recorded success.
                    declined = not peer_availability.is_provider_refusal(exc)
                    await _record_peer(peer_availability.note_failure, peer_name, exc)
                    if streamed and streamed.get("text"):
                        if declined:
                            # The peer ANSWERED — text is on the user's screen —
                            # and then a local fault ended the turn. A PRIOR block
                            # must not survive an attempt that demonstrably served
                            # from this peer; records only refresh during a home
                            # outage, so a stale "blocked" stands for days.
                            await _record_peer(
                                peer_availability.note_success, peer_name,
                            )
                        return ""  # double-output guard, as above
                    continue
                # Record availability only when the peer DEMONSTRABLY served the
                # turn: a usable output, or answer text already on the user's
                # screen. The degenerate empty non-error output (a silent cap)
                # otherwise takes the success path below — behaviour identical to
                # what shipped before this feature — and recording "available" on
                # it would clear a real block with a turn that showed nothing.
                # What to DO about that empty reply (advance? dead-end?) is retry
                # policy, deliberately out of scope here; the effects-guard
                # follow-up owns it.
                usable = not output.is_error and bool((output.text or "").strip())
                if usable or (streamed and streamed.get("text")):
                    await _record_peer(peer_availability.note_success, peer_name)
                # Success on this peer. Record the account-wide flag + this session's
                # sticky peer session (only with a real session id, else continuity
                # can't resume). Home identity in cc_sessions stays on Claude.
                transitioned = fallback_state.enter(home, peer_name, "rate_limit")
                await _remember_peer_session(output)
                # Keep the session fresh. Cost + triage are intentionally NOT recorded
                # for failover turns: CC's cost_usd is bogus for routed models, and
                # triage must not attribute a peer model's output to the home model's
                # learning signal.
                try:
                    await self._session_mgr.update_activity(session["id"])
                except Exception:
                    logger.debug("activity update on failover failed", exc_info=True)
                if transitioned:
                    await self._fire_fallback_alert(
                        topic="cc_fallback_switch",
                        context=(
                            f"<b>CC failover</b>\n\n{home} is rate-limited — replies "
                            f"are now running on <b>{peer_name}</b> with full tools. "
                            f"Genesis returns to {home} automatically on recovery."
                        ),
                    )
                parts = self._formatter.format(output.text + _bg_notice(output), channel=channel)
                return "\n".join(parts)
            return None
        except Exception:
            logger.error(
                "roster failover errored — falling through to contingency",
                exc_info=True,
            )
            return None

    async def _maybe_clear_fallback(self, session: dict) -> None:
        """On a successful HOME-model turn, clear any prior fallback — this session's
        sticky peer session (foreground-specific) AND the account-wide flag (via the
        shared helper, which fires one recovery ALERT). Reached only when the home
        invocation actually succeeded (= genuine recovery; failover returns early and
        never falls through to here)."""
        try:
            if self._session_fallback_session(session) is not None:
                await self._merge_session_metadata(
                    session["id"], {"fallback_session": None},
                )
            from genesis.cc.fallback_recovery import note_home_recovery
            await note_home_recovery()
        except Exception:
            logger.warning("fallback recovery handling failed", exc_info=True)

    async def _fire_fallback_alert(self, *, topic: str, context: str) -> None:
        """Fire-and-forget CC-fallback ALERT (never crash the turn). Delegates to the
        shared module helper — same impl used for the switch alert here and for
        background/probe recovery in genesis.cc.fallback_recovery."""
        from genesis.cc.fallback_recovery import fire_fallback_alert
        await fire_fallback_alert(topic=topic, context=context)

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
        pending proposal board AND recent thread messages so the CC session
        can discuss and resolve proposals with full conversational context.
        """
        if self._db is None:
            return None
        try:
            # Look up which topic this thread_id belongs to
            async with self._db.execute(
                "SELECT category, chat_id FROM telegram_topics WHERE thread_id = ?",
                (int(thread_id),),
            ) as cur:
                row = await cur.fetchone()
            if not row or row[0] != "ego_proposals":
                return None
            topic_chat_id = row[1]

            # Fetch pending proposals
            from genesis.db.crud import ego as ego_crud

            # User-ego scoped (with pre-migration NULL fallback) so
            # Genesis-ego proposals stay off the user board — matches the
            # resolver (ego_proposal_resolve) and UserEgoContextBuilder.
            pending = await ego_crud.list_proposals(
                self._db, status="pending", limit=10, ego_source="user_ego_cycle",
            )
            if not pending:
                pending = await ego_crud.list_proposals(
                    self._db, status="pending", limit=10,
                )

            lines = ["\n\n## You Are in the Ego Proposals Topic\n"]
            lines.append(
                "The user communicates with you here to review, approve, reject, "
                "or discuss ego proposals. When the user indicates approval "
                "(e.g., 'do it', 'yes', 'go ahead', 'approve 1'), resolve the "
                "proposal. When they reject, mark it rejected with their reason.\n"
            )

            # ── Recent thread messages (scroll-up) ──────────────────────
            # Fetch the last few messages so the LLM sees the actual digest
            # messages the ego sent, not just an abstract proposal board.
            # This is critical for understanding references like "this one"
            # or "the older ones" — the user is responding to what they SEE
            # in the thread, not to an internal data structure.
            thread_messages = await self._fetch_thread_messages(
                int(thread_id), chat_id=topic_chat_id, limit=8,
            )
            if thread_messages:
                lines.append("### Recent Messages in This Thread:\n")
                for m in thread_messages:
                    sender = m.get("sender", "?")
                    content = m.get("content", "")
                    # Truncate very long messages but keep enough to see
                    # proposal digests and their numbered items
                    if len(content) > 800:
                        content = content[:800] + "…"
                    prefix = "User" if sender == "user" else "Genesis"
                    lines.append(f"**{prefix}**: {content}\n")

            # ── Pending proposals board ─────────────────────────────────
            if not pending:
                lines.append("\n### Pending Proposals:\n\nNone.\n")
            else:
                lines.append("### Pending Proposals:\n")
                for i, p in enumerate(pending, 1):
                    cat = p.get("action_category", "unknown")
                    content = (p.get("content") or "")[:120]
                    pid = p["id"]
                    lines.append(f"{i}. **[{cat}]** {content}")
                    lines.append(f"   ID: `{pid}`\n")

            lines.append(
                "\n### To resolve a proposal:\n"
                "Use the `ego_proposal_resolve` MCP tool. PREFER `proposal_ids` "
                "(the `ID:` shown under each item above) — it targets exactly that "
                "proposal regardless of batch/digest, so it can never resolve the "
                "wrong one:\n"
                "- Approve specific (preferred): `ego_proposal_resolve(action=\"approve\", "
                "proposal_ids=\"<id>\")`\n"
                "- Reject with reason: `ego_proposal_resolve(action=\"reject\", "
                "proposal_ids=\"<id>\", reason=\"not relevant right now\")`\n"
                "- Approve all pending: `ego_proposal_resolve(action=\"approve\")`\n"
                "- Positional numbers (`proposal_numbers=\"1\"`) index THIS board "
                "(top to bottom); use only when no ID is available.\n"
                "\n### When rejecting with a reason, distill the ruling:\n"
                "- If the reason states a STANDING position (a rule that should\n"
                "  bind future cycles, not just this proposal), also pass\n"
                "  `standing_rule=\"[type/category] one-sentence ruling\"` — it\n"
                "  becomes a durable Settled Decision the ego always sees.\n"
                "- If the rejection is situational ('not right now'), pass\n"
                "  `one_off=true` so no standing decision is recorded.\n"
                "\n### Important:\n"
                "- Match user intent to the proposals visible in the thread above.\n"
                "  If the user says 'this one', they mean the most recently presented\n"
                "  proposal — resolve it by its `ID:`. 'The older ones' means\n"
                "  proposals listed under the '📋 N older proposal(s)' header in the\n"
                "  digest.\n"
                "- If the user states a RULING (settles a question, sets a standing\n"
                "  rule, overrules an assumption) outside a reject flow, capture it\n"
                "  with the `ego_decision` MCP tool. Soft guidance and preferences\n"
                "  go to `memory_store` instead.\n"
                "- Always confirm what you did: 'Approved proposal 1: [content]'\n"
            )
            return "\n".join(lines)
        except Exception:
            logger.debug("Failed to build topic context", exc_info=True)
            return None

    async def _fetch_thread_messages(
        self, thread_id: int, *, chat_id: int | None = None, limit: int = 8,
    ) -> list[dict]:
        """Fetch recent messages from a Telegram thread (scroll-up).

        Uses both chat_id and thread_id to avoid cross-group leakage
        (thread_ids are scoped per chat in Telegram).
        """
        if self._db is None:
            return []
        try:
            if chat_id is not None:
                query = """SELECT sender, content, timestamp FROM telegram_messages
                           WHERE chat_id = ? AND thread_id = ?
                           ORDER BY timestamp DESC LIMIT ?"""
                params = (chat_id, thread_id, limit)
            else:
                query = """SELECT sender, content, timestamp FROM telegram_messages
                           WHERE thread_id = ?
                           ORDER BY timestamp DESC LIMIT ?"""
                params = (thread_id, limit)
            async with self._db.execute(query, params) as cur:
                rows = await cur.fetchall()
            # Return in chronological order (oldest first)
            return [dict(r) for r in reversed(rows)]
        except Exception:
            logger.debug("Failed to fetch thread messages", exc_info=True)
            return []

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

    # Total BYTE budget for the recovery recap (env-overridable settings
    # lever: GENESIS_RECOVERY_CONTEXT_BUDGET). Sized so several full-length
    # analytical replies survive — the old per-message 300-char HEAD chop
    # dropped exactly the part that matters (numbered options/conclusions sit
    # at the END of long replies; measured miss 2026-08-18: "option 3" at char
    # ~3,850 of a 4,463-char reply).
    RECOVERY_CONTEXT_BUDGET = 6000
    RECOVERY_CONTEXT_MESSAGES = 20

    @staticmethod
    def _conversation_identity_block(
        chat_ref: str,
        channel: ChannelType,
        thread_id: str | None,
    ) -> str:
        """One prompt block telling the session WHICH chat it is in, so it can
        scroll up on demand. Without an explicit chat_id the model cannot make
        a scoped ``conversation_history`` call — the 2026-08-18 failure mode
        was a session truthfully claiming earlier context "isn't retrievable"
        while the full thread sat one tool call away.

        ``chat_ref`` is the REAL chat id (the handler's ``msg.chat.id`` —
        negative for groups), optionally ``tg-``-prefixed. Never pass a
        sender/user id here: in group/topic sessions the sender's personal id
        is a valid-looking number that would misdirect scoped scroll-up at
        the sender's private DM.
        """
        if str(channel) != "telegram":
            return ""
        chat_id_str = chat_ref.replace("tg-", "")
        try:
            int(chat_id_str)  # negative group ids are valid
        except ValueError:
            return ""
        thread_note = f", thread_id={thread_id}" if thread_id else ""
        # Scope the suggested call to the ACTIVE topic when in a forum thread —
        # an unscoped group call would pull unrelated topics' messages.
        thread_arg = f", thread_id={thread_id}" if thread_id else ""
        return (
            "\n\n## Conversation identity\n"
            f"This is the Telegram chat with chat_id={chat_id_str}{thread_note}. "
            "When the user references earlier conversation that is not in your "
            "context, SCROLL UP before claiming it is unavailable: call "
            f"`conversation_history(channel='telegram', chat_id={chat_id_str}"
            f"{thread_arg}, limit=50)` (add `before=<oldest timestamp seen>` to "
            "page further back). Messages return full-length."
        )

    async def _build_recovery_context(
        self,
        chat_ref: str,
        channel: ChannelType,
        thread_id: str | None,
    ) -> str:
        """Load recent messages for session recovery context injection.

        ``chat_ref``: the real chat id (optionally ``tg-``-prefixed; negative
        for groups) — same contract as ``_conversation_identity_block``.

        Byte-budgeted and TAIL-biased: messages are kept whole newest-first
        until the budget runs low; a message too large for the remaining
        budget keeps its END (marked with a leading ellipsis), because that is
        where long analytical replies put their conclusions and option lists.
        Returns a formatted string of recent conversation, or "" if none.
        """
        if str(channel) != "telegram":
            return ""
        try:
            import os

            from genesis.db.crud.telegram_messages import query_recent

            try:
                chat_id = int(chat_ref.replace("tg-", ""))
            except ValueError:
                return ""

            try:
                budget = int(
                    os.environ.get("GENESIS_RECOVERY_CONTEXT_BUDGET", "")
                    or self.RECOVERY_CONTEXT_BUDGET,
                )
            except ValueError:
                budget = self.RECOVERY_CONTEXT_BUDGET
            budget = max(500, budget)

            messages = await query_recent(
                self._db,
                chat_id,
                thread_id=int(thread_id) if thread_id else None,
                limit=self.RECOVERY_CONTEXT_MESSAGES,
            )
            if not messages:
                return ""

            def _tail_by_bytes(s: str, max_bytes: int) -> str:
                """Longest end-slice of ``s`` whose UTF-8 length ≤ max_bytes, cut
                on a CHARACTER boundary. Slicing raw bytes then decoding with
                errors='ignore' drops only the leading partial multibyte char, so
                the result is always valid UTF-8 (never a U+FFFD)."""
                if max_bytes <= 0:
                    return ""
                encoded = s.encode()
                if len(encoded) <= max_bytes:
                    return s
                return encoded[-max_bytes:].decode("utf-8", errors="ignore")

            # Walk newest → oldest, spending the BYTE budget where recency is;
            # then restore chronological order for readability. Bytes (not chars)
            # so a multibyte-heavy transcript can't balloon the real payload ~3-4x.
            kept: list[str] = []
            remaining = budget  # bytes
            for m in reversed(messages):
                content = str(m.get("content") or "")
                if not content:
                    continue
                prefix = "User" if m.get("sender") == "user" else "Genesis"
                line = f"{prefix}: {content}"
                # Charge the "\n" that "\n".join will insert before this line
                # (one per line after the first) so the budget is enforced on the
                # ACTUAL recap size, not the sum of lines alone.
                sep = 1 if kept else 0
                line_bytes = len(line.encode()) + sep
                if line_bytes <= remaining:
                    kept.append(line)
                    remaining -= line_bytes
                elif remaining - sep > 200:
                    # Tail-keep: the end of a long reply carries its
                    # conclusions/option lists — never the head alone. Measured
                    # in bytes, cut on a char boundary so the recap stays valid
                    # UTF-8. Reserve the separator + exact marker cost so the kept
                    # entry (and its joining newline) fits.
                    marker = f"{prefix}: …"
                    tail = _tail_by_bytes(
                        content, remaining - sep - len(marker.encode()),
                    )
                    kept.append(marker + tail)
                    remaining = 0
                else:
                    # Doesn't fit and no room for a meaningful tail — STOP
                    # rather than skip: appending a smaller OLDER message here
                    # would leave an unmarked hole mid-recap.
                    break
                if remaining <= 0:
                    break
            if not kept:
                return ""
            kept.reverse()
            recap = "\n".join(kept)
            logger.info(
                "Recovery context built for chat %s: %d msgs, %d/%d bytes",
                chat_id,
                len(kept),
                len(recap.encode()),
                budget,
            )
            return recap
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

    def _should_reset(self, session: dict, *, now: datetime | None = None) -> bool:
        """Check if session is from a previous day boundary.

        The boundary is local-midnight (``day_boundary_hour`` in the user's
        timezone), not UTC-midnight — otherwise the daily reset fires at the
        UTC-midnight instant instead of local midnight on any UTC-offset install
        (see :func:`genesis.util.tz.local_day_boundary`).

        Supergroup topic sessions (thread_id set) are persistent — they
        only compact when CC context limits are hit, never by day boundary.

        *now* is injectable for deterministic tests; defaults to the current
        instant.
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
        boundary = tz.local_day_boundary(self._day_boundary_hour, now=now)
        return started_dt < boundary
