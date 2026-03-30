"""Ego compaction engine — manages rolling memory across ego cycles.

Each ego cycle's output is stored in the ego_cycles table.  Before each
new cycle the engine checks for uncompacted cycles beyond the retention
window.  If found, the oldest gets summarised by a cheap LLM and folded
into a running ``compacted_summary`` in ego_state.

Graceful degradation: when the LLM call fails, compaction is skipped
for this cycle.  The cycle data is never lost — it stays uncompacted and
will be picked up next time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiosqlite

from genesis.db.crud import ego as ego_crud
from genesis.ego.types import EgoCycle

if TYPE_CHECKING:
    from genesis.ego.context import EgoContextBuilder
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compaction prompt
# ---------------------------------------------------------------------------

_COMPACTION_SYSTEM_PROMPT = """\
You are a memory compaction assistant for an autonomous AI system called Genesis.

Your job: fold one ego cycle's output into an existing running summary,
preserving what matters and discarding what doesn't.

PRESERVE:
- Decisions made and WHY (the rationale matters)
- Outcomes observed (did past decisions work?)
- Patterns learned (what keeps happening?)
- Open threads still active (unfinished business)
- Proposals that were approved or rejected (and why)
- The ego's current strategic focus areas

DISCARD:
- Full reasoning chains (keep the conclusion, not the journey)
- Investigation details (keep the finding, not the process)
- Raw signal data (keep "error rate spiked" not the numbers)
- Verbose context descriptions (keep actionable summaries)
- Proposals that were fully executed with no follow-up needed

OUTPUT FORMAT:
Return ONLY the updated summary text.  No preamble, no explanation.
Keep the summary structured with clear sections.  Use bullet points.
Target: keep the summary under 2000 words.  If it grows beyond that,
compress older entries more aggressively.

