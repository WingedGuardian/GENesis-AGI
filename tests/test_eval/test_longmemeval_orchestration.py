"""Orchestration test: run_longmemeval actually runs questions concurrently.

Guards the code-review finding that a synchronous OpenAI client called from
``async def`` without ``to_thread`` would block the event loop and silently
defeat ``concurrency``. With the ``asyncio.to_thread`` offload, multiple
questions' LLM calls overlap. Uses fakes only — no network.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time

import pytest

from genesis.eval.longmemeval.dataset import LongMemEvalInstance, Turn
from genesis.eval.longmemeval.query import QueryArm
from genesis.eval.longmemeval.runner import Arm, run_longmemeval
from genesis.qdrant.collections import VECTOR_DIM

_TOKEN = re.compile(r"[a-z0-9]+")


class _HashingEmbedder:
    tracker = None

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * VECTOR_DIM
        for tok in _TOKEN.findall(text.lower()):
            h = int(hashlib.sha1(tok.encode()).hexdigest(), 16)  # noqa: S324
            vec[h % VECTOR_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_batch(self, texts):
        return [await self.embed(t) for t in texts]


class _ConcurrencyTrackingClient:
    """Sync client whose create() sleeps and records max concurrent calls."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

        chat = type("Chat", (), {})()
        completions = type("Completions", (), {})()

        def create(**kwargs):
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.15)  # simulate a blocking network round-trip
            with self._lock:
                self.active -= 1
            return _FakeCompletion("yes Business Administration")

        completions.create = create
        chat.completions = completions
        self.chat = chat


class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 2})()
        self.model = "openai/gpt-4o-2024-08-06"


def _instance(qid: str) -> LongMemEvalInstance:
    return LongMemEvalInstance(
        question_id=qid,
        question_type="single-session-user",
        question="What degree did I graduate with?",
        answer="Business Administration",
        question_date="2023/05/23 (Tue) 19:11",
        haystack_dates=["2023/05/01 (Mon) 10:00"],
        haystack_session_ids=["s1"],
        haystack_sessions=[
            [
                Turn("user", "I graduated with a degree in Business Administration.", True),
            ],
        ],
        answer_session_ids=["a1"],
    )


@pytest.mark.asyncio
async def test_run_longmemeval_runs_questions_concurrently():
    instances = [_instance(f"q{i}") for i in range(4)]
    client = _ConcurrencyTrackingClient()
    # one arm keeps the LLM-call count low; concurrency=3 across 4 questions
    summaries = await run_longmemeval(
        instances,
        db=None,
        arms=[Arm(QueryArm.RAW, rerank=False)],
        k=5,
        concurrency=3,
        client=client,
        embedding_provider=_HashingEmbedder(),
    )
    # sanity: all four questions produced a result in the single arm
    assert summaries["raw"].total_cases == 4
    # the offload lets multiple blocking LLM calls overlap; a blocking-on-loop
    # implementation would serialize to max_active == 1
    assert client.max_active >= 2
