"""Content integrity utilities for ego pipeline audit tracking.

Provides lightweight hash and size computation for tracking content
mutations through the ego cycle → realist gate → proposal pipeline.
"""

from __future__ import annotations

import hashlib


def content_hash(text: str) -> str:
    """SHA-256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode()).hexdigest()


def content_size(text: str) -> int:
    """Byte count of UTF-8 encoded text."""
    return len(text.encode())
