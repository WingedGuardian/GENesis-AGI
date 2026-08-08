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
    # Byte offset of the START of the NEXT line (i.e. one-past this message's
    # line).  Populated only by ``read_transcript_delta`` (byte-aware); left
    # None by the text-mode ``read_transcript_messages``.
    end_byte: int | None = None


@dataclass
class TranscriptDelta:
    """Result of an incremental (byte-offset-resumed) transcript read.

    INVARIANT: ``new_byte_offset`` is the byte position of the START of line
    ``new_line_count``.  Passing them back as ``(start_line, start_byte)`` on a
    later call resumes exactly where this read stopped, with ABSOLUTE line
    numbers preserved.
    """

    messages: list[ConversationMessage]
    new_byte_offset: int
    new_line_count: int
    unchanged: bool = False       # stat-gate hit: file size == start_byte, nothing read
    truncated_reset: bool = False  # stored offset was past EOF → re-read from 0
    failed: bool = False          # stat()/read() raised — caller MUST NOT advance watermarks


def _parse_transcript_line(
    raw_line: str, line_num: int
) -> ConversationMessage | None:
    """Parse one JSONL entry into a ConversationMessage, or None.

    Returns None for tool_result/thinking/metadata entries and for
    user/assistant entries with no textual content.  Extracted from
    ``read_transcript_messages`` so both the text-mode and byte-mode readers
    share one parse contract.
    """
    try:
        obj = json.loads(raw_line)
    except json.JSONDecodeError:
        return None

    entry_type = obj.get("type")
    if entry_type not in ("user", "assistant"):
        return None

    msg = obj.get("message", {})
    content = msg.get("content", "")
    timestamp = obj.get("timestamp")

    if isinstance(content, str) and content.strip():
        # User text message
        return ConversationMessage(
            role="user",
            text=content.strip(),
            line_number=line_num,
            timestamp=timestamp,
        )

    if isinstance(content, list):
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
                tool_names.append(block.get("name", "unknown_tool"))
            # Skip: tool_result, thinking, other block types
        if text_parts:
            return ConversationMessage(
                role=entry_type,
                text="\n\n".join(text_parts),
                line_number=line_num,
                timestamp=timestamp,
                tool_names=tool_names,
            )

    return None


def read_transcript_delta(
    path: Path,
    *,
    start_line: int = 0,
    start_byte: int | None = None,
    max_lines: int | None = None,
) -> TranscriptDelta:
    """Incrementally read a CC JSONL transcript, resuming from a byte offset.

    When ``start_byte`` is not None it is treated as the byte position of the
    start of line ``start_line`` (the INVARIANT above): the reader ``seek()``s
    there and reads only the delta.  When ``start_byte`` is None the reader
    falls back to a full scan from byte 0 (legacy behaviour), emitting only
    messages at ``line_number >= start_line`` but still tracking true byte
    offsets so the caller can persist a resume point.

    Safety:
      * file size == ``start_byte``  → ``unchanged=True`` (stat-gate, no open).
      * file size <  ``start_byte``  → ``truncated_reset=True``, re-read from 0.
      * a partial trailing line (no newline yet — active append) is NOT
        consumed; ``new_byte_offset`` stays at its start so it is read whole
        on a later call.
      * a ``stat()``/read ``OSError`` → ``failed=True`` (empty result); the
        caller MUST NOT advance either watermark — persisting the coerced
        ``byte=0`` against a nonzero line watermark would force a whole-file
        re-read next cycle.

    Opens in BINARY mode: ``stat().st_size`` is directly comparable to the byte
    offset, and splitting on ``b"\\n"`` (0x0A) never tears a multibyte UTF-8
    sequence.
    """
    try:
        size = path.stat().st_size
    except OSError:
        logger.warning("Could not stat transcript: %s", path, exc_info=True)
        # I/O failure — signal it so the caller preserves BOTH watermarks. A
        # non-failed empty result would let the empty branch persist byte=0
        # against a nonzero line watermark (invariant violation → whole-file
        # re-read next cycle).
        return TranscriptDelta(
            messages=[],
            new_byte_offset=start_byte or 0,
            new_line_count=start_line,
            failed=True,
        )

    truncated_reset = False
    if start_byte is not None:
        if size == start_byte:
            # Nothing appended since last read — the cheap stat-gate.
            return TranscriptDelta(
                messages=[],
                new_byte_offset=start_byte,
                new_line_count=start_line,
                unchanged=True,
            )
        if size < start_byte:
            # Transcript truncated/rotated — the stored offset is invalid.
            truncated_reset = True
            start_byte = None

    seeking = start_byte is not None
    seek_pos = start_byte if seeking else 0
    # When seeking, the line AT seek_pos IS line `start_line`.  When scanning
    # from 0 (legacy / reset), line numbers count from 0 and we emit only
    # those >= start_line (0 after a reset).
    emit_from = 0 if truncated_reset else start_line
    line_no = start_line if seeking else 0

    messages: list[ConversationMessage] = []
    byte_pos = seek_pos
    read_failed = False
    try:
        with open(path, "rb") as f:
            f.seek(seek_pos)
            while True:
                raw = f.readline()
                if not raw:
                    break  # EOF
                if not raw.endswith(b"\n"):
                    # Partial trailing line (write in progress) — do not
                    # consume; byte_pos stays at its start.
                    break
                if max_lines is not None and (line_no - start_line) >= max_lines:
                    break
                current_line = line_no
                byte_pos += len(raw)
                line_no += 1
                if current_line < emit_from:
                    continue
                text_line = raw.decode("utf-8", errors="replace")
                msg = _parse_transcript_line(text_line, current_line)
                if msg is not None:
                    msg.end_byte = byte_pos
                    messages.append(msg)
    except OSError:
        logger.warning("Could not read transcript: %s", path, exc_info=True)
        read_failed = True

    return TranscriptDelta(
        messages=messages,
        new_byte_offset=byte_pos,
        new_line_count=line_no,
        truncated_reset=truncated_reset,
        failed=read_failed,
    )


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

    Each chunk contains up to ``chunk_size`` messages. Messages are split
    into consecutive chunks of up to ``chunk_size`` while preserving their
    original order.
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
