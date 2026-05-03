"""genesis.reflection — Deep reflection orchestration (Phase 7).

Handles periodic deep reflection: context gathering, output routing,
learning stability monitoring, and weekly scheduled assessments.
Separate from genesis.perception which handles real-time Micro/Light reflection.

Public API (import from genesis.reflection directly):
- ContextGatherer: Assembles context windows for reflection prompts
- OutputRouter: Routes reflection outputs to appropriate destinations
- ReflectionScheduler: Manages deep/strategic reflection scheduling
- QuestionGate: Controls when reflection questions are surfaced
"""

from genesis.reflection.context_gatherer import ContextGatherer
from genesis.reflection.output_router import OutputRouter
from genesis.reflection.question_gate import QuestionGate
from genesis.reflection.scheduler import ReflectionScheduler

__all__ = [
    "ContextGatherer",
    "OutputRouter",
    "QuestionGate",
    "ReflectionScheduler",
]
