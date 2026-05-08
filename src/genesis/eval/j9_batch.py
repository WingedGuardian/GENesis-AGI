"""J-9 daily eval batch — LLM-judged memory relevance scoring.

Runs as a surplus task. For each recall_fired event from the past 24h,
asks a cheap model to judge whether each recalled memory was relevant
to the query. Stores recall_relevance events.

Cost: ~50K tokens/day at Haiku ≈ $0.04/day.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import litellm

from genesis.db.crud import j9_eval
from genesis.surplus.types import ExecutorResult, SurplusTask

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# Model for relevance judgments — cheap and fast
_JUDGE_MODEL = "claude-haiku-4-5-20251001"

def _text_overlap(memory_text: str, reference_text: str, min_phrases: int = 2) -> bool:
    """Check if memory content appears in reference text via trigram overlap.

    Extracts 3-word phrases from memory_text and checks if at least
    min_phrases appear in reference_text. This is a cheap proxy for
    "was this memory used?" without LLM judgment.
    """
    words = memory_text.split()
    if len(words) < 3:
        # Short memory: check if the whole thing appears
        return memory_text.strip() in reference_text

    trigrams = [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]
    matches = sum(1 for t in trigrams if t in reference_text)
    return matches >= min_phrases


_RELEVANCE_PROMPT = """\
You are judging whether a recalled memory is relevant to a query.

Query: {query}

Memory content: {memory_content}

Rate the relevance from 0.0 (completely irrelevant) to 1.0 (highly relevant).
A memory is relevant if it provides useful context, background, or information
that would help someone respond to the query.

Respond with ONLY a JSON object: {{"relevance": <float>, "rationale": "<brief reason>"}}"""


class J9EvalBatchExecutor:
    """Surplus executor for J9_EVAL_BATCH tasks.

    Scores memory recall relevance for the past 24 hours.
    """

    def __init__(self, *, db: aiosqlite.Connection | None = None) -> None:
        self._db = db

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        if self._db is None:
            return ExecutorResult(success=False, error="no db connection")

        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        recall_events = await j9_eval.get_events(
            self._db,
            dimension="memory",
            event_type="recall_fired",
            since=since,
            limit=200,
        )

        if not recall_events:
            return ExecutorResult(
                success=True,
                content="J9 eval batch: no recall events in past 24h",
            )

        scored = 0
        errors = 0

        for event in recall_events:
            metrics = event.get("metrics", {})
            query = metrics.get("query", "")
            memory_ids = metrics.get("memory_ids", [])

            if not query or not memory_ids:
                continue

            # Fetch memory content for each recalled memory
            for mid in memory_ids[:5]:  # Cap at top-5 per recall
                content = await self._get_memory_content(mid)
                if not content:
                    continue

                relevance, rationale = await self._judge_relevance(query, content)
                if relevance is not None:
                    await j9_eval.insert_event(
                        self._db,
                        dimension="memory",
                        event_type="recall_relevance",
                        subject_id=mid,
                        session_id=event.get("session_id"),
                        metrics={
                            "recall_event_id": event["id"],
                            "memory_id": mid,
                            "relevance": relevance,
                            "judge_rationale": rationale,
                            "judge_model": _JUDGE_MODEL,
                        },
                    )
                    scored += 1
                else:
                    errors += 1

        # Pass 2: recall_used — check if recalled memories were referenced
        # in session-extracted content (text overlap, no LLM needed)
        used_count = await self._compute_recall_used(since, recall_events)

        content = (
            f"J9 eval batch: scored {scored} memory-query pairs "
            f"({errors} errors), {used_count} recall_used events"
        )
        return ExecutorResult(
            success=True,
            content=content,
            insights=[{
                "content": content,
                "source_task_type": task.task_type,
                "generating_model": _JUDGE_MODEL,
                "drive_alignment": "competence",
                "confidence": 0.8 if errors == 0 else 0.6,
            }],
        )

    async def _compute_recall_used(
        self, since: str, recall_events: list[dict],
    ) -> int:
        """Check if recalled memories were referenced in session-extracted content.

        Uses text overlap (trigram similarity) between recalled memory content
        and memories extracted from the same session. No LLM calls needed.
        """
        if self._db is None:
            return 0

        # Group recall events by session
        by_session: dict[str, list[dict]] = {}
        for ev in recall_events:
            sid = ev.get("session_id")
            if sid:
                by_session.setdefault(sid, []).append(ev)

        used_count = 0
        for session_id, events in by_session.items():
            # Get memories extracted FROM this session (content created by the session)
            try:
                cursor = await self._db.execute(
                    """SELECT content FROM memory_fts
                       WHERE memory_id IN (
                           SELECT memory_id FROM memory_metadata
                           WHERE created_at >= ? LIMIT 100
                       )""",
                    (since,),
                )
                session_content = " ".join(
                    (row[0] if isinstance(row, tuple) else row["content"])
                    for row in await cursor.fetchall()
                )
            except Exception:
                continue

            if not session_content:
                continue

            session_lower = session_content.lower()

            # Check each recalled memory against session content
            for ev in events:
                memory_ids = ev.get("metrics", {}).get("memory_ids", [])
                for mid in memory_ids[:5]:
                    mem_content = await self._get_memory_content(mid)
                    if not mem_content:
                        continue

                    # Trigram overlap: extract 3-word phrases from memory,
                    # check if any appear in session content
                    used = _text_overlap(mem_content.lower(), session_lower)

                    try:
                        await j9_eval.insert_event(
                            self._db,
                            dimension="memory",
                            event_type="recall_used",
                            subject_id=mid,
                            session_id=session_id,
                            metrics={
                                "recall_event_id": ev["id"],
                                "memory_id": mid,
                                "used": used,
                                "method": "trigram_overlap",
                            },
                        )
                        if used:
                            used_count += 1
                    except Exception:
                        logger.debug("Failed to emit recall_used for %s", mid)

        return used_count

    async def _get_memory_content(self, memory_id: str) -> str | None:
        """Fetch memory content from Qdrant payload or FTS5."""
        if self._db is None:
            return None
        try:
            cursor = await self._db.execute(
                "SELECT content FROM memory_fts WHERE memory_id = ? LIMIT 1",
                (memory_id,),
            )
            row = await cursor.fetchone()
            if row:
                return row[0] if isinstance(row, tuple) else row["content"]
        except Exception:
            logger.debug("Failed to fetch memory %s from FTS5", memory_id)
        return None

    async def _judge_relevance(
        self, query: str, memory_content: str,
    ) -> tuple[float | None, str]:
        """Ask the judge model to score relevance."""
        prompt = _RELEVANCE_PROMPT.format(
            query=query[:500],
            memory_content=memory_content[:1000],
        )
        try:
            response = await litellm.acompletion(
                model=_JUDGE_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.0,
            )
            text = response.choices[0].message.content or ""
            # Parse JSON response
            parsed = json.loads(text.strip())
            relevance = float(parsed.get("relevance", 0.0))
            rationale = parsed.get("rationale", "")
            return (max(0.0, min(1.0, relevance)), rationale)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("J9 relevance parse error: %s", exc)
            return (None, str(exc))
        except Exception as exc:
            logger.debug("J9 relevance LLM error: %s", exc)
            return (None, str(exc))
