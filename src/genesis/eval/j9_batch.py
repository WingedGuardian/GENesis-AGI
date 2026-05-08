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

        content = f"J9 eval batch: scored {scored} memory-query pairs ({errors} errors)"
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
