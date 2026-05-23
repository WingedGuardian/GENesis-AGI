"""AgentProvider protocol — abstract interface for agent invocation.

CCInvoker implements this today. Alternative backends (Codex CLI, Pi)
can implement this protocol to become swappable agent providers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from genesis.cc.types import CCInvocation, CCOutput, StreamEvent


@runtime_checkable
class AgentProvider(Protocol):
    """Abstract agent invocation interface."""

    async def run(self, invocation: CCInvocation) -> CCOutput: ...

    async def run_streaming(
        self,
        invocation: CCInvocation,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None = None,
    ) -> CCOutput: ...

    async def interrupt(self) -> None: ...
