"""AgentProvider protocol — abstract interface for agent invocation.

CCInvoker implements this today. SDK/ACP backends can be added in V5
(see docs/plans/v5-hybrid-agent-protocol.md).
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
