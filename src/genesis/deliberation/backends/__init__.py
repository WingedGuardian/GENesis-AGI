"""Deliberation backends registry.

v1 ships Fusion only. Fugu (`model: fugu`) and a Genesis-orchestrated panel land as new
files registered here, with no change to `deliberate()` or existing backends.
"""

from __future__ import annotations

from genesis.deliberation.backends.base import Backend
from genesis.deliberation.backends.fusion import FusionBackend

BACKENDS: dict[str, Backend] = {"fusion": FusionBackend()}


def get_backend(name: str) -> Backend | None:
    return BACKENDS.get(name)
