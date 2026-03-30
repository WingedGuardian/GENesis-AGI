"""Real signal collectors for Phase 6 — replace Phase 1 stubs."""

from genesis.learning.signals.budget import BudgetCollector
from genesis.learning.signals.cc_version import CCVersionCollector
from genesis.learning.signals.conversation import ConversationCollector
from genesis.learning.signals.critical_failure import CriticalFailureCollector
from genesis.learning.signals.error_spike import ErrorSpikeCollector
from genesis.learning.signals.memory_backlog import MemoryBacklogCollector
from genesis.learning.signals.task_quality import TaskQualityCollector

__all__ = [
    "BudgetCollector",
    "CCVersionCollector",
    "ConversationCollector",
    "CriticalFailureCollector",
    "ErrorSpikeCollector",
    "MemoryBacklogCollector",
    "TaskQualityCollector",
]