STRUCTURE the summary as:
1. **Active Threads** — what the ego is still working on
2. **Completed Resolutions** — solved problems (don't re-examine)
3. **Stable Patterns** — recurring themes and user preferences
4. **Proposal Track Record** — approval/rejection patterns by category\
"""

_NO_PRIOR_SUMMARY = (
    "(No prior summary — this is the first compaction.  "
    "Extract key information from the cycle below.)"
)


def _build_compaction_prompt(
    *,
    existing_summary: str | None,
    cycle_output: str,
    cycle_focus: str,
    cycle_created_at: str,
) -> list[dict[str, str]]:
    """Build the messages list for the compaction LLM call."""
    summary_text = existing_summary or _NO_PRIOR_SUMMARY
    user_content = (
        f"## Existing Summary\n{summary_text}\n\n"
        f"## New Cycle to Fold In\n"
        f"**Date**: {cycle_created_at}\n"
        f"**Focus**: {cycle_focus}\n\n"
        f"{cycle_output}\n\n"
        f"---\n\n"
        f"Produce the updated summary that incorporates this cycle's "
        f"key information."
    )
    return [
        {"role": "system", "content": _COMPACTION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# CompactionEngine
# ---------------------------------------------------------------------------


class CompactionEngine:
    """Manages the ego's rolling memory via incremental compaction.

    Parameters
    ----------
    db:
        Open aiosqlite connection (with row_factory = aiosqlite.Row).
    router:
        LLM router for cheap-model summarisation calls.
    window_size:
        Number of recent uncompacted cycles to keep in full text.
    call_site_id:
        Routing call-site for the summarisation LLM call.
    """

    DEFAULT_WINDOW_SIZE = 10
    STATE_KEY_SUMMARY = "compacted_summary"
    MAX_CYCLE_OUTPUT_CHARS = 3000

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        router: Router,
        window_size: int = DEFAULT_WINDOW_SIZE,
        call_site_id: str = "8_memory_consolidation",
    ) -> None:
        self._db = db
        self._router = router
        self._window_size = window_size
        self._call_site_id = call_site_id

    # -- Public API --------------------------------------------------------

    async def store_cycle(self, cycle: EgoCycle) -> str:
        """Persist a completed ego cycle to the database.

        Returns the cycle id.
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

    async def maybe_compact(self) -> bool:
        """Compact ONE cycle beyond the retention window.

        Returns True if a cycle was compacted, False if nothing to
        compact or if the LLM call failed (graceful degradation).

        Designed to be called once at the start of each ego cycle.
        """
        candidates = await ego_crud.list_uncompacted_beyond_window(
            self._db, window_size=self._window_size,
        )
        if not candidates:
            return False

        # Take the oldest candidate.
        oldest = candidates[0]
        cycle_id = oldest["id"]
        cycle_output = oldest["output_text"]
        cycle_focus = oldest["focus_summary"]
        cycle_created_at = oldest["created_at"]

        existing_summary = await ego_crud.get_state(
            self._db, self.STATE_KEY_SUMMARY,
        )

        # Truncate before sending to cheap LLM to avoid exceeding its
        # context window.  MAX_CYCLE_OUTPUT_CHARS is also used for display
        # truncation in assemble_context; use a larger limit here (5x)
        # because the compaction prompt should preserve more detail.
        _COMPACTION_CHAR_LIMIT = self.MAX_CYCLE_OUTPUT_CHARS * 5  # 15000
        if len(cycle_output) > _COMPACTION_CHAR_LIMIT:
            cycle_output = cycle_output[:_COMPACTION_CHAR_LIMIT] + "\n[truncated]"

        new_summary = await self._summarize_cycle(
            existing_summary=existing_summary,
            cycle_output=cycle_output,
            cycle_focus=cycle_focus,
            cycle_created_at=cycle_created_at,
        )

        if new_summary is None:
            # LLM call failed — skip compaction, retry next cycle.
            return False

        # Atomic write: update summary + mark cycle compacted.
        # Uses raw SQL instead of CRUD functions because CRUD functions
        # call db.commit() individually — we need both writes in one
        # explicit transaction.  An explicit BEGIN is required so that
        # ROLLBACK actually discards uncommitted writes (implicit
        # transactions behave unpredictably with rollback).
        try:
            await self._db.execute("BEGIN")
            await self._db.execute(
                "INSERT INTO ego_state (key, value, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET "
                "  value = excluded.value, updated_at = datetime('now')",
                (self.STATE_KEY_SUMMARY, new_summary),
            )
            await self._db.execute(
                "UPDATE ego_cycles SET compacted_into = ? WHERE id = ?",
                (self.STATE_KEY_SUMMARY, cycle_id),
            )
            await self._db.commit()
        except Exception:
            logger.error(
                "Compaction transaction failed for cycle %s", cycle_id,
                exc_info=True,
            )
            try:
                await self._db.rollback()
            except Exception:
                logger.error("Rollback also failed", exc_info=True)
            return False

        logger.info(
            "Compacted ego cycle %s (summary now %d chars)",
            cycle_id, len(new_summary),
        )
        return True

    async def get_compacted_summary(self) -> str | None:
        """Retrieve the current compacted summary from ego_state."""
        return await ego_crud.get_state(self._db, self.STATE_KEY_SUMMARY)

    async def assemble_context(
        self,
        *,
        context_builder: EgoContextBuilder,
    ) -> str:
        """Assemble the full context for a new ego cycle.

        Returns a markdown string combining:
        1. Compacted summary (all old cycles compressed)
        2. Last N uncompacted cycle outputs in full text
        3. Fresh input context from EgoContextBuilder
        """
        sections: list[str] = []
        sections.append("# Ego Memory State\n")

        # Section 1: Compacted history
        summary = await self.get_compacted_summary()
        sections.append("## Compacted History\n")
        if summary:
            sections.append(summary)
        else:
            sections.append("*(No compacted history yet — system is fresh.)*")
        sections.append("")

        # Section 2: Recent cycles (last N uncompacted, oldest first).
        # Assumption: the most recent window_size cycles are always
        # uncompacted because maybe_compact only touches cycles OUTSIDE
        # the window.  The filter is defensive — in normal operation all
        # returned rows pass.
        recent = await ego_crud.list_recent_cycles(
            self._db, limit=self._window_size,
        )
        uncompacted = [r for r in recent if r["compacted_into"] is None]
        uncompacted.reverse()

        if uncompacted:
            sections.append(f"## Recent Cycles (last {len(uncompacted)})\n")
            for cycle in uncompacted:
                sections.append(f"### Cycle — {cycle['created_at']}")
                sections.append(f"**Focus**: {cycle['focus_summary']}\n")
                output = cycle["output_text"]
                if len(output) > self.MAX_CYCLE_OUTPUT_CHARS:
                    output = (
                        output[:self.MAX_CYCLE_OUTPUT_CHARS]
                        + f"\n\n*[truncated — {len(cycle['output_text'])} chars]*"
                    )
                sections.append(output)
                sections.append("\n---\n")

        # Section 3: Fresh situational context
        sections.append("## Current Situational Context\n")
        fresh_context = await context_builder.build()
        sections.append(fresh_context)

        return "\n".join(sections)

    # -- Internal ----------------------------------------------------------

    async def _summarize_cycle(
        self,
        *,
        existing_summary: str | None,
        cycle_output: str,
        cycle_focus: str,
        cycle_created_at: str,
    ) -> str | None:
        """Send one cycle to a cheap LLM for summarisation.

        Returns the updated summary text, or None on failure.
        """
        messages = _build_compaction_prompt(
            existing_summary=existing_summary,
            cycle_output=cycle_output,
            cycle_focus=cycle_focus,
            cycle_created_at=cycle_created_at,
        )

        try:
            result = await self._router.route_call(
                self._call_site_id, messages,
            )
        except Exception:
            # route_call should never raise, but be defensive
            # (matches codebase convention in code_audit.py, contingency.py).
            logger.error(
                "Router raised during compaction", exc_info=True,
            )
            return None

        if not result.success or not result.content:
            logger.warning(
                "Compaction LLM call failed: %s (attempts=%d, providers=%s)",
                result.error, result.attempts, result.failed_providers,
            )
            return None

        logger.debug(
            "Compaction LLM call succeeded via %s (cost=$%.6f, tokens=%d+%d)",
            result.provider_used, result.cost_usd,
            result.input_tokens, result.output_tokens,
        )
        return result.content
