"""Redact payment card numbers from tool results and log records.

A page or a document can show a card number (a saved-card page, a receipt, a
test form). Code that hands such text to the model or writes it to a log runs
it through this module. Detection is ``card_shapes``, plus a split pass for
digit groups separated by short runs of other text (see _split_card_spans).
What neither recognises (other brands and lengths, digits spelled as escapes
inside exception text, a card inside a long list of 4-digit numbers) is not
redacted, by design.

Three pieces:

* :func:`redact_value` walks a result (dicts, lists, tuples, strings, numbers,
  and JSON text whose digits only parsing reveals) and redacts every string,
  key and number; a container whose values together spell a card, or whose
  keys collide once redacted, is withheld whole.
* :func:`redacted` decorates a tool implementation, sync or async: its result
  is walked, and an exception becomes an ``{"error": ...}`` result with the
  message redacted.
* :class:`CardRedactingFilter` rewrites a log record as its redacted text,
  traceback included. :data:`CARD_FILTER` is the shared instance.

Stdlib and ``card_shapes`` only.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import math
import re
from collections.abc import Callable

from genesis.security.card_shapes import (
    _GROUPINGS,
    REDACTED,
    _tokens,
    card_brand,
    contains_card_number,
    redact,
)

logger = logging.getLogger(__name__)


def redact_text(text: str) -> str:
    """``text`` with card numbers redacted, inside JSON text too.

    The plain pass catches a number written out; the split pass
    (:func:`_split_card_spans`) one whose digit groups are separated by short
    runs of other text, such as one segmented card field per snapshot line.
    A JSON string can also spell
    the digits as ``\\u`` escapes, which only parsing reveals; when parsing
    finds a number the plain pass missed, the JSON is re-serialised redacted.
    Parsed: text starting with ``{``, ``[`` or ``"`` (a JSON string scalar).
    Bare numeric text is not parsed: its digits are exact, so the plain pass
    checks them (a rounded number already written out as text is not caught).
    """
    out = _redact_split(redact(text))
    if out.lstrip()[:1] not in ("{", "[", '"'):
        return out
    try:
        parsed = json.loads(out)
    except ValueError:
        return out
    walked = redact_value(parsed)
    return out if walked == parsed else json.dumps(walked, ensure_ascii=False)


# The split pass. A card can render as several digit groups with other text
# between them: one segmented input per aria-snapshot line, a group per table
# cell, digits typed into separate boxes. Two digit tokens are LINKED when the
# text between them is at most _SPLIT_GAP_MAX characters and holds no digit (a
# label such as "part 2" breaks the link). A card is then read in two shapes:
# * a window of linked tokens whose sizes are a standard printed grouping
#   (card_shapes._GROUPINGS: 4-4-4-4, 4-6-5, ...), unless a linked neighbour on
#   either side is another 4-digit number that is not a plausible MMYY expiry
#   (the detector's own rule against tables and lists of 4-digit numbers);
# * a MAXIMAL run of 12-19 linked single-digit tokens (one digit per box).
# Either way the digits must pass the brand, length and checksum rules. Tokens
# are the detector's (card_shapes._tokens): decimal fractions, UUID groups and
# "+" phone numbers never take part.
_SPLIT_GAP_MAX = 40
_ASCII_DIGIT = re.compile(r"[0-9]")


def _linked(text: str, left: tuple[int, int], right: tuple[int, int]) -> bool:
    gap = text[left[1] : right[0]]
    return 0 < len(gap) <= _SPLIT_GAP_MAX and not _ASCII_DIGIT.search(gap)


def _listed_neighbour(text: str, token: tuple[int, int]) -> bool:
    start, end = token
    return end - start == 4 and not 1 <= int(text[start : start + 2]) <= 12


def _split_card_spans(text: str) -> list[tuple[int, int]]:
    """Spans in ``text`` of card numbers whose digit groups are split by short
    runs of other text (see the comment above)."""
    tokens = _tokens(text)
    if len(tokens) < 2:
        return []
    links = [_linked(text, a, b) for a, b in zip(tokens, tokens[1:], strict=False)]
    sizes = [end - start for start, end in tokens]
    spans: list[tuple[int, int]] = []
    for i in range(len(tokens)):
        for grouping in _GROUPINGS:
            j = i + len(grouping)
            if j > len(tokens) or tuple(sizes[i:j]) != grouping or not all(links[i : j - 1]):
                continue
            if i > 0 and links[i - 1] and _listed_neighbour(text, tokens[i - 1]):
                continue
            if j < len(tokens) and links[j - 1] and _listed_neighbour(text, tokens[j]):
                continue
            digits = "".join(text[a:b] for a, b in tokens[i:j])
            if card_brand(digits):
                spans.append((tokens[i][0], tokens[j - 1][1]))
    i = 0
    while i < len(tokens):
        j = i
        while j < len(tokens) and sizes[j] == 1 and (j == i or links[j - 1]):
            j += 1
        if 12 <= j - i <= 19:
            digits = "".join(text[a:b] for a, b in tokens[i:j])
            if card_brand(digits):
                spans.append((tokens[i][0], tokens[j - 1][1]))
        i = max(j, i + 1)
    return sorted(spans)


def _redact_split(text: str) -> str:
    spans = _split_card_spans(text)
    if not spans:
        return text
    out, last = [], 0
    for start, end in spans:
        if start < last:  # overlapping reading of the same digits
            continue
        out.append(text[last:start])
        out.append(REDACTED)
        last = end
    out.append(text[last:])
    return "".join(out)


def contains_card_text(text: str) -> bool:
    """True when ``text`` holds a card number, written out or split by short gaps."""
    return contains_card_number(text) or bool(_split_card_spans(text))


# JavaScript's Number.MAX_SAFE_INTEGER. A page script that returns a 17-19
# digit card number as a Number gets it back ROUNDED above this, and a rounded
# value fails the checksum, so it cannot be told apart from any other number.
_MAX_SAFE_INTEGER = 2**53 - 1
# One past the largest 19-digit value: no card number is longer.
_CARD_MAGNITUDE_END = 10**19


def _redact_number(value: int | float) -> int | float | str:
    """A number holding a card number, or possibly a rounded one, as REDACTED.

    Beyond the safe-integer range and below 20 digits, every number is
    withheld without a checksum: it may be a rounded card. Accepted false
    positive: any other number in that range (a 64-bit id, a large count).
    """
    if isinstance(value, float) and not math.isfinite(value):
        return value
    if _MAX_SAFE_INTEGER < abs(value) < _CARD_MAGNITUDE_END:
        return REDACTED
    if isinstance(value, float):
        if not value.is_integer():
            return value
        value_digits = str(int(value))
    else:
        value_digits = str(value)
    return REDACTED if contains_card_number(value_digits) else value


def _scalar_text(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if math.isfinite(value) and value.is_integer() else repr(value)
    return None


def _flat_scalars(value, out: list[tuple[str, bool]], mode: str) -> list[tuple[str, bool]]:
    """The scalars under ``value`` in document order, as ``(text, is_string)``.

    ``mode`` picks what of a mapping is read: ``"values"``, ``"keys"`` (keys
    only, at every depth), or ``"both"`` (each key, then its value).
    """
    if isinstance(value, dict):
        for k, v in value.items():
            if mode != "values" and isinstance(k, str):
                out.append((k, True))
            if mode != "keys" or isinstance(v, dict | list | tuple):
                _flat_scalars(v, out, mode)
    elif isinstance(value, list | tuple):
        for v in value:
            if mode != "keys" or isinstance(v, dict | list | tuple):
                _flat_scalars(v, out, mode)
    else:
        text = _scalar_text(value)
        if text is not None:
            out.append((text, isinstance(value, str)))
    return out


def _digit_string(text: str) -> str | None:
    digits = text.replace(" ", "").replace("-", "")
    return digits if digits and digits.isascii() and digits.isdigit() else None


def _hides_split_card(parts: list[tuple[str, bool]]) -> bool:
    """Whether a card is split across adjacent scalars of one container.

    Two readings, both over every contiguous subrun, not one maximal run:
    * the scalars joined by newlines go through the split pass, which reads
      standard printed groupings (``["4242", "4242", "4242", "4242"]``, as
      strings or numbers);
    * every contiguous subrun of two or more digit-only STRINGS whose digits
      total 12-19 is joined with nothing and checked for brand, length and
      checksum (a card cut into chunks of any widths, ``["424242",
      "4242424242"]``). Each start extends only while the total stays within 19
      digits, so the work is O(n x 19). Strings only: a number cannot carry a
      chunk's leading zeros, and over numeric lists this reading withheld
      about one result in five (MEASURED on synthetic numeric script results).
    """
    if len(parts) < 2:
        return False
    if _split_card_spans("\n".join(text for text, _ in parts)):
        return True
    chunks = [_digit_string(text) if is_string else None for text, is_string in parts]
    for i in range(len(chunks)):
        total = ""
        for chunk in chunks[i:]:
            if chunk is None:
                break
            total += chunk
            if len(total) > 19:
                break
            if len(total) >= 12 and len(total) > len(chunk) and card_brand(total):
                return True
    return False


def _container_hides_card(walked) -> bool:
    return any(
        _hides_split_card(_flat_scalars(walked, [], mode)) for mode in ("values", "keys", "both")
    )


def redact_value(value):
    """``value`` with card numbers redacted in every string, key and number.

    Covers dicts, lists, tuples, strings and numbers (a page script can return
    a card number as a number). A container whose scalars together spell a
    card (:func:`_hides_split_card`) is withheld whole, as is a mapping whose
    keys collide once redacted (two card-number keys, or one and a literal
    ``[card redacted]`` key, would otherwise overwrite each other). Anything
    else is returned as it is.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return _redact_number(value)
    if isinstance(value, dict):
        keys = [redact_value(k) if isinstance(k, str) else k for k in value]
        if len(set(keys)) < len(keys):
            return REDACTED
        walked = dict(zip(keys, (redact_value(v) for v in value.values()), strict=True))
        return REDACTED if _container_hides_card(walked) else walked
    if isinstance(value, list | tuple):
        walked = type(value)(redact_value(v) for v in value)
        return REDACTED if _container_hides_card(walked) else walked
    return value


def _redacted_result(name: str, result):
    try:
        walked = redact_value(result)
    except Exception:  # RecursionError on a deeply nested script result
        logger.warning("%s: the result could not be checked for card numbers", name)
        return {
            "error": f"{name}: the result could not be checked for card numbers, so it is withheld."
        }
    if isinstance(result, dict) and not isinstance(walked, dict):
        # Keep the tool's dict shape when the whole result had to be withheld.
        return {
            "error": f"{name}: the result spelled a card number across its values, so it is withheld."
        }
    return walked


def _failed(name: str, exc: Exception) -> dict:
    # The traceback goes to this module's logger, which carries CARD_FILTER.
    logger.error("%s raised %s", name, type(exc).__name__, exc_info=True)
    return {"error": f"{name} failed: {type(exc).__name__}: {redact_text(str(exc))}"}


def redacted(fn):
    """Redact card numbers from a tool implementation's result and errors.

    The tool name in messages is the function name without an ``_impl_``
    prefix. An exception becomes an ``{"error": ...}`` result with its message
    redacted. Two kinds still propagate: BaseExceptions such as cancellation,
    and the built-in TimeoutError (re-raised with its message redacted), which
    a caller's tool timeout may handle. A result too deep to walk is withheld.
    """
    name = fn.__name__.removeprefix("_impl_")

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def run_async(*args, **kwargs):
            try:
                result = await fn(*args, **kwargs)
            except TimeoutError as e:
                raise TimeoutError(redact(str(e))) from None
            except Exception as e:
                return _failed(name, e)
            return _redacted_result(name, result)

        return run_async

    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except TimeoutError as e:
            raise TimeoutError(redact(str(e))) from None
        except Exception as e:
            return _failed(name, e)
        return _redacted_result(name, result)

    return run


class CardRedactingFilter(logging.Filter):
    """Rewrite a record as its redacted text, traceback included.

    The message, the formatted exception and the stack are joined into the
    message and redacted; ``exc_info``, ``exc_text`` and ``stack_info`` are
    cleared so no handler formats the raw traceback again.

    Works on a logger or a handler. A LOGGER filter sees only records created
    on that logger, not ones propagated from its children, but survives code
    that replaces the logger's handlers (FastMCP's configure_logging). A
    HANDLER filter sees every record that handler emits.
    """

    _formatter = logging.Formatter()

    #: Armed mode: while this returns True (a payment tab is armed, see
    #: ``genesis.mcp.health.payment_tab``) a record keeps only its format
    #: string. Its args are withheld and its traceback is never formatted, so
    #: nothing read from a page during a card fill reaches a log. A probe that
    #: raises counts as armed.
    armed: Callable[[], bool] | None = None

    def _armed(self) -> bool:
        if self.armed is None:
            return False
        try:
            return bool(self.armed())
        except Exception:
            return True

    def filter(self, record: logging.LogRecord) -> bool:
        if self._armed():
            withheld = bool(record.args or record.exc_info or record.exc_text or record.stack_info)
            record.msg = redact(str(record.msg)) + (
                " [args withheld: payment tab armed]" if withheld else ""
            )
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return True
        try:
            parts = [record.getMessage()]
        except Exception:
            parts = [str(record.msg)]
        if record.exc_info:
            parts.append(self._formatter.formatException(record.exc_info))
        elif record.exc_text:
            parts.append(record.exc_text)
        if record.stack_info:
            parts.append(record.stack_info)
        record.msg = redact("\n".join(parts))
        record.args = None
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


CARD_FILTER = CardRedactingFilter()
logger.addFilter(CARD_FILTER)  # _failed logs exception text with its traceback
