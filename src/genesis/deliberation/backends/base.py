"""Backend protocol for `deliberate()`.

A backend turns a question into a DeliberationResult. It owns ALL error handling
and returns ``DeliberationResult(error=...)`` on failure — it does not raise. New
backends (Fugu, a Genesis-orchestrated panel) are added as new files implementing
this Protocol, with no change to the core or to existing backends.
"""

from __future__ import annotations

from typing import Protocol

from genesis.deliberation.types import DeliberationResult


class Backend(Protocol):
    name: str

    async def run(
        self,
        question: str,
        *,
        context: str | None,
        stakes: str,
        timeout_s: float,
        models: list[str] | None,
    ) -> DeliberationResult: ...
