"""Real signal collectors for Phase 6 — replace Phase 1 bootstrap placeholders.

See ``genesis/awareness/signals.py`` module docstring for the two-phase
bootstrap contract: awareness init registers placeholder collectors from
``genesis.awareness.signals``, and learning init swaps them for the real
implementations exported here.
"""

from genesis.learning.signals.budget import BudgetCollector
from genesis.learning.signals.cc_version import CCVersionCollector
from genesis.learning.signals.conversation import ConversationCollector
from genesis.learning.signals.critical_failure import CriticalFailureCollector
from genesis.learning.signals.error_spike import ErrorSpikeCollector
from genesis.learning.signals.genesis_version import GenesisVersionCollector
from genesis.learning.signals.task_quality import TaskQualityCollector

__all__ = [
    "BudgetCollector",
    "CCVersionCollector",
    "GenesisVersionCollector",
    "ConversationCollector",
    "CriticalFailureCollector",
    "ErrorSpikeCollector",
    "TaskQualityCollector",
]
