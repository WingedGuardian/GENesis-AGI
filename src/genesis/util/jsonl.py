"""Shared JSONL transcript parsing utilities.

Reads Claude Code session transcripts (JSONL files) and extracts
conversation content for memory extraction. Filters tool results
and thinking blocks to focus on actual conversation content.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ConversationMessage:
    """A single user or assistant message extracted from a JSONL transcript."""

    role: str  # "user" or "assistant"
    text: str
    line_number: int
    timestamp: str | None = None
    tool_names: list[str] = field(default_factory=list)


def read_transcript_messages(
    path: Path,
    *,
    start_line: int = 0,
    max_lines: int | None = None,
) -> list[ConversationMessage]:
    """Read conversation messages from a CC JSONL transcript.

    Extracts user messages and assistant text blocks.  Skips tool_result
    content, thinking blocks, and progress/metadata entries.  Keeps tool
    names as metadata but strips their payloads.

    Args:
        path: Path to the JSONL transcript file.
        start_line: Line number to start reading from (for watermark resume).
        max_lines: Maximum lines to read (None = read all remaining).

    Returns:
        List of ConversationMessage in order of appearance.
    """
    messages: list[ConversationMessage] = []

    try:
        with open(path, encoding="utf-8") as f:
            for line_num, raw_line in enumerate(f):
                if line_num < start_line:
                    continue
                if max_lines is not None and (line_num - start_line) >= max_lines:
                    break

                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                entry_type = obj.get("type")
                if entry_type not in ("user", "assistant"):
                    continue

                msg = obj.get("message", {})
                content = msg.get("content", "")
                timestamp = obj.get("timestamp")

                if isinstance(content, str) and content.strip():
                    # User text message
                    messages.append(ConversationMessage(
                        role="user",
                        text=content.strip(),
                        line_number=line_num,
                        timestamp=timestamp,
                    ))

                elif isinstance(content, list):
                    # Assistant response with typed blocks
                    text_parts: list[str] = []
                    tool_names: list[str] = []

                    for block in content:
                        if not isinstance(block, dict):
                            continue

                        block_type = block.get("type")

                        if block_type == "text":
                            text = block.get("text", "").strip()
                            if text:
                                text_parts.append(text)

                        elif block_type == "tool_use":
                            # Keep tool name, skip payload
                            name = block.get("name", "unknown_tool")
                            tool_names.append(name)

                        # Skip: tool_result, thinking, other block types

                    if text_parts:
                        messages.append(ConversationMessage(
                            role=entry_type,
                            text="\n\n".join(text_parts),
                            line_number=line_num,
                            timestamp=timestamp,
                            tool_names=tool_names,
                        ))

    except OSError:
        logger.warning("Could not read transcript: %s", path, exc_info=True)

    return messages


def chunk_messages(
    messages: list[ConversationMessage],
    chunk_size: int = 50,
) -> list[list[ConversationMessage]]:
    """Split messages into chunks for extraction.

    Each chunk contains up to ``chunk_size`` messages.  Chunks preserve
    message order and never split a user+assistant pair across chunks
    (best effort — if the last message in a chunk is a user message,
    the next assistant message starts the next chunk).
    """
    if not messages:
        return []

    chunks: list[list[ConversationMessage]] = []
    current: list[ConversationMessage] = []

    for msg in messages:
        current.append(msg)
        if len(current) >= chunk_size:
            chunks.append(current)
            current = []

    if current:
        chunks.append(current)

    return chunks


def format_chunk_for_extraction(messages: list[ConversationMessage]) -> str:
    """Format a chunk of messages as text suitable for LLM extraction.

    Produces a readable conversation format with role labels and timestamps.
    Tool names are included as metadata; tool output is excluded.
    """
    parts: list[str] = []
    for msg in messages:
        timestamp = f" [{msg.timestamp}]" if msg.timestamp else ""
        role_label = "USER" if msg.role == "user" else "GENESIS"
        tools_note = ""
        if msg.tool_names:
            tools_note = f" [Used tools: {', '.join(msg.tool_names)}]"

        # Truncate very long messages to keep extraction focused
        text = msg.text
        if len(text) > 2000:
            text = text[:1800] + "\n[... truncated ...]"

        parts.append(f"{role_label}{timestamp}{tools_note}:\n{text}")

    return "\n\n---\n\n".join(parts)
