"""LiveAgent: a persistent, fully Genesis-loaded Claude Code session.

Driven the way Codex drives its realtime voice: a turn either starts work or,
while work is running, STEERS it (start-or-steer); progress streams back as
short lines; interrupt is explicit; permission asks go to the host as
approvals that default to deny.

The session is the owner's own: it inherits the owner's shell environment and
loads every settings source (user, project, local, policy), so hooks, skills,
MCP servers and CLAUDE.md behave as in a terminal, and the SessionStart hook
registers it as an owner foreground session. That is why Phase 0 lets only the
``owner_cli`` initiator call ``turn``. A model-initiated turn (a voice model
delegating) needs the autonomous-CLI approval gate first, and is refused here
until that is wired. Turns the session injects itself (background-task
notifications, peer or scheduled messages) still run, as in any terminal
session; their permission asks reach the same approval queue, so each ask
shows the full tool input it would run.

Whether the session is busy comes from the CLI's own ``session_state_changed``
frames, never from counting results: a result is not one-per-sent-turn
(MEASURED on CLI 2.1.299: a ``priority: "now"`` steer aborts the running turn,
which yields its own result; steers sent close together are batched into one
turn and one result; background tasks inject turns with results of their own).
Results reach the host only as ``finished`` events.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TextIO

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    ToolPermissionContext,
)

from genesis.cc.gmodel_routes import strip_route_env
from genesis.live_agent.progress import ProgressSummarizer
from genesis.util.atomic import atomic_write_text
from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)

ACCEPTED_INITIATORS = frozenset({"owner_cli"})
OWNER_CLI_MARKER = "[The owner is typing this through the live-agent CLI.]"

# How long a permission ask waits for the owner. No hang is being bounded: the
# owner may simply be away, so the project's 2-hour floor applies. On expiry the
# ask is DENIED, never allowed.
APPROVAL_TIMEOUT_S = 7200.0
# Interrupt-mode steer: how long to wait for the CLI to report idle after the
# interrupt. Measured interrupt-to-abort: 0.33 s with a tool running. 60 s
# matches the SDK's own wait for a control-request reply, past which the
# interrupt itself has failed; the steer is then refused, never sent blind.
STEER_INTERRUPT_WAIT_S = 60.0
_FLUSH_TICK_S = 0.2
# The CLI's error when a saved session id no longer exists. Only this falls
# back to a fresh session; any other start failure is raised, so a transient
# fault never discards the saved session.
_STALE_RESUME = "No conversation found"
_IDLE = "idle"
# A tool this host cannot answer: the CLI expects the answers back in the
# tool input. Denied with a message, so the model asks in plain text instead.
_UNANSWERABLE_TOOLS = frozenset({"AskUserQuestion"})

# Variables that name ANOTHER session or a dispatched parent. Inherited, they
# would hand the agent another session's identity, messaging socket and slot,
# put its hooks in unattended mode (GENESIS_CC_SESSION turns an ask into a
# denial), stamp its memory writes with the wrong origin, or restrict its Bash.
# The SDK can add variables to the child but never remove them, so the host
# runs ``owner_environ`` over its own environment before starting the agent.
PARENT_SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_EFFORT",
    "CLAUDE_PLUGIN_DATA",
    "GENESIS_SLOT",
    "GENESIS_CC_SESSION",
    "GENESIS_SESSION_ID",
    "GENESIS_TRACE_ID",
    "GENESIS_PARENT_SPAN_ID",
    "GENESIS_SESSION_ORIGIN",
    "GENESIS_BASH_ALLOWLIST",
)
# Present after ``owner_environ``, any of these means the environment is still
# not the owner's (a routed peer's backend, or a dispatched session), so the
# agent refuses to start rather than run as the wrong session.
_MUST_BE_ABSENT = (*PARENT_SESSION_VARS, "GENESIS_ROSTER_MODEL")


def _validate_environment(environ: Mapping[str, str] | None = None) -> None:
    """The SDK inherits os.environ; an optional mapping can never hide it."""
    inherited = sorted(v for v in _MUST_BE_ABSENT if v in os.environ or v in (environ or {}))
    if inherited:
        raise RuntimeError(
            f"live agent refuses to start with {inherited} set: run owner_environ() "
            "over the host environment first"
        )


# A bare reply that answers a waiting approval (normalised: lower case, outer
# punctuation and commas dropped). Anything else is NOT an answer.
# Not "ok"/"okay" (filler, and what speech-to-text hears as acknowledgement) and
# not "y"/"n" (one keystroke from a typo); "/y ID" covers terse use.
_YES = frozenset({"yes", "yeah", "yep", "approve", "approved", "allow"})
_NO = frozenset({"no", "nope", "deny", "denied", "reject"})
# A bare reply to an ask that appeared less than this long ago is refused: it
# was more likely typed or said about whatever was on screen just before.
MIN_ASK_VISIBLE_S = 1.5


class ApprovalPending(RuntimeError):
    """A turn was held because a permission ask is waiting for the owner."""


def approval_reply(text: str) -> bool | None:
    """True / False when ``text`` is a bare yes / no, else None."""
    # A trailing "?" is a question, not an answer: it is NOT stripped.
    words = " ".join(text.lower().replace(",", " ").split()).strip(" .!")
    if words in _YES:
        return True
    if words in _NO:
        return False
    return None


def owner_environ(env: dict[str, str]) -> dict[str, str]:
    """Return ``env`` as the owner's own: parent identity dropped, route pins undone."""
    clean = {k: v for k, v in env.items() if k not in PARENT_SESSION_VARS}
    return strip_route_env(clean)


