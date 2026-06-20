"""J-9 daily eval batch — LLM-judged memory relevance scoring.

Runs as a surplus task. For each recall_fired event from the past 24h,
asks a cheap model to judge whether each recalled memory was relevant
to the query. Stores recall_relevance events.

Routes through the Genesis LLM router (call site "judge") to use whatever
provider is available — no hardcoded model dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from genesis.db.crud import j9_eval
from genesis.surplus.types import ExecutorResult, SurplusTask

if TYPE_CHECKING:
    import aiosqlite

    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# Max concurrent judge calls. Mirrors knowledge.distillation's
# _MAX_CONCURRENT_CHUNKS=4. The real throughput cap is the per-provider rate
# gate (judge chain → openrouter-deepseek-v4 @ 20 RPM = 3s/dispatch); the
# semaphore just bounds in-flight coroutines (≈ response_latency/interval),
# so going higher only queues on the rate-gate lock.
_MAX_CONCURRENT_JUDGES = 4

# Internal wall-clock budget for one batch run. Chosen to sit comfortably below
# the surplus "running" reaper threshold (2h, surplus/queue.py:recover_stuck
# older_than_hours=2) so a slow run returns a graceful partial instead of being
# killed and retried-from-scratch. A healthy run finishes in ~25 min (rate-gate
# bound), so this only bites under a degraded provider — exactly when a partial
# (resumed next run via checkpointing) beats a hard kill.
_BATCH_DEADLINE_S = 90 * 60

# Upper bound for the checkpoint lookback query. Generous so historical
# duplicates from prior failed attempts are all captured and skipped.
_CHECKPOINT_QUERY_LIMIT = 10000

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

    def __init__(
        self,
        *,
        db: aiosqlite.Connection | None = None,
        router: Router | None = None,
    ) -> None:
        self._db = db
        self._router = router

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        if self._db is None:
            return ExecutorResult(success=False, error="no db connection")

        now = datetime.now(UTC)
        since = (now - timedelta(hours=24)).isoformat()
        until = now.isoformat()
        deadline = time.monotonic() + _BATCH_DEADLINE_S

        recall_events = await j9_eval.get_events(
            self._db,
            dimension="memory",
            event_type="recall_fired",
            since=since,
            until=until,
            limit=200,
        )

        if not recall_events:
            return ExecutorResult(
                success=True,
                content="J9 eval batch: no recall events in past 24h",
            )

        # Checkpoint: skip (recall_event, memory) pairs already judged in this
        # window. Makes a reaper-retried run RESUME instead of restarting from
        # scratch, and eliminates the duplicate recall_relevance events that
        # prior restart-from-scratch retries produced.
        already_judged = await self._judged_pairs(
            since, until, event_type="recall_relevance",
        )

        # Build the judgment worklist (skipping already-judged pairs).
        worklist: list[tuple[dict, str, str, int]] = []
        for event in recall_events:
            metrics = event.get("metrics", {})
            query = metrics.get("query", "")
            memory_ids = metrics.get("memory_ids", [])
            if not query or not memory_ids:
                continue
            # rank = the memory's position in this recall's memory_ids[:5] (1-5),
            # persisted per relevance event so the aggregator computes MRR by
            # retrieval rank — not by concurrent-insert / DB (timestamp DESC) order.
            for rank, mid in enumerate(memory_ids[:5], 1):  # Cap at top-5 per recall
                if (event["id"], mid) in already_judged:
                    continue
                worklist.append((event, query, mid, rank))

        scored = 0
        errors = 0
        deferred = 0
        sem = asyncio.Semaphore(_MAX_CONCURRENT_JUDGES)

        async def _judge_and_store(
            index: int, event: dict, query: str, mid: str, rank: int,
        ) -> None:
            nonlocal scored, errors, deferred
            # Deadline check before acquiring a slot — past the budget, defer
            # the rest to the next (checkpointed) run rather than risk the
            # 2h reaper.
            if time.monotonic() > deadline:
                deferred += 1
                return
            async with sem:
                if time.monotonic() > deadline:
                    deferred += 1
                    return
                content = await self._get_memory_content(mid)
                if not content:
                    return
                # chain_offset rotates the judge provider chain per item so
                # concurrent calls spread across providers (same pattern as
                # knowledge.distillation).
                relevance, rationale, model_used = await self._judge_relevance(
                    query, content, chain_offset=index,
                )
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
                            "rank": rank,
                            "relevance": relevance,
                            "judge_rationale": rationale,
                            "judge_model": model_used,
                        },
                    )
                    scored += 1
                else:
                    errors += 1

        results = await asyncio.gather(
            *(
                _judge_and_store(i, event, query, mid, rank)
                for i, (event, query, mid, rank) in enumerate(worklist)
            ),
            return_exceptions=True,
        )
        # return_exceptions=True turns a raised coroutine into a returned value;
        # surface it instead of silently dropping it.
        for r in results:
            if isinstance(r, Exception):
                logger.warning("J9 judge coroutine raised: %s", r)
                errors += 1

        # Pass 2: recall_used — check if recalled memories were referenced
        # in session-extracted content (text overlap, no LLM needed).
        used_count, used_cut_short = await self._compute_recall_used(
            since, until, recall_events, deadline,
        )

        partial = deferred > 0 or used_cut_short
        content = (
            f"J9 eval batch: scored {scored} memory-query pairs "
            f"({errors} errors, {len(already_judged)} already-judged skipped, "
            f"{deferred} deferred to next run), {used_count} recall_used events"
            + (" [PARTIAL — resumes next run]" if partial else "")
        )
        return ExecutorResult(
            success=True,
            content=content,
            insights=[{
                "content": content,
                "source_task_type": task.task_type,
                "generating_model": "router:judge",
                "drive_alignment": "competence",
                "confidence": 0.8 if errors == 0 else 0.6,
            }],
        )

    async def _judged_pairs(
        self, since: str, until: str, *, event_type: str,
    ) -> set[tuple[str, str]]:
        """Return the set of (recall_event_id, memory_id) pairs already emitted
        for *event_type* in the window — the checkpoint for resume/dedup."""
        if self._db is None:
            return set()
        events = await j9_eval.get_events(
            self._db,
            dimension="memory",
            event_type=event_type,
            since=since,
            until=until,
            limit=_CHECKPOINT_QUERY_LIMIT,
        )
        if len(events) >= _CHECKPOINT_QUERY_LIMIT:
            # Checkpoint may be incomplete → some pairs could be re-judged.
            # Loud rather than silent (the very backlog this fix prevents).
            logger.warning(
                "J9 checkpoint query for %s hit limit %d — checkpoint may be "
                "incomplete; some pairs may be re-judged this run",
                event_type, _CHECKPOINT_QUERY_LIMIT,
            )
        pairs: set[tuple[str, str]] = set()
        for ev in events:
            m = ev.get("metrics", {})
            rid = m.get("recall_event_id")
            mid = m.get("memory_id")
            if rid and mid:
                pairs.add((rid, mid))
        return pairs

    async def _compute_recall_used(
        self, since: str, until: str, recall_events: list[dict],
        deadline: float,
    ) -> tuple[int, bool]:
        """Check if recalled memories were referenced in session-extracted content.

        Uses text overlap (trigram similarity) between recalled memory content
        and memories extracted from the same session. No LLM calls needed.
        Checkpointed (skips already-emitted pairs) and deadline-aware so a
        retried run resumes rather than re-emitting duplicates.

        Returns ``(used_count, cut_short)`` — ``cut_short`` is True if the
        deadline interrupted pass 2 (remaining sessions resume next run).
        """
        if self._db is None:
            return 0, False

        already_used = await self._judged_pairs(
            since, until, event_type="recall_used",
        )

        # Group recall events by session
        by_session: dict[str, list[dict]] = {}
        for ev in recall_events:
            sid = ev.get("session_id")
            if sid:
                by_session.setdefault(sid, []).append(ev)

        used_count = 0
        cut_short = False
        for session_id, events in by_session.items():
            if not session_id:
                continue  # Skip events without session attribution
            if time.monotonic() > deadline:
                cut_short = True
                break  # Out of budget — remaining sessions resume next run
            # Get memories extracted FROM this session via pending_embeddings
            # which tracks source_session_id for each extracted memory.
            try:
                cursor = await self._db.execute(
                    """SELECT content FROM memory_fts
                       WHERE memory_id IN (
                           SELECT memory_id FROM pending_embeddings
                           WHERE source_session_id = ?
                             AND status = 'embedded'
                           LIMIT 100
                       )""",
                    (session_id,),
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
                    if (ev["id"], mid) in already_used:
                        continue  # Checkpoint: already emitted in this window
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

        return used_count, cut_short

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
        self, query: str, memory_content: str, *, chain_offset: int = 0,
    ) -> tuple[float | None, str, str]:
        """Ask the judge model to score relevance via the router.

        Returns (relevance_score, rationale_or_error, model_used).
        ``chain_offset`` rotates the provider chain so concurrent callers
        spread across the judge chain's providers.
        """
        if self._router is None:
            return (None, "no router configured", "none")

        prompt = _RELEVANCE_PROMPT.format(
            query=query[:500],
            memory_content=memory_content[:1000],
        )
        try:
            result = await self._router.route_call(
                call_site_id="judge",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=150,
                chain_offset=chain_offset,
            )
            model_used = result.model_id or result.provider_used or "router:judge"
            if not result.success:
                logger.debug("J9 relevance routing failed: %s", result.error)
                return (None, result.error or "routing failed", model_used)
            text = result.content or ""
            # Parse JSON response
            parsed = json.loads(text.strip())
            if "relevance" not in parsed:
                # Valid JSON but no 'relevance' — route to the error path (None)
                # so it is NOT stored as a fake relevance=0.0 event that would
                # silently pollute precision@5 / MRR.
                raise ValueError("judge response missing required 'relevance' key")
            relevance = float(parsed["relevance"])
            rationale = parsed.get("rationale", "")
            return (max(0.0, min(1.0, relevance)), rationale, model_used)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("J9 relevance parse error: %s", exc)
            return (None, str(exc), "router:judge")
        except Exception as exc:
            logger.debug("J9 relevance LLM error: %s", exc)
            return (None, str(exc), "router:judge")
