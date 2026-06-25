"""Homegrown OpenTelemetry-convention span context — the tracing primitive.

A ``Span`` is one timed node in a trace; spans nest via a per-process
``ContextVar`` (mirrors ``session_context.py``). ``start_span`` is a context
manager: it makes the new span the current one for its block, parents it under
whatever span is active (or starts a new root trace), captures an exception as
``status='error'``, and emits the finished span to the injected ``SpanWriter`` on
close. NO OpenTelemetry SDK — the shapes follow OTel conventions so a future
export adapter is trivial, but nothing here depends on the SDK.

Safety posture (matches ``ProviderActivityTracker``): tracing is best-effort and
transparent. The ONLY thing ``start_span`` ever propagates to the caller is the
caller's own exception — every tracer fault (setup, ContextVar, writer) is
debug-logged and swallowed. There is no ``await`` on the span path; the writer's
``record`` is a sync list-append.

Kill switch: capture is OFF unless a writer is wired AND not disabled. Disable
universally (server, MCP, hooks) by setting ``GENESIS_SPANS_DISABLED=1`` in the
environment — read once when the writer is wired. When disabled, ``start_span``
yields a no-op sentinel so call sites stay valid with zero overhead.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class SpanKind(StrEnum):
    """Span categories (extensible — un-CHECKed in the DB, validated here)."""

    OPERATION = "operation"  # a logical cognitive op (reflection/ego cycle)
    LLM = "llm"  # a single route_call LLM invocation
    TOOL = "tool"  # a CC tool invocation (from the PostToolUse hook)
    CC_SESSION = "cc_session"  # a dispatched Claude Code session
    INTERNAL = "internal"  # generic internal step
    RECALL = "recall"  # reserved — memory/knowledge recall (later phase)
    EXECUTOR = "executor"  # reserved — executor task/step (later phase)


@dataclass
class Span:
    """A single trace node. Field names map 1:1 to ``otel_spans`` columns."""

    span_id: str
    trace_id: str
    parent_span_id: str | None
    name: str
    kind: str
    start_unix_us: int
    _t0_mono: float  # not persisted — monotonic origin for duration
    status: str = "ok"
    status_message: str | None = None
    end_unix_us: int | None = None
    duration_us: int | None = None
    session_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    # denormalized LLM block (populated only for kind='llm')
    call_site: str | None = None
    provider: str | None = None
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cost_known: bool | None = None

    def set_status_error(self, msg: str) -> None:
        self.status = "error"
        self.status_message = msg[:1024]

    def set_attr(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_llm_fields(
        self,
        *,
        call_site: str | None = None,
        provider: str | None = None,
        model_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
        cost_known: bool | None = None,
    ) -> None:
        self.call_site = call_site
        self.provider = provider
        self.model_id = model_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.cost_known = cost_known


class _NullSpan:
    """No-op span yielded when capture is disabled — keeps call sites valid."""

    span_id = None
    trace_id = None

    def set_status_error(self, *a: Any, **k: Any) -> None: ...
    def set_attr(self, *a: Any, **k: Any) -> None: ...
    def set_llm_fields(self, *a: Any, **k: Any) -> None: ...


_NULL_SPAN = _NullSpan()


class _Writer(Protocol):
    def record(self, span: Span) -> None: ...


# Per-process, auto-propagating across await / create_task / gather / to_thread.
_current_span: ContextVar[Span | None] = ContextVar("genesis_current_span", default=None)

# Injected at bootstrap (runtime/init/observability.py). None => capture off.
_writer: _Writer | None = None
_enabled: bool = True


def _env_disabled() -> bool:
    return os.environ.get("GENESIS_SPANS_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def set_writer(writer: _Writer | None, *, enabled: bool = True) -> None:
    """Wire the span writer (once, at bootstrap). Honors GENESIS_SPANS_DISABLED."""
    global _writer, _enabled
    _writer = writer
    _enabled = enabled and not _env_disabled()


def is_enabled() -> bool:
    """True when spans are actively captured (writer wired and not disabled)."""
    return _writer is not None and _enabled


def current_trace_context() -> tuple[str, str] | None:
    """(trace_id, span_id) of the active span — for cross-process env injection."""
    sp = _current_span.get()
    if sp is None:
        return None
    return (sp.trace_id, sp.span_id)


def _new_id() -> str:
    return uuid.uuid4().hex


def _now_us() -> int:
    return int(time.time() * 1_000_000)


@contextmanager
def start_span(
    name: str,
    kind: str = SpanKind.INTERNAL,
    *,
    attributes: dict[str, Any] | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
) -> Generator[Span | _NullSpan]:
    """Open a span for the duration of the block.

    Parent resolution: explicit ``parent_span_id``/``trace_id`` args (used for
    cross-process handoff) > the current ContextVar span (in-process nesting) >
    a brand-new root trace. Yields ``_NULL_SPAN`` when capture is disabled.
    """
    if _writer is None or not _enabled:
        yield _NULL_SPAN
        return

    token = None
    try:
        from genesis.observability.session_context import get_session_id

        parent = _current_span.get()
        tid = trace_id or (parent.trace_id if parent else _new_id())
        pid = (
            parent_span_id
            if parent_span_id is not None
            else (parent.span_id if parent else None)
        )
        span = Span(
            span_id=_new_id(),
            trace_id=tid,
            parent_span_id=pid,
            name=name,
            kind=str(kind),
            start_unix_us=_now_us(),
            _t0_mono=time.monotonic(),
            session_id=get_session_id(),
            attributes=dict(attributes or {}),
        )
        token = _current_span.set(span)
    except Exception:
        logger.debug("start_span setup failed", exc_info=True)
        if token is not None:
            with contextlib.suppress(Exception):
                _current_span.reset(token)
        yield _NULL_SPAN
        return

    try:
        yield span
    except Exception as exc:
        span.set_status_error(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        with contextlib.suppress(Exception):
            span.end_unix_us = _now_us()
            span.duration_us = int((time.monotonic() - span._t0_mono) * 1_000_000)
        with contextlib.suppress(Exception):
            _current_span.reset(token)
        try:
            _writer.record(span)
        except Exception:
            logger.debug("span record failed", exc_info=True)
