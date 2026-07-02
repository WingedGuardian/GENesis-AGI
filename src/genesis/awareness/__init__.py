"""Genesis Awareness package.

Public API:
- AwarenessLoop: main heartbeat loop (periodic signal collection + depth ticks)
- JobRetryRegistry: retry tracking for failed awareness jobs
- Depth, SignalReading, TickResult: lightweight leaf types

AwarenessLoop and JobRetryRegistry are exposed LAZILY (PEP 562 module __getattr__)
so importing the leaf types — or any module that transitively touches this package
(e.g. cc.contingency importing Depth) — does NOT eagerly load loop.py and its heavy
async machinery. Eager-loading loop.py here previously created an import cycle
(cc.contingency -> awareness.types -> this __init__ -> loop -> cc.contingency). For
eager access, import from the submodules directly (genesis.awareness.loop /
genesis.awareness.job_retry).
"""

from genesis.awareness.types import Depth, SignalReading, TickResult

__all__ = [
    "AwarenessLoop",
    "Depth",
    "JobRetryRegistry",
    "SignalReading",
    "TickResult",
]


def __getattr__(name: str):
    # PEP 562 lazy re-export — see the module docstring for the cycle rationale.
    # Cache the resolved symbol back onto the module so later lookups are plain
    # dict hits and __getattr__ is not re-entered.
    if name == "AwarenessLoop":
        from genesis.awareness.loop import AwarenessLoop

        globals()[name] = AwarenessLoop
        return AwarenessLoop
    if name == "JobRetryRegistry":
        from genesis.awareness.job_retry import JobRetryRegistry

        globals()[name] = JobRetryRegistry
        return JobRetryRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
