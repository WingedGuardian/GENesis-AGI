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
whether or not the draft streamer was still running. ``upto`` advances per
Telegram chunk accepted, not per message.

One turn can run several attempts: a stale-resume retry or a failover peer starts
a new Claude Code run, which begins with its own ``init`` event, and contingency
answers the turn again without streaming. A new attempt keeps what was delivered
(it is on the user's screen: a retry that streams it again sends it no second
time) and drops what the failed attempt only held, its halt, and its text.
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


def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


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
        self._replay = ""  # what earlier attempts delivered
        self._any_delivered = False
        self._saw_text = False
        self._last_sent = float("-inf")
        self._last_event = clock()
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
        self._last_event = self._clock()
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
        delivered = self.delivered
        if len(delivered) > len(self._replay):
            self._replay = delivered
        self._seen = self._collected = ""
        self._upto = self._held_end = self._segment_start = 0
        self._held = []
        self._halted = False

    def _skip_replayed(self) -> None:
        """A retry that streams what an earlier attempt delivered: that text is
        delivered already, so it is not sent or held again."""
        if not self._replay or not self._held:
            return
        common = _common_prefix(self._seen, self._replay)
        # A span that runs past everything delivered (A+B after A) keeps only its
        # new part. Past a divergence mid-way it stays whole: that is new text.
        whole = common == len(self._replay)
        kept = []
        for s, e in self._held:
            if e <= common:
                continue
            if whole and s < common:
                rest = self._seen[common:e]
                s = common + len(rest) - len(rest.lstrip())
            if s < e:
                kept.append((s, e))
        if kept == self._held:
            return
        self._held = kept
        self._upto = kept[0][0] if kept else len(self._seen)

    def _ack(self, spans: list[tuple[int, int]], done: int) -> None:
        """``done`` characters of the joined held text are delivered."""
        pos = 0
        for i, (s, e) in enumerate(spans):
            if done < pos + e - s:
                cut = s + done - pos
                rest = self._seen[cut:e]
                start = cut + len(rest) - len(rest.lstrip())
                self._upto = cut
                self._held = ([(start, e)] if start < e else []) + spans[i + 1 :]
                return
            pos += e - s
            if i + 1 < len(spans) and done < pos + len(_JOIN):
                break
            pos += len(_JOIN)
        else:
            i = len(spans) - 1
        self._upto = spans[i][1]
        self._held = spans[i + 1 :]

    async def _flush(self) -> None:
        self._skip_replayed()  # sends nothing, so it holds through a halt
        if self._halted or self._interrupt.is_set():
            return
        text = self._held_text()
        now = self._clock()
        if len(text) < MIN_PROGRESS_CHARS or now - self._last_sent < MIN_PROGRESS_INTERVAL_S:
            return
        from genesis.cc.formatter import ResponseFormatter
        from genesis.channels.telegram.transport.streaming import TELEGRAM_MAX_LENGTH

        self._last_sent = now
        spans = list(self._held)
        done = 0
        # One chunk per send, each recorded as Telegram accepts it: a later chunk
        # that fails leaves only the unsent rest held, never a repeat.
        for chunk, size in ResponseFormatter().split_spans(text, TELEGRAM_MAX_LENGTH):
            try:
                await self._deliver(chunk)
            except Exception:
                # Held, not lost: the next boundary or the final reply carries it.
                log.warning("Progress message failed to send", exc_info=True)
                return
            done += size
            self._any_delivered = True
            self._ack(spans, done)
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

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_S)
            streamer = self._streamer
            if streamer is None or not streamer.enabled:
                continue
            if self._interrupt.is_set() or self._halted:
                continue
            quiet = self._clock() - self._last_event
            if quiet >= KEEPALIVE_S:
                await streamer.keepalive(quiet)
