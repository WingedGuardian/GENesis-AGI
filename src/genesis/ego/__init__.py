"""Genesis Ego — the autonomous decision-making session.

Two-ego architecture:

- **User Ego (CEO)**: Proactive user value. Opus, user-focused context.
  Single voice to the user. Continuous cognitive loop.
- **Genesis Ego (COO)**: Self-maintenance, infrastructure. Sonnet,
  system-focused context. Escalates to user ego via observations.

Both egos share the same EgoSession infrastructure (persistent CC
session, compaction, proposals, cadence). They differ in context
builder, prompt, model, and cadence settings.

    Awareness Loop (SENSE)
      → Reflections (PERCEIVE)
        → User Ego (DECIDE for user)
        → Genesis Ego (MAINTAIN infrastructure)
"""

from genesis.ego.cadence import EgoCadenceManager
from genesis.ego.compaction import CompactionEngine
from genesis.ego.context import EgoContextBuilder
from genesis.ego.dispatch import EgoDispatcher
from genesis.ego.executor import EgoProposalExecutor
from genesis.ego.genesis_context import GenesisEgoContextBuilder
from genesis.ego.proposals import ProposalWorkflow
from genesis.ego.session import EgoSession
from genesis.ego.types import (
    EgoConfig,
    EgoCycle,
    EgoProposal,
    ProposalStatus,
)
from genesis.ego.user_context import UserEgoContextBuilder

__all__ = [
    "CompactionEngine",
    "EgoCadenceManager",
    "EgoConfig",
    "EgoContextBuilder",
    "EgoCycle",
    "EgoDispatcher",
    "EgoProposal",
    "EgoProposalExecutor",
    "EgoSession",
    "GenesisEgoContextBuilder",
    "ProposalStatus",
    "ProposalWorkflow",
    "UserEgoContextBuilder",
]
