"""Shorten display text without cutting a word in half.

For text shown to a reader (an injected context block, a prompt section, a
dashboard line), where a raw ``text[:n]`` slice leaves fragments such as
"nothing leaves Gen". Not for byte-bounded transports: lengths here are
counted in characters.
"""

from __future__ import annotations

import re

ELLIPSIS = "…"

#: Stripped from the end of a cut so the marker follows a word, not a comma or
#: a dangling dash. Closing brackets and quotes are kept: they close something.
#: The marker itself is in the set, so re-clipping already-clipped text (a
#: joined summary of clipped parts) never shows "……".
_TRAILING = ".,;:!?-–—/\\([{" + ELLIPSIS

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def clip_words(text: str, n: int) -> str:
    """``text`` shortened to at most ``n`` characters, ending on a whole word.

    Unchanged when it already fits. Otherwise it is cut at the last whitespace
    that leaves room for the marker, trailing punctuation and whitespace are
    stripped, and ``…`` is appended, so a cut is always visible and the result
    (marker included) never exceeds ``n``. A first word longer than the room
    is the one case cut mid-word, still with the marker.
    """
    if len(text) <= n:
        return text
    if n <= 0:
        return ""
    room = n - len(ELLIPSIS)
    head = text[:room]
    if not text[room].isspace():
        # The cut falls inside a word: back up to the last boundary.
        for i in range(len(head) - 1, -1, -1):
            if head[i].isspace():
                head = head[:i]
                break
    kept = head.rstrip().rstrip(_TRAILING).rstrip()
    if not kept:
        # Nothing but one long word (or separators) fits: hard cut, from the
        # first non-whitespace character so a leading newline cannot break a
        # markdown bullet.
        kept = text.lstrip()[:room] or text[:room]
    return kept + ELLIPSIS


def first_sentence(text: str) -> str:
    """The first sentence of ``text``, with its terminator.

    A sentence ends at ``.``, ``!`` or ``?`` followed by whitespace, so a dot
    inside a token (``v3.0``, ``file.py``) does not end it. Text with no such
    end is returned whole (stripped).
    """
    return _SENTENCE_END.split(text.strip(), maxsplit=1)[0]
