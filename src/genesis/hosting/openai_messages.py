"""Parsing for the OpenAI chat-completions request shape.

Home for the parse so that every Genesis surface accepting that shape reads it
the same way. This is not pre-emptive: `dashboard/routes/voice_api.py` already
carries two inline copies of the same extraction. They read only the newest
`role == "user"` entry, which is the correct semantics (see the function), but
they call `.strip()` on `content`, which raises on a multimodal list.
Migrating them is issue #2272 and deliberately not bundled with this change.

Three copies of one parse across two surfaces is the argument for the module:
the same request otherwise yields a different prompt depending on which door it
came through, and each copy has to be fixed separately when the shape moves.
"""

from __future__ import annotations

# Entries that carry instructions rather than conversation turns; a client may
# append them after the newest user turn without that turn ceasing to be newest.
_INSTRUCTION_ROLES = frozenset({"system", "developer"})


def extract_last_user_message(messages: object) -> str | None:
    """Text of the NEWEST user turn, or None when that turn has no usable text.

    Handles both plain string content and OpenAI multimodal content arrays
    (``[{"type": "text", "text": "..."}]``), which off-the-shelf clients send.

    Only the newest user turn is ever read. Clients of this shape resend the
    full history on every request while the caller's conversation loop keeps
    its own, so whatever this returns is submitted as a NEW turn. Falling back
    to an older user message when the newest is empty, image-only or malformed
    would re-submit an instruction that already ran, repeating its side
    effects; None lets the caller reject the request instead. For the same
    reason the request must END in a user turn: only system/developer entries
    (instructions, not turns) are skipped on the way back. A trailing
    assistant, tool or malformed entry means there is no new user turn, so the
    answer is None rather than whatever user message sits further back.
    """
    # list OR tuple: the inline version this replaces iterated whatever it was
    # given, so narrowing to `list` would be an unannounced regression for any
    # caller holding an immutable sequence. Still a guard, because a str is
    # iterable and would otherwise be walked character by character.
    if not isinstance(messages, (list, tuple)):
        return None
    newest = next(
        (
            m for m in reversed(messages)
            # isinstance first: a JSON list/object role is unhashable, and set
            # membership on it would raise instead of treating it as malformed.
            if not (
                isinstance(m, dict)
                and isinstance(m.get("role"), str)
                and m.get("role") in _INSTRUCTION_ROLES
            )
        ),
        None,
    )
    if isinstance(newest, dict) and newest.get("role") == "user":
        content = newest.get("content")
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
