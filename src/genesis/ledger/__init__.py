"""WS-2 Cognitive Ledger — falsifiable predictions, mechanically graded.

P1a ships the substrate: the metric registry (``metrics.py``, gate 1 of the
three-gate falsifiability design) and the ``ledger_predictions`` table
(migration 0064). Writer hooks land in P1b; the grader in P2.
"""

from genesis.ledger.metrics import (
    HORIZON_CAP,
    REGISTRY,
    TASK_GRACE,
    MetricSpec,
    Resolution,
)

__all__ = ["HORIZON_CAP", "REGISTRY", "TASK_GRACE", "MetricSpec", "Resolution"]