CostRecorder = Callable[[str, float, dict[str, Any]], Awaitable[None]]


@dataclass
class Event:
    """One thing the host should show: kind + a short line + raw details."""

    kind: str  # progress | approval | session | rate_limited | finished | error
    text: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskHandle:
    """What ``turn`` did: its id and whether it steered running work."""

    id: str
    steered: bool


@dataclass
class _Approval:
    id: str
    tool_name: str
    tool_input: dict[str, Any]
    reason: str
    future: asyncio.Future[bool]
    shown_at: float = field(default_factory=time.monotonic)


async def record_cost_on_session_row(
    session_id: str, cost_usd: float, usage: dict[str, Any]
) -> None:
    """Add a turn's cost to the session's cc_sessions row.

    The SessionStart hook registers terminal-convention rows (``id ==
    cc_session_id``). The CLI pinned here reports session-cumulative totals,
    which ``record_turn_cost`` diffs against its per-session cursor.
    """
    from genesis.db.connection import connect_aiosqlite_rw
    from genesis.db.crud.cc_sessions import record_turn_cost
    from genesis.env import genesis_db_path

    async with connect_aiosqlite_rw(genesis_db_path()) as db:
        found = await record_turn_cost(
            db,
            session_id,
            cc_session_id=session_id,
            reported_cost_usd=cost_usd,
            cumulative=True,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )
    if not found:
        logger.warning("live agent: no cc_sessions row %s; turn cost not recorded", session_id)


