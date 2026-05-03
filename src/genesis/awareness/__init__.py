"""Genesis Awareness Loop — the system's heartbeat.

Public API (import from genesis.awareness directly):
- AwarenessLoop: Main loop class — periodic signal collection and depth-based ticking
- JobRetryRegistry: Retry tracking for failed awareness jobs
- SignalReading: Data class for individual signal measurements
- Depth: Enum for tick depth levels (MICRO, LIGHT, DEEP, STRATEGIC)
- TickResult: Result of a single awareness tick
"""

from genesis.awareness.job_retry import JobRetryRegistry
from genesis.awareness.loop import AwarenessLoop
from genesis.awareness.types import Depth, SignalReading, TickResult

__all__ = [
    "AwarenessLoop",
    "Depth",
    "JobRetryRegistry",
    "SignalReading",
    "TickResult",
]
