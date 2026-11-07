"""Content-free diagnostics for an explicitly private native invocation.

The context is task-local: concurrent ordinary invocations retain diagnostics.
Receipt state and timings remain available; raw CLI output/callback exceptions
belong to the private result, not the server log or tracing plane.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

_private = ContextVar("genesis_private_execution", default=False)


def private_execution_active() -> bool:
    return _private.get()


class _PrivateDiagnosticFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not private_execution_active()


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
        "genesis.util.proc_kill",
    ):
        logging.getLogger(name).addFilter(_filter)
    token = _private.set(True)
    try:
        yield
    finally:
        _private.reset(token)
