"""Shared LongMemEval test fixtures (deterministic, no network, no private data).

``make_linkable_instance`` builds the canonical "two near-identical evidence
turns across two sessions" instance: the token-hashing test embedder scores
the pair well above the 0.75 link threshold, and a ``k=1`` recall retrieves
exactly one of them — so graph expansion is the ONLY way to reach full
evidence coverage. Used by both the store wiring tests and the orchestration
tests; keep the linkability recipe in ONE place.
"""

from __future__ import annotations

from genesis.eval.longmemeval.dataset import LongMemEvalInstance, Turn

_BASE = (
    "I spent the whole afternoon repairing the old wooden fence around the "
    "garden before the storm arrived"
)


def make_linkable_instance(qid: str = "q_link") -> LongMemEvalInstance:
    return LongMemEvalInstance(
        question_id=qid,
        question_type="multi-session",
        question="What did I repair around the garden before the storm arrived?",
        answer="the wooden fence",
        question_date="2023/05/23 (Tue) 19:11",
        haystack_dates=["2023/05/01 (Mon) 10:00", "2023/05/02 (Tue) 10:00"],
        haystack_session_ids=["s1", "s2"],
        haystack_sessions=[
            [Turn("user", f"{_BASE} yesterday.", True)],
            [Turn("user", f"{_BASE} today.", True)],
        ],
        answer_session_ids=["a1", "a2"],
    )
