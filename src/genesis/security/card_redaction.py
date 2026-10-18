"""Redact payment card numbers from tool results and log records.

A page or a document can show a card number (a saved-card page, a receipt, a
test form). Code that hands such text to the model or writes it to a log runs
it through this module. Detection is ``card_shapes``: what that detector does
not recognise (other brands and lengths, numbers split by other characters,
digits spelled as escapes inside exception text) is not redacted, by design.

Three pieces:

* :func:`redact_value` walks a result (dicts, lists, tuples, strings, numbers,
  and JSON text whose digits only parsing reveals) and redacts every string,
  key and number.
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

from genesis.security.card_shapes import REDACTED, contains_card_number, redact

logger = logging.getLogger(__name__)


def redact_text(text: str) -> str:
    """``text`` with card numbers redacted, inside JSON text too.

    The plain pass catches a number written out. A JSON string can also spell
    the digits as ``\\u`` escapes, which only parsing reveals; when parsing
    finds a number the plain pass missed, the JSON is re-serialised redacted.
    """
    out = redact(text)
    if out.lstrip()[:1] not in ("{", "["):
        return out
    try:
        parsed = json.loads(out)
    except ValueError:
        return out
    walked = redact_value(parsed)
    return out if walked == parsed else json.dumps(walked, ensure_ascii=False)


def _redact_number(value: int | float) -> int | float | str:
    whole = (
        value
        if isinstance(value, int)
        else (
            int(value)
            if math.isfinite(value) and value.is_integer() and abs(value) < 1e20
            else None
        )
    )
    return REDACTED if whole is not None and contains_card_number(str(whole)) else value


def redact_value(value):
    """``value`` with card numbers redacted in every string, key and number.

    Covers dicts, lists, tuples, strings and numbers (a page script can return
    a card number as a number). Anything else is returned as it is.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return _redact_number(value)
    if isinstance(value, dict):
        return {
            (redact_value(k) if isinstance(k, str) else k): redact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(redact_value(v) for v in value)
    return value


def _redacted_result(name: str, result):
    try:
        return redact_value(result)
    except Exception:  # RecursionError on a deeply nested script result
        logger.warning("%s: the result could not be checked for card numbers", name)
        return {
            "error": f"{name}: the result could not be checked for card numbers, so it is withheld."
        }


def _failed(name: str, exc: Exception) -> dict:
    # The traceback goes to this module's logger, which carries CARD_FILTER.
    logger.error("%s raised %s", name, type(exc).__name__, exc_info=True)
    return {"error": f"{name} failed: {type(exc).__name__}: {redact(str(exc))}"}


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

    def filter(self, record: logging.LogRecord) -> bool:
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
