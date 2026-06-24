"""`deliberate()` — the chorus.

Ask a panel of models (via a backend) and return a synthesized verdict PLUS the dissent.
Opt-in, recursion-blocked, never-raises. For genuinely high-stakes / explicit decisions —
not a default judgment path.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from genesis.deliberation.backends import get_backend
from genesis.deliberation.types import DeliberationResult, Stakes

logger = logging.getLogger(__name__)

# Blocks a deliberate-within-deliberate (e.g. a future orchestrated backend whose panel
# member re-invokes the tool). ContextVar propagates through awaits and resets cleanly.
_in_deliberate: ContextVar[bool] = ContextVar("genesis_in_deliberate", default=False)


async def deliberate(
    question: str,
    *,
    context: str | None = None,
    stakes: Stakes = "normal",
    backend: str = "fusion",
    timeout_s: float = 180.0,
    models: list[str] | None = None,
) -> DeliberationResult:
    """Run a question through a chorus of models and return verdict + dissent.

    Never raises — every failure comes back as ``DeliberationResult(error=...)``.
    PAID (Fusion) and recursion-blocked.
    """
    if not question or not question.strip():
        return DeliberationResult(answer=None, backend_used=backend, error="question is required")
    if _in_deliberate.get():
        return DeliberationResult(
            answer=None,
            backend_used=backend,
            error="deliberate() is recursion-blocked (already inside a deliberation)",
        )
    be = get_backend(backend)
    if be is None:
        return DeliberationResult(answer=None, backend_used=backend, error=f"unknown backend: {backend}")
    if stakes not in ("normal", "high"):
        stakes = "normal"

    token = _in_deliberate.set(True)
    try:
        return await be.run(question, context=context, stakes=stakes, timeout_s=timeout_s, models=models)
    except Exception as exc:  # noqa: BLE001 — last-resort envelope; backends are not supposed to raise
        logger.exception("deliberate() backend %s raised", backend)
        return DeliberationResult(
            answer=None, backend_used=backend, error=f"deliberate failed: {type(exc).__name__}: {exc}"
        )
    finally:
        _in_deliberate.reset(token)
