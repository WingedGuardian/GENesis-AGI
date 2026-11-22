"""Progress for one Telegram turn: what the model writes while it works reaches the
user as real messages, the way the CLI shows it (#4849).

The live draft cannot carry this alone. Telegram deletes a live draft about 30 s
after its last update, and whenever a normal message arrives ("AI features for
bots", live response streaming), so a long turn showed at most a model label.

Claude Code emits one content block per stream line (39,713 of 39,713 assistant
lines over 5 transcripts, 2026-11-17), so text the model writes before a tool call
arrives as ``text`` events followed by a ``tool_use`` event. Text is kept as
SEGMENTS, each ended by a tool call. At a tool call the ended segments go out as
one message, logged with sender ``genesis-progress``; a segment too short to send,
or inside the pacing interval, is HELD for the next boundary or the final reply.
The text after the last tool call is the reply itself, and is never held.

``seen`` is every text event of the current ATTEMPT in order, and ``seen[:upto]``
is what has been delivered, so a rescue or a fallback sends only ``seen[upto:]``
whether or not the draft streamer was still running. Held text goes out as one
unit, delivered only when Telegram accepts all of it (one message, or several
when it is longer than one), so ``upto`` only ever moves to a tool boundary.

One turn can run several attempts: a stale-resume retry or a failover peer starts
a new Claude Code run, which begins with its own ``init`` event, and contingency
answers the turn again without streaming. A new attempt starts a new ledger and
drops what the failed attempt only held, its halt, and its text. A retry that
streams text an earlier attempt delivered sends it again: repeats across
attempts are #4888's, at the streaming retry, not this ledger's.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from genesis.cc.provider_readiness import rate_limit_rejected
from genesis.util.tasks import tracked_task

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from genesis.channels.telegram.transport.streaming import DraftStreamer

log = logging.getLogger(__name__)

PROGRESS_SENDER = "genesis-progress"
# A one-line aside ("Checking the logs.") is not worth a notification; a sentence
# that says something is. Shorter text is held for the final reply.
MIN_PROGRESS_CHARS = 40
# Telegram allows about one message a second per chat; a tool-heavy turn can cross
# a tool boundary every few seconds, so progress is paced well under that.
MIN_PROGRESS_INTERVAL_S = 10.0
# Posted once if a turn has shown nothing real this long after Claude Code started
# (the old timer started at receipt and fired during cold start).
SNAPSHOT_AFTER_S = 60.0
# Under the ~30 s draft lifetime, so a quiet stretch keeps a visible draft.
KEEPALIVE_S = 15.0
_JOIN = "\n\n"


def halts(event_type: str, raw: dict | None) -> bool:
    """Whether a stream event stops the draft and progress for its attempt (the
    rate-limit double-send, #4849 review): an ``error``, or a ``rate_limit_event``
    that refuses use. Claude Code emits ``rate_limit_event`` on ordinary turns
    too (status ``allowed`` / ``allowed_warning``), and those change nothing.
    What progress held when it stops goes to the final reply."""
    if event_type == "error":
        return True
    return event_type == "rate_limit_event" and rate_limit_rejected(raw)


class TurnProgress:
    """One turn's interim delivery, snapshot and draft keepalive."""

    def __init__(
        self,
        deliver: Callable[[str], Awaitable[None]],
        streamer: DraftStreamer | None,
        interrupt: asyncio.Event,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._deliver = deliver
        self._streamer = streamer
        self._interrupt = interrupt
        self._clock = clock
        self._seen = ""
        self._collected = ""  # all non-synthetic text, subagent text included
        self._upto = 0
        self._held_end = 0  # where the last tool call ended a segment, in _seen
        self._segment_start = 0
        self._held: list[tuple[int, int]] = []  # stripped segment spans in _seen
        self._any_delivered = False
        self._saw_text = False
        self._last_sent = float("-inf")
        self._halted = False
        self._started = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @property
    def delivered(self) -> str:
        """What this attempt has delivered, as streamed."""
        return self._seen[: self._upto]

    @property
    def any_delivered(self) -> bool:
        """Whether any attempt of this turn delivered progress."""
        return self._any_delivered

    @property
    def saw_text(self) -> bool:
        return self._saw_text

    def start(self) -> None:
        self._tasks = [
            tracked_task(self._snapshot(), name="telegram-progress-snapshot"),
            tracked_task(self._keepalive(), name="telegram-progress-keepalive"),
        ]

    def finish(self) -> None:
        """No more interim sends: call before the final reply or any error reply."""
        for task in self._tasks:
            task.cancel()

    stop = finish

    async def on_event(
        self,
        event_type: str,
        text: str | None,
        *,
        synthetic: bool = False,
        raw: dict | None = None,
    ) -> None:
        """Feed every stream event; the first one starts the snapshot clock.

        ``synthetic`` text is the CLI's own notice (a usage limit), not the
        model's: it is neither progress nor part of the reply. A line with a
        ``parent_tool_use_id`` comes from a subagent the Agent tool started: it
        is activity, but its text and tool calls are not this turn's narration.
        """
        self._started.set()
        if event_type == "text" and text and not synthetic:
            # What the invoker collects, subagent text included, in stream order:
            # for comparing the empty-result fallback's reply only.
            self._collected += text
        if (raw or {}).get("parent_tool_use_id"):
            return
        if event_type == "init":
            # Every Claude Code run opens with one; on a fresh turn this is a no-op.
            self._new_attempt()
        elif event_type == "text" and text and not synthetic:
            self._seen += text
            self._saw_text = True
        elif event_type == "tool_use":
            segment = self._seen[self._segment_start :]
            start = self._segment_start + len(segment) - len(segment.lstrip())
            end = self._segment_start + len(segment.rstrip())
            if end > start:
                self._held.append((start, end))
            self._segment_start = self._held_end = len(self._seen)
            await self._flush()
        elif halts(event_type, raw):
            self._halted = True

    def pending(self) -> str:
        """Everything streamed and not yet delivered, for a rescue send."""
        return self._seen[self._upto :].lstrip("\n")

    def final_reply(self, response: str) -> str:
        """The final reply, without delivered text, with held text in front.

        The reply is normally the last text block, which follows the last tool
        call and so is never held: the held asides go in front of it. The
        empty-result fallback's reply (all streamed text joined) and a rescue
        (``pending()``) already START with the held span, raw, so it is compared
        as streamed rather than re-joined, and anchored at the start. A
        contingency reply answers the turn again, so it is a new attempt.
        """
        from genesis.cc.conversation import CONTINGENCY_HEADER_PREFIX

        if response.startswith(CONTINGENCY_HEADER_PREFIX):
            self._held = []
            return response
        response = self._as_streamed(response)
        delivered = self.delivered
        if delivered and response.startswith(delivered):
            response = response[len(delivered) :]
        raw = self._seen[self._upto : self._held_end].strip()
        held = self._held_text()
        self._held = []
        response = response.lstrip("\n")
        if raw and not response.lstrip().startswith(raw):
            response = f"{held}\n\n{response}" if response else held
        return response

    def _as_streamed(self, response: str) -> str:
        """The empty-result fallback's reply is all of this attempt's text,
        subagent text included, split for Telegram and re-joined with newlines,
        which adds characters at each chunk boundary. Recognise it and return this
        turn's own text as streamed: a subagent's narration is not the reply."""
        from genesis.cc.conversation import _BG_TRUNCATION_NOTICE
        from genesis.cc.formatter import ResponseFormatter
        from genesis.cc.types import ChannelType

        if not self._seen.strip() or response == self._seen:
            return response  # nothing of this turn's own to compare
        if response == self._collected:
            return self._seen
        formatter = ResponseFormatter()
        for tail in ("", _BG_TRUNCATION_NOTICE):
            collected = self._collected + tail
            if response == "\n".join(formatter.format(collected, channel=ChannelType.TELEGRAM)):
                return self._seen + tail
        return response

    def _held_text(self) -> str:
        return _JOIN.join(self._seen[s:e] for s, e in self._held)

    def _new_attempt(self) -> None:
        self._seen = self._collected = ""
        self._upto = self._held_end = self._segment_start = 0
        self._held = []
        self._halted = False

    async def _flush(self) -> None:
        if self._halted or self._interrupt.is_set():
            return
        from genesis.channels.telegram.transport.streaming import TELEGRAM_MAX_LENGTH

        text = self._held_text()
        now = self._clock()
        if len(text) < MIN_PROGRESS_CHARS or now - self._last_sent < MIN_PROGRESS_INTERVAL_S:
            return
        self._last_sent = now
        if len(text) <= TELEGRAM_MAX_LENGTH:
            parts = [text]
        else:
            from genesis.cc.formatter import ResponseFormatter

            parts = ResponseFormatter()._split_preserving_code(text, TELEGRAM_MAX_LENGTH)
        # The held text is one unit: delivered only when every part is accepted.
        # A part that fails leaves all of it held, so the final reply repeats the
        # parts already sent; that rare repeat is accepted (owner, 2026-11-22) over
        # tracking how much of a split text was delivered.
        for part in parts:
            if self._interrupt.is_set():
                return  # /stop: nothing more goes out, and what is held stays held
            try:
                await self._deliver(part)
            except Exception:
                log.warning("Progress message failed to send", exc_info=True)
                return
            finally:
                self._message_sent()
            self._any_delivered = True  # on screen, so no status snapshot after it
        self._held = []
        self._upto = len(self._seen)  # a tool call just ended the segment

    async def _snapshot(self) -> None:
        await self._started.wait()
        await asyncio.sleep(SNAPSHOT_AFTER_S)
        streamer = self._streamer
        if (
            self._interrupt.is_set()
            or self._halted
            or self._any_delivered
            or streamer is None
        ):
            return
        body = streamer.status_body()
        if body:
            try:
                await self._deliver(body)
            except Exception:
                log.debug("Status snapshot send failed", exc_info=True)
            finally:
                self._message_sent()

    def _message_sent(self) -> None:
        # A real message (even one whose send raised: it may have arrived) ends
        # the live draft, so the streamer's next update redraws it at once.
        if self._streamer is not None:
            self._streamer.message_sent()

    async def _keepalive(self) -> None:
        wait = KEEPALIVE_S
        while True:
            await asyncio.sleep(wait)
            wait = KEEPALIVE_S
            streamer = self._streamer
            if streamer is None or not streamer.enabled:
                continue
            if self._interrupt.is_set() or self._halted:
                continue
            # Quiet is measured on the draft itself, not on the stream: events the
            # draft does not show (a subagent's, a tool result, a throttled update)
            # leave it to expire all the same. Sleep to the refresh deadline rather
            # than a fixed tick, so a send just after a tick cannot stretch the gap
            # toward the draft's lifetime.
            quiet = streamer.quiet_s()
            if quiet >= KEEPALIVE_S:
                await streamer.keepalive(after_s=KEEPALIVE_S)
            elif quiet > 0:
                wait = KEEPALIVE_S - quiet
