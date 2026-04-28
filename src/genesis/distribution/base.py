"""Base types and protocol for platform distributors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class PostResult:
    """Result of a platform publish attempt."""

    post_id: str | None
    platform: str
    url: str | None
    status: str  # "published" | "failed" | "draft"
    error: str | None = None


@runtime_checkable
class PlatformDistributor(Protocol):
    """Interface for platform-specific content delivery.

    Mirrors the channel adapter shape used in OutreachPipeline._channels
    so the patterns are parallel and a future unification is possible.
    """

    @property
    def platform(self) -> str: ...

    async def publish(
        self,
        content: str,
        *,
        visibility: str = "PUBLIC",
    ) -> PostResult: ...

    async def delete(self, post_id: str) -> bool: ...
