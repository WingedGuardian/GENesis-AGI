"""ContextVar-based session tracking for event bus observability.

Provides implicit session_id propagation to emit() calls without
requiring every call site to pass session_id explicitly.  Set the
session_id at entry points (ego cycles, direct sessions, reflection
bridges) and all downstream emit() calls inherit it automatically.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

_current_session_id: ContextVar[str | None] = ContextVar(
    "genesis_session_id", default=None
)


def get_session_id() -> str | None:
    """Return the current session_id, or None if not in a session scope."""
    return _current_session_id.get()


def set_session_id(session_id: str | None) -> None:
    """Set the current session_id for the running context."""
    _current_session_id.set(session_id)


@contextmanager
def session_scope(session_id: str) -> Generator[None]:
    """Context manager that sets session_id for the duration of a block.

    Restores the previous value on exit (supports nesting).
    """
    token = _current_session_id.set(session_id)
    try:
        yield
    finally:
        _current_session_id.reset(token)
