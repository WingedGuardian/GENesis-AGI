"""Task executor package --- autonomous multi-step task execution.

Re-exports the public API so callers can do::

    from genesis.autonomy.executor import CCSessionExecutor, TaskReviewer
"""

from genesis.autonomy.executor.engine import CCSessionExecutor
from genesis.autonomy.executor.review import ReviewResult, TaskReviewer, VerifyResult
from genesis.autonomy.executor.trace import ExecutionTracer
from genesis.autonomy.executor.types import (
    VALID_TRANSITIONS,
    ExecutionTrace,
    ExecutionTracerProto,
    StepResult,
    StepType,
    TaskPhase,
    WorkaroundResult,
    WorkaroundSearcher,
)
from genesis.autonomy.executor.workaround import WorkaroundSearcherImpl

__all__ = [
    "CCSessionExecutor",
    "ExecutionTrace",
    "ExecutionTracer",
    "ExecutionTracerProto",
    "ReviewResult",
    "StepResult",
    "StepType",
    "TaskPhase",
    "TaskReviewer",
    "VALID_TRANSITIONS",
    "VerifyResult",
    "WorkaroundResult",
    "WorkaroundSearcher",
    "WorkaroundSearcherImpl",
]