class LiveAgent:
    def __init__(
        self,
        *,
        cwd: Path,
        state_path: Path,
        steer: Literal["priority", "interrupt"] = "priority",
        approval_timeout_s: float = APPROVAL_TIMEOUT_S,
        client_factory: Callable[[ClaudeAgentOptions], Any] = ClaudeSDKClient,
        cost_recorder: CostRecorder | None = record_cost_on_session_row,
        cli_path: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        _validate_environment(environ)
        self._cwd = cwd
        self._state_path = state_path
        self._steer = steer
        self._approval_timeout_s = approval_timeout_s
        self._client_factory = client_factory
        self._cost_recorder = cost_recorder
        # The PATH symlink, deliberately not its resolved target: the process
        # name stays `claude`, which the SessionStart registration hook matches.
        # Without it the SDK would silently run its own bundled CLI, a
        # different version from the one every wire fact here was measured on.
        self._cli_path = cli_path or shutil.which("claude")
        if not self._cli_path:
            raise CLINotFoundError("`claude` is not on PATH; refusing the SDK's bundled CLI")
        self._client: Any = None
        self._host_lock: TextIO | None = None
        self._resume_attempt: str | None = None
        self._stale_resume_diagnostic = False
        self._reconnecting = False
        self._stream_open = False
        self._tasks: list[asyncio.Task[None]] = []
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._summary = ProgressSummarizer()
        self._pending: dict[str, _Approval] = {}
        # When an ask last left the queue unanswered (withdrawn or expired):
        # after that, a bare reply cannot be tied to the ask the owner read.
        self._ask_churn_at = float("-inf")
        self._session_id: str | None = None
        self._api_key_source: str | None = None
        self._rate_limited_until: int | None = None
        # The CLI's last reported state: idle | running | requires_action.
        self._state = _IDLE
        self._idle = asyncio.Event()
        self._idle.set()

    # -- lifecycle ---------------------------------------------------------

    def options(self, resume: str | None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cli_path=self._cli_path,
            # None would pass `--system-prompt ""` and wipe Claude Code's prompt.
            system_prompt={"type": "preset", "preset": "claude_code"},
            setting_sources=None,  # every source, exactly as a terminal loads
            cwd=str(self._cwd),
            permission_mode="default",
            can_use_tool=self._permission,
            include_partial_messages=True,
            resume=resume,
            # Surfaces the CLI's running / requires_action / idle frames, which
            # the SDK otherwise reads and drops (measured on 2.1.299).
            env={"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"},
            stderr=self._stderr,
        )

    async def start(self) -> None:
        if self._host_lock is not None:
            raise RuntimeError("this live host is already started")
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._state_path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            lock.close()
            if isinstance(exc, BlockingIOError):
                raise RuntimeError("the session state already has a live host") from exc
            raise
        self._host_lock = lock
        try:
            saved = self._load_session_id()
            try:
                await self._connect(saved)
            except (CLIConnectionError, ProcessError) as exc:
                if not self._missing_resume(exc):
                    raise
                self._report_fresh_session()
                await self._connect(None)
        except BaseException:
            await self.close()
            raise

    async def _spawn(self, resume: str | None) -> Any:
        _validate_environment()
        self._resume_attempt = resume
        self._stale_resume_diagnostic = False
        client = self._client_factory(self.options(resume))
        try:
            await client.connect()
        except BaseException:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        return client

    async def _connect(self, resume: str | None) -> None:
        self._client = await self._spawn(resume)
        self._stream_open = True
        self._tasks = [
            tracked_task(self._read(), name="live_agent.read", logger=logger),
            tracked_task(self._flush(), name="live_agent.flush", logger=logger),
        ]

    async def close(self) -> None:
        self._stream_open = False  # before anything wakes a waiting steer
        self._deny_pending()
        for task in self._tasks:
            task.cancel()
        try:
            for task in self._tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        finally:
            self._stream_open = False
            self._set_state(_IDLE)
            try:
                if self._client is not None:
                    await self._client.disconnect()
            finally:
                self._client = None
                if self._host_lock is not None:
                    self._host_lock.close()
                    self._host_lock = None

    # -- driving -----------------------------------------------------------

    async def turn(
        self, text: str, initiator: str = "owner_cli", *, cancel_pending: bool = False
    ) -> TaskHandle:
        """Send a turn; returns at once. Busy: steer the running work instead.

        The outcome arrives as ``finished`` events, not on the handle. While a
        permission ask is waiting the turn is HELD (``ApprovalPending``): the
        CLI withdraws a waiting ask when any message arrives, which denies the
        action (measured), so a reply meant as "yes" would deny it. Only an
        explicit ``cancel_pending=True`` sends anyway. One window remains that no
        host can close: an ask the CLI has written but the SDK has not yet
        delivered is invisible here, so a turn sent then still withdraws it
        (denied, and reported as withdrawn).
        """
        if initiator not in ACCEPTED_INITIATORS:
            raise PermissionError(
                f"initiator {initiator!r} is refused: only {sorted(ACCEPTED_INITIATORS)} "
                "may drive the live agent until the approval gate is wired"
            )
        client = self._require_client()
        if cancel_pending:
            self._deny_pending()
        if self._pending and not cancel_pending:
            waiting = ", ".join(sorted(self._pending))
            raise ApprovalPending(
                f"approval {waiting} is waiting; answer it first, or steer explicitly "
                "to cancel it (that denies the action)"
            )
        busy = self._state != _IDLE
        handle = TaskHandle(uuid.uuid4().hex[:8], busy)
        body = f"{OWNER_CLI_MARKER}\n\n{text}"
        if busy and self._steer == "interrupt":
            await self.interrupt()
            try:
                await asyncio.wait_for(self._idle.wait(), STEER_INTERRUPT_WAIT_S)
            except TimeoutError:
                raise CLIConnectionError(
                    "the interrupt never settled; the steer was not sent"
                ) from None
            if cancel_pending:
                self._deny_pending()
            if self._pending and not cancel_pending:
                waiting = ", ".join(sorted(self._pending))
                raise ApprovalPending(
                    f"approval {waiting} arrived while the steer waited; not sent"
                )
            if not self._stream_open or self._client is not client:
                raise CLIConnectionError("the session ended while waiting; the steer was not sent")
            body = f"{OWNER_CLI_MARKER}\n\nI interrupted you to steer the request above: {text}"
            busy = False
        previous = self._state
        # Busy from the moment the turn is sent; the CLI's own "running" frame
        # follows. Restored if the send fails. An "idle" frame from the turn
        # before can still land after this send; then the next turn goes out
        # as a plain turn the CLI queues, not a steer, until "running" arrives.
        self._set_state("running")
        try:
            if not busy:
                await client.query(body)
            else:
                await client.query(_one(_user_message(body, priority="now")))
        except BaseException:
            self._set_state(previous)
            raise
        return handle

    async def interrupt(self) -> None:
        client = self._require_client()
        self._deny_pending()
        await client.interrupt()

    def _deny_pending(self) -> None:
        """Retire asks before yielding, so a late yes cannot approve old work."""
        for approval in self._pending.values():
            if not approval.future.done():
                approval.future.set_result(False)
                self._ask_churn_at = time.monotonic()
                self._emit("approval", f"[{approval.id}] cancelled; denied", {"id": approval.id})
        self._pending.clear()

    def answer_reply(self, allow: bool, *, last_input_at: float) -> tuple[bool, str]:
        """Apply a bare yes / no only when the owner can only have meant one ask.

        ``last_input_at`` is the ``time.monotonic()`` of the owner's previous
        input. Refused (with what to do instead) when several asks wait, when
        an ask left the queue unanswered since that input (the screen changed
        under the owner), or when the waiting ask appeared under
        ``MIN_ASK_VISIBLE_S`` ago. Returns ``(answered, message)``. Known
        limit: the anchor is when the host READ the previous line, so a reply
        typed while the host was blocked (an interrupt-mode wait) can still be
        read against an ask that appeared during that block.
        """
        waiting = sorted(self._pending.values(), key=lambda a: a.shown_at)
        if len(waiting) != 1:
            ids = ", ".join(a.id for a in waiting) or "none"
            return False, f"approvals waiting: {ids}; answer one with /y ID or /n ID"
        approval = waiting[0]
        confirm = f"confirm with /y {approval.id} or /n {approval.id}"
        if self._ask_churn_at > last_input_at:
            return (
                False,
                f"the waiting approval changed (now {approval.id}: {approval.tool_name}); {confirm}",
            )
        if time.monotonic() - approval.shown_at < MIN_ASK_VISIBLE_S:
            return False, f"approval {approval.id} just appeared; {confirm}"
        self.answer(approval.id, allow)
        return True, f"{'approved' if allow else 'denied'} {approval.id}"

    def answer(self, approval_id: str, allow: bool) -> bool:
        approval = self._pending.get(approval_id)
        if approval is None or approval.future.done():
            return False
        approval.future.set_result(allow)
        return True

    def status(self) -> dict[str, Any]:
        return {
            "connected": self._stream_open and not self._reconnecting,
            "session_id": self._session_id,
            "state": self._state,
            "busy": self._state != _IDLE,
            "api_key_source": self._api_key_source,
            "rate_limited_until": self._rate_limited_until,
            "pending_approvals": [
                {"id": a.id, "tool": a.tool_name, "reason": a.reason}
                for a in self._pending.values()
            ],
            "cli_path": self._cli_path,
            "steer": self._steer,
        }

    async def events(self) -> AsyncIterator[Event]:
        while True:
            yield await self._events.get()

    # -- internals ---------------------------------------------------------

    async def _permission(
        self, tool_name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if not self._stream_open or self._reconnecting:
            return PermissionResultDeny(message="The live session is disconnected.")
        if tool_name in _UNANSWERABLE_TOOLS:
            self._emit("progress", f"{tool_name} declined: this host cannot answer it")
            return PermissionResultDeny(
                message="This session cannot show that tool. Ask your question in plain text."
            )
        loop = asyncio.get_running_loop()
        reason = ctx.decision_reason or ctx.title or f"{tool_name} needs approval"
        approval = _Approval(
            uuid.uuid4().hex[:6], tool_name, tool_input, reason, loop.create_future()
        )
        self._pending[approval.id] = approval
        # The owner must see exactly what would run: the whole input, unclipped
        # (a model-written "description" is a claim about the action, not it).
        source = f" (sub-agent {ctx.agent_id})" if ctx.agent_id else ""
        shown = json.dumps(tool_input, default=str, ensure_ascii=False)
        self._emit(
            "approval",
            f"[{approval.id}] {tool_name}{source}: {shown} — {reason}",
            {"id": approval.id, "input": tool_input},
        )
        try:
            allowed = await asyncio.wait_for(approval.future, self._approval_timeout_s)
        except TimeoutError:
            allowed = False
            self._ask_churn_at = time.monotonic()
            self._emit(
                "approval", f"[{approval.id}] expired unanswered; denied", {"id": approval.id}
            )
        except asyncio.CancelledError:
            # The CLI withdrew the ask (a steer arrived, or the turn ended).
            self._ask_churn_at = time.monotonic()
            self._emit("approval", f"[{approval.id}] withdrawn by the session", {"id": approval.id})
            raise
        finally:
            self._pending.pop(approval.id, None)
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message="The owner did not approve this action.")

    async def _read(self) -> None:
        try:
            while self._stream_open:
                try:
                    await self._consume_messages()
                except (CLIConnectionError, ProcessError) as exc:
                    if not self._missing_resume(exc):
                        raise
                    self._reconnecting = True
                    self._deny_pending()
                    self._set_state(_IDLE)
                    await self._client.disconnect()
                    self._report_fresh_session()
                    self._client = await self._spawn(None)
                    self._reconnecting = False
                    continue
                break
        except Exception as exc:  # the CLI process died or the stream broke
            logger.exception("live agent: session stream ended")
            self._emit("error", f"session stream ended: {exc}; restart the host to resume")
        finally:
            unexpected = self._stream_open
            self._stream_open = False
            self._reconnecting = False
            self._deny_pending()
            self._set_state(_IDLE)  # wakes an interrupt-mode steer, which then refuses
            if unexpected:
                self._emit("disconnected", "session disconnected; restart the host to resume")

    async def _consume_messages(self) -> None:
        """Keep one malformed message from disabling controls on a live session."""
        async for msg in self._client.receive_messages():
            try:
                for line in self._summary.feed(msg):
                    self._emit("progress", line)
                await self._handle(msg)
            except Exception:
                logger.exception("live agent: could not handle a %s", type(msg).__name__)
                self._emit("error", f"could not handle a {type(msg).__name__}; continuing")

    def _stderr(self, line: str) -> None:
        # Only keep the diagnostic bit; stderr can contain private tool output.
        if _STALE_RESUME in line:
            self._stale_resume_diagnostic = True

    def _missing_resume(self, exc: Exception) -> bool:
        detail = f"{exc} {getattr(exc, 'stderr', '') or ''}"
        return self._resume_attempt is not None and (
            self._stale_resume_diagnostic or _STALE_RESUME in detail
        )

    def _report_fresh_session(self) -> None:
        self._emit(
            "session", f"could not resume {self._resume_attempt}; starting a fresh session; "
            "resend any request sent before this notice",
        )

    async def _handle(self, msg: Any) -> None:
        if isinstance(msg, SystemMessage) and msg.subtype == "session_state_changed":
            state = msg.data.get("state")
            if isinstance(state, str) and state:
                self._set_state(state)
        elif isinstance(msg, SystemMessage) and msg.subtype == "init":
            self._resume_attempt = None  # a live session can no longer be a missing resume
            # The CLI sends an init frame before EVERY turn; report it once.
            first = self._api_key_source is None
            self._api_key_source = msg.data.get("apiKeySource")
            if self._remember_session(msg.data.get("session_id")) or first:
                self._emit("session", f"session {self._session_id}", msg.data)
        elif isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            if info.status == "rejected":
                self._rate_limited_until = info.resets_at
                self._emit("rate_limited", f"rate-limited until {info.resets_at}", info.raw)
        elif isinstance(msg, AssistantMessage) and msg.error == "rate_limit":
            self._emit("rate_limited", "rate-limited (assistant error)")
        elif isinstance(msg, ResultMessage):
            self._remember_session(msg.session_id)
            if msg.api_error_status == 429:
                self._emit("rate_limited", "rate-limited (HTTP 429); the turn was not retried")
            self._emit(
                "finished",
                ProgressSummarizer.final_line(msg),
                {
                    "cost_usd": msg.total_cost_usd,
                    "duration_ms": msg.duration_ms,
                    "terminal_reason": msg.terminal_reason,
                    "origin": msg.origin,
                    "result": msg.result,
                },
            )
            await self._record_cost(msg)

    def _set_state(self, state: str) -> None:
        self._state = state
        if state == _IDLE:
            self._idle.set()
        else:
            self._idle.clear()

    async def _flush(self) -> None:
        while self._stream_open:
            await asyncio.sleep(_FLUSH_TICK_S)
            line = self._summary.flush()
            if line:
                self._emit("progress", line)

    async def _record_cost(self, msg: ResultMessage) -> None:
        if self._cost_recorder is None or not msg.session_id or msg.total_cost_usd is None:
            return
        try:
            await self._cost_recorder(msg.session_id, msg.total_cost_usd, msg.usage or {})
        except Exception:
            logger.warning("live agent: cost recording failed", exc_info=True)

    def _remember_session(self, session_id: str | None) -> bool:
        """Persist a new session id; True when it changed."""
        if not session_id or session_id == self._session_id:
            return False
        try:
            atomic_write_text(self._state_path, json.dumps({"session_id": session_id}))
        except OSError:
            logger.exception("live agent: could not persist session id")
            self._emit("error", "could not persist session id; continuing and will retry")
            return False
        self._session_id = session_id
        return True

    def _load_session_id(self) -> str | None:
        try:
            value = json.loads(self._state_path.read_text()).get("session_id")
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, str) and value else None

    def _require_client(self) -> Any:
        if self._client is None or not self._stream_open or self._reconnecting:
            raise CLIConnectionError("live agent session is not running; restart the host")
        return self._client

    def _emit(self, kind: str, text: str, data: dict[str, Any] | None = None) -> None:
        # Payloads pass as one dict: a CLI key can never collide with a parameter.
        self._events.put_nowait(Event(kind, text, dict(data or {})))


def _user_message(body: str, *, priority: str) -> dict[str, Any]:
    # `priority: "now"` asks the CLI to take the message at the next step
    # rather than queue it behind the running turn; `origin` marks it human.
    return {
        "type": "user",
        "message": {"role": "user", "content": body},
        "parent_tool_use_id": None,
        "priority": priority,
        "origin": {"kind": "human"},
    }


async def _one(message: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    yield message
