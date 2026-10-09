"""Internal peer-to-CC bindings; never a peer-supplied execution shape."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from genesis.cc.peer_segment import PeerSegment


@dataclass(frozen=True)
class PeerSessionBinding:
    task_id: str
    segment: PeerSegment
    generation: int
    facade_config: str
    working_dir: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.task_id, str)
            or not re.fullmatch(r"[a-f0-9]{32}", self.task_id)
            or not isinstance(self.segment, PeerSegment)
            or type(self.generation) is not int
            or self.generation < 0
            or not isinstance(self.facade_config, str)
            or not Path(self.facade_config).is_absolute()
            or not isinstance(self.working_dir, str)
            or not Path(self.working_dir).is_absolute()
        ):
            raise ValueError("Invalid internal peer session binding")

    def metadata(self, ceiling: int) -> dict:
        return {
            "peer_task_id": self.task_id,
            "peer_segment_id": self.segment.segment_id,
            "peer_generation": self.generation,
            "caller_context": f"peer_api:{self.task_id}",
            "autonomy_ceiling": min(3, ceiling),
        }


class PeerSessionLifecycle(Protocol):
    """Unit8 coordinates these checks; no missing-controller fallback exists.

    begin binds the segment/session and verifies current grants, generation,
    expiry, budget and lease readiness immediately before model execution.
    drain returns only after scope AND broker operations have stopped. finish
    publishes the aggregate through the coordinator's generation/hold rules.
    """

    async def authorize(self, binding: PeerSessionBinding) -> None: ...

    async def begin(self, binding: PeerSessionBinding, session_id: str, ceiling: int) -> None: ...

    async def drain(self, binding: PeerSessionBinding) -> None: ...

    async def finish(self, binding: PeerSessionBinding, session_id: str, result: dict) -> None: ...
