"""Base types for content processors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class ProcessedContent:
    """Standardized output from any content processor."""

    text: str
    metadata: dict = field(default_factory=dict)
    source_type: str = "unknown"
    source_path: str = ""
    sections: list[str] | None = None


@runtime_checkable
class ContentProcessor(Protocol):
    """Protocol for content processors."""

    async def process(self, source: str, **kwargs: object) -> ProcessedContent:
        """Process a source (file path or URL) into standardized text."""
        ...

    def can_handle(self, source: str) -> bool:
        """Check if this processor can handle the given source."""
        ...
