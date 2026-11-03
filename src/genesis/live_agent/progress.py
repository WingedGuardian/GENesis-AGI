"""Turn raw Claude Code session messages into short progress lines.

The consumer is a voice model that is told what the agent is doing while it
works (Codex streams the same thing back into its realtime voice). Each line
is bounded at ``MAX_LINE_BYTES``. The bound comes from the candidate voice
engine: OpenAI's GPT-Live session reference (read 2026-10-22) caps one context
append at 500 tokens, and 500 bytes cannot exceed 500 tokens.
The full text is not lost by the bound: it stays in the session transcript;
a clipped line says how much it left out.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

MAX_LINE_BYTES = 500

# Input keys that name what a tool call is doing, most descriptive first.
_DETAIL_KEYS = ("description", "command", "file_path", "pattern", "query", "url", "prompt")


def clip(text: str, limit: int = MAX_LINE_BYTES) -> str:
    """Return ``text`` within ``limit`` UTF-8 bytes, marking any omission."""
    raw = text.encode()
    if len(raw) <= limit:
        return text
    marker = f" … [+{len(raw)} bytes total]"
    head = raw[: limit - len(marker.encode())].decode(errors="ignore")
    return head + marker


def _tool_detail(tool_input: dict[str, Any]) -> str:
    for key in _DETAIL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


class ProgressSummarizer:
    """Feed it every session message; it returns the progress lines to send.

    Discrete events (a tool started, a tool failed, the turn ended) come back
    from ``feed`` at once. Assistant text is coalesced and released by
    ``flush``: at a sentence end once ``min_interval_s`` has passed, or
    whatever has accumulated once ``max_interval_s`` has passed.
    """

    def __init__(
        self,
        *,
        use_deltas: bool = True,
        min_interval_s: float = 0.2,
        max_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._use_deltas = use_deltas
        self._min = min_interval_s
        self._max = max_interval_s
        self._clock = clock
        self._text = ""
        self._last_flush = clock()
        self._tool_names: dict[str, str] = {}

    def feed(self, msg: Any) -> list[str]:
        lines: list[str] = []
        if isinstance(msg, StreamEvent):
            if self._use_deltas and msg.parent_tool_use_id is None:
                delta = msg.event.get("delta") or {}
                if (
                    msg.event.get("type") == "content_block_delta"
                    and delta.get("type") == "text_delta"
                ):
                    self._text += delta.get("text", "")
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    self._tool_names[block.id] = block.name
                    detail = _tool_detail(block.input)
                    lines.append(clip(f"tool: {block.name}" + (f" — {detail}" if detail else "")))
                elif (
                    isinstance(block, TextBlock)
                    and not self._use_deltas
                    and msg.parent_tool_use_id is None
                ):
                    self._text += block.text
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    name = self._tool_names.pop(block.tool_use_id, "tool")
                    if block.is_error:
                        lines.append(clip(f"tool failed: {name}"))
        elif isinstance(msg, ResultMessage):
            self._tool_names.clear()
            # Only the text still pending; the host emits ``final_line`` once,
            # as its "finished" event.
            pending = self.flush(force=True)
            if pending:
                lines.append(pending)
        return lines

    def flush(self, *, force: bool = False) -> str | None:
        text = self._text.strip()
        if not text:
            return None
        elapsed = self._clock() - self._last_flush
        at_sentence_end = text[-1] in ".!?:\n"
        if not (force or elapsed >= self._max or (elapsed >= self._min and at_sentence_end)):
            return None
        keep = ""
        if not (force or at_sentence_end):
            # Released by the ceiling mid-sentence: stop at the last word
            # boundary so a word is never split across two lines (a voice
            # would speak the halves as two words).
            cut = max(self._text.rfind(" "), self._text.rfind("\n"))
            if cut <= 0:
                return None
            text, keep = self._text[:cut].strip(), self._text[cut:]
        self._text = keep
        self._last_flush = self._clock()
        return clip(" ".join(text.split()))

    @staticmethod
    def final_line(result: ResultMessage) -> str:
        if result.terminal_reason in ("aborted_streaming", "aborted_tools"):
            return "interrupted"
        if result.is_error:
            status = f" (HTTP {result.api_error_status})" if result.api_error_status else ""
            return clip(f"error: {result.subtype}{status}")
        summary = " ".join((result.result or "").split())
        return clip(f"finished: {summary}" if summary else "finished")
