"""Genesis cognitive surplus — intentional use of free compute."""

from genesis.surplus.brainstorm import BrainstormRunner
from genesis.surplus.compute_availability import ComputeAvailability
from genesis.surplus.executor import StubExecutor
from genesis.surplus.idle_detector import IdleDetector
from genesis.surplus.queue import SurplusQueue
from genesis.surplus.scheduler import SurplusScheduler
from genesis.surplus.types import (
    ComputeTier,
    ExecutorResult,
    SurplusExecutor,
    SurplusTask,
    TaskStatus,
    TaskType,
)

__all__ = [
    "BrainstormRunner",
    "ComputeAvailability",
    "ComputeTier",
    "ExecutorResult",
    "IdleDetector",
    "StubExecutor",
    "SurplusExecutor",
    "SurplusQueue",
    "SurplusScheduler",
    "SurplusTask",
    "TaskStatus",
    "TaskType",
]
