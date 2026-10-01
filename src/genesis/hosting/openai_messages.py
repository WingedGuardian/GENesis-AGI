"""Parsing for the OpenAI chat-completions request shape.

Home for the parse so that every Genesis surface accepting that shape reads it
the same way. This is not pre-emptive: `dashboard/routes/voice_api.py` already
carries two inline copies of the same extraction, and both hold the defects
fixed here -- they stop at the first `role == "user"` entry however empty it
is, and they call `.strip()` on `content`, which raises on a multimodal list.
Migrating them is issue #2272 and deliberately not bundled with this change.

Three copies of one parse across two surfaces is the argument for the module:
the same request otherwise yields a different prompt depending on which door it
came through, and each copy has to be fixed separately when the shape moves.
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
    # list OR tuple: the inline version this replaces iterated whatever it was
    # given, so narrowing to `list` would be an unannounced regression for any
    # caller holding an immutable sequence. Still a guard, because a str is
    # iterable and would otherwise be walked character by character.
    if not isinstance(messages, (list, tuple)):
        return None
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, (list, tuple)):
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
