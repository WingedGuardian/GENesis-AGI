"""Parsing for the OpenAI chat-completions request shape.

Shared by every Genesis surface that accepts that shape, so the parsing lives
in ONE place. Two endpoints previously carried their own copy and drifted: one
handled multimodal content arrays, the other stringified them into a Python
repr and forwarded that as the user's question.
"""

from __future__ import annotations


def extract_last_user_message(messages: object) -> str | None:
    """Text of the most recent non-empty user message, or None.

    Handles both plain string content and OpenAI multimodal content arrays
    (``[{"type": "text", "text": "..."}]``), which off-the-shelf clients send.
    Walks BACKWARD past malformed or empty entries rather than stopping at the
    last user entry, so a trailing empty message does not mask a real question
    in front of it.
    """
    if not isinstance(messages, list):
        return None
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            # JOIN every text block, in order. Returning at the first one
            # silently drops the rest, and in the common [text, image, text]
            # shape the trailing block is the actual instruction after an
            # attachment -- so the model would answer a truncated question
            # with no indication anything was missing.
            blocks = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block.get("text", "").strip()
            ]
            if blocks:
                return "\n\n".join(blocks)
    return None
