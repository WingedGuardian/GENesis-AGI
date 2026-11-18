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

``seen`` is every text event in order, and ``seen[:upto]`` is what has been
delivered, so a rescue or a fallback sends only ``seen[upto:]`` whether or not the
draft streamer was still running.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

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
# The events that already stop the draft (the rate-limit double-send, #4849 review):
# progress stops with it, and what it held goes to the final reply.
_HALT_EVENTS = frozenset({"rate_limit_event", "error"})


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
        self._upto = 0
        self._held_end = 0  # where the last tool call ended a segment, in _seen
        self._segment = ""
        self._held: list[str] = []
        self._last_sent = float("-inf")
        self._last_event = clock()
        self._halted = False
        self._started = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @property
    def delivered(self) -> str:
        return self._seen[: self._upto]

    @property
    def saw_text(self) -> bool:
        return bool(self._seen)

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

    async def on_event(self, event_type: str, text: str | None) -> None:
        """Feed every stream event; the first one starts the snapshot clock."""
        self._last_event = self._clock()
        self._started.set()
        if event_type == "text" and text:
            self._seen += text
            self._segment += text
        elif event_type == "tool_use":
            segment = self._segment.strip()
            self._segment = ""
            if segment:
                self._held.append(segment)
            self._held_end = len(self._seen)
            await self._flush()
        elif event_type in _HALT_EVENTS:
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
        as streamed rather than re-joined, and anchored at the start.
        """
        delivered = self.delivered
        if delivered and response.startswith(delivered):
            response = response[len(delivered) :]
        raw = self._seen[self._upto : self._held_end].strip()
        held = "\n\n".join(self._held)
        self._held = []
        response = response.lstrip("\n")
        if raw and not response.lstrip().startswith(raw):
            response = f"{held}\n\n{response}" if response else held
        return response

    async def _flush(self) -> None:
        if self._halted or self._interrupt.is_set():
            return
        text = "\n\n".join(self._held)
        now = self._clock()
        if len(text) < MIN_PROGRESS_CHARS or now - self._last_sent < MIN_PROGRESS_INTERVAL_S:
            return
        self._last_sent = now
        try:
            await self._deliver(text)
        except Exception:
            # Held, not lost: the next boundary or the final reply carries it.
            log.warning("Progress message failed to send", exc_info=True)
            return
        self._held = []
        self._upto = len(self._seen)  # a tool call just ended the segment

    async def _snapshot(self) -> None:
        await self._started.wait()
        await asyncio.sleep(SNAPSHOT_AFTER_S)
        streamer = self._streamer
        if self._interrupt.is_set() or self._upto or streamer is None:
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
            if self._interrupt.is_set():
                continue
            quiet = self._clock() - self._last_event
            if quiet >= KEEPALIVE_S:
                await streamer.keepalive(quiet)
