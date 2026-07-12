"""Data shapes for the infrastructure body schema.

The persisted profile is a plain JSON dict (see ``store.py``); these dataclasses
type the collector boundary only. The facts/metrics split is load-bearing:

- ``facts``   — slow-changing configuration/topology. Hashed; a hash change is
  drift (observation + annotation regeneration). Collectors MUST emit
  deterministic values here: stable key sets, lists in a defined order.
- ``metrics`` — volatile readings (free bytes, counts, offsets, states).
  Rendered in the document but NEVER hashed, so they cannot churn annotations
  or spam drift observations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Section status values.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_UNAVAILABLE = "unavailable"

PLANE_CONTAINER = "container"
PLANE_HOST = "host"


@dataclass
class SectionResult:
    """One collector's output for one profile section."""

    name: str
    plane: str = PLANE_CONTAINER
    status: str = STATUS_OK
    facts: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def failed(cls, name: str, error: str, plane: str = PLANE_CONTAINER) -> SectionResult:
        return cls(name=name, plane=plane, status=STATUS_ERROR, error=error)

    @classmethod
    def unavailable(cls, name: str, reason: str, plane: str = PLANE_HOST) -> SectionResult:
        return cls(name=name, plane=plane, status=STATUS_UNAVAILABLE, error=reason)
