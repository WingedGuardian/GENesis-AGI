"""Ego cycle storage and context assembly.

Stores completed ego cycles in the ego_cycles table for audit logging
and cost tracking. Assembles operational context for each new cycle
by combining the previous cycle's focus summary with fresh situational
context from the EgoContextBuilder.

The ego uses ephemeral sessions — no compaction, no cycle history
injection, no LLM summarization. Durable knowledge lives in the
memory system (memory_store/memory_recall).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiosqlite

from genesis.db.crud import ego as ego_crud
from genesis.ego.types import EgoCycle

if TYPE_CHECKING:
    from genesis.ego.context import EgoContextBuilder

logger = logging.getLogger(__name__)


class CompactionEngine:
    """Ego cycle storage and context assembly.

    Despite the legacy name, this class no longer performs LLM compaction.
    It stores cycles for audit logging and assembles operational context
    for each new cycle.

    Parameters
    ----------
    db:
        Open aiosqlite connection (with row_factory = aiosqlite.Row).
    """

    DEFAULT_STATE_KEY_SUMMARY = "compacted_summary"

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        state_key_summary: str | None = None,
        focus_summary_key: str | None = None,
        # Legacy params accepted but ignored for backward compatibility
        # with existing runtime init code.
        router: object | None = None,
        window_size: int = 10,
        call_site_id: str = "8_ego_compaction",
    ) -> None:
        self._db = db
        self._state_key_summary = state_key_summary or self.DEFAULT_STATE_KEY_SUMMARY
        self._focus_summary_key = focus_summary_key or "ego_focus_summary"

    # -- Public API --------------------------------------------------------

    async def store_cycle(self, cycle: EgoCycle) -> str:
        """Persist a completed ego cycle to the database.

        Returns the cycle id. Cycles are stored for audit logging,
        cost tracking, and historical analysis.
        """
        return await ego_crud.create_cycle(
            self._db,
            id=cycle.id,
            output_text=cycle.output_text,
            proposals_json=cycle.proposals_json,
            focus_summary=cycle.focus_summary,
            model_used=cycle.model_used,
            cost_usd=cycle.cost_usd,
            input_tokens=cycle.input_tokens,
            output_tokens=cycle.output_tokens,
            duration_ms=cycle.duration_ms,
            created_at=cycle.created_at,
        )

    async def assemble_context(
        self,
        *,
        context_builder: EgoContextBuilder,
    ) -> str:
        """Assemble operational context for a new ego cycle.

        Returns a markdown string combining:
        1. Previous cycle's focus summary (continuity thread)
        2. Fresh situational context from EgoContextBuilder
        """
        sections: list[str] = []

        # Previous focus — one-line continuity from last cycle.
        focus = await ego_crud.get_state(
            self._db, self._focus_summary_key,
        )
        if focus:
            sections.append(f"## Previous Focus\n{focus}\n")

        # Fresh situational context from the context builder.
        sections.append("## Operational Context\n")
        fresh_context = await context_builder.build()
        sections.append(fresh_context)

        return "\n".join(sections)

