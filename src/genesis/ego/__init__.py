"""Genesis Ego — the autonomous decision-making session.

The ego is Genesis's first ability to think-then-act autonomously.
It sits above reflections (perception) as the action layer:

    Awareness Loop (SENSE)
      → Reflections (PERCEIVE)
        → Ego Session (DECIDE + ACT)

Key design decisions:
- Proposal mode first: ego proposes actions via Telegram, user approves
- Thinks as Genesis: full operational awareness, not just a task runner
- Continuity via managed context with periodic compaction
- Adaptive cadence: pauses when user is active, backs off when idle
"""

from genesis.ego.cadence import EgoCadenceManager
from genesis.ego.compaction import CompactionEngine
from genesis.ego.dispatch import EgoDispatcher
from genesis.ego.proposals import ProposalWorkflow
from genesis.ego.session import EgoSession
from genesis.ego.types import (
    EgoConfig,
    EgoCycle,
    EgoProposal,
    ProposalStatus,
)

__all__ = [
    "CompactionEngine",
    "EgoCadenceManager",
    "EgoConfig",
    "EgoCycle",
    "EgoDispatcher",
    "EgoProposal",
    "EgoSession",
    "ProposalStatus",
    "ProposalWorkflow",
]
