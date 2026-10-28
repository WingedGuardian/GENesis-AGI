"""Content-free diagnostics for an explicitly private native invocation.

The context is task-local: concurrent ordinary invocations retain diagnostics.
Receipt state and timings remain available; raw CLI output/callback exceptions
belong to the private result, not the server log or tracing plane.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

_private = ContextVar("genesis_private_execution", default=False)
_ERROR_MARKER = "_genesis_trace_error_message"
PRIVATE_ERROR = "private_execution_failed"


def _mark_private_exception(exc: BaseException) -> None:
    try:
        attrs = BaseException.__dict__["__dict__"].__get__(exc)
        dict.__setitem__(attrs, _ERROR_MARKER, PRIVATE_ERROR)
    except BaseException:
        pass  # Diagnostics must never replace the caller's exception.


def exception_is_private(exc: BaseException | None) -> bool:
    """Inspect only exception provenance, never exception text or user hooks."""
    if private_execution_active():
        return True
    pending, seen = [exc], set()
    try:
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            attrs = BaseException.__dict__["__dict__"].__get__(current)
            if dict.__contains__(attrs, _ERROR_MARKER):
                return True  # Presence, including malformed/falsey values.
            for edge in ("__cause__", "__context__"):
                pending.append(BaseException.__dict__[edge].__get__(current))
            if isinstance(current, BaseExceptionGroup):
                pending.extend(BaseExceptionGroup.__dict__["exceptions"].__get__(current))
    except BaseException:
        return True  # Metadata refusal is not permission to format content.
    return False


def private_execution_active() -> bool:
    return _private.get()


class _PrivateDiagnosticFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            error = record.exc_info[1] if record.exc_info else None
            return not exception_is_private(error)
        except BaseException:
            return False


_filter = _PrivateDiagnosticFilter()


@contextmanager
def private_execution():
    # Logger filters run before handlers, including test/log forwarding handlers.
    # Only these execution diagnostics are suppressed, not owner notifications.
    for name in (
        "genesis.cc.invoker",
        "genesis.cc.direct_session",
        "genesis.cc.session_manager",
        "genesis.observability.spans",
        "genesis.observability.span_writer",
        "genesis.util.proc_kill",
    ):
        logging.getLogger(name).addFilter(_filter)
    token = _private.set(True)
    try:
        yield
    except BaseException as exc:
        _mark_private_exception(exc)
        raise
    finally:
        _private.reset(token)
