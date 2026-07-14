"""Tests for LongMemEval haystack ingest + date normalization (WS-1 A4)."""

from __future__ import annotations

import pytest

from genesis.eval.longmemeval.dataset import LongMemEvalInstance, Turn
from genesis.eval.longmemeval.ingest import ingest_haystack, normalize_date
from genesis.memory.provenance import ORIGIN_FIRST_PARTY


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2023/05/01 (Mon) 10:00", "2023-05-01T10:00:00"),
        ("2023/12/31 (Sun) 23:59", "2023-12-31T23:59:00"),
        ("no date here", None),
        ("", None),
        (None, None),
        ("2023/13/40 (Xxx) 99:99", None),  # regex matches but datetime() raises
    ],
)
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


class _FakeStore:
    """Records store() calls; returns a synthetic id per call."""

    def __init__(self):
        self.calls: list[dict] = []

    async def store(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return f"mem-{len(self.calls)}"


def _instance() -> LongMemEvalInstance:
    return LongMemEvalInstance(
        question_id="q1",
        question_type="single-session-user",
        question="What degree?",
        answer="BA",
        question_date="2023/05/23 (Tue) 19:11",
        haystack_dates=["2023/05/01 (Mon) 10:00"],
        haystack_session_ids=["s1"],
        haystack_sessions=[
            [
                Turn("user", "I graduated with a BA.", True),
                Turn("assistant", "", False),  # empty -> skipped
                Turn("user", "Unrelated chatter.", False),
            ],
        ],
        answer_session_ids=["a1"],
    )


@pytest.mark.asyncio
async def test_ingest_tags_first_party_and_skips_empty():
    store = _FakeStore()
    result = await ingest_haystack(store, _instance())
    assert result.stored_count == 2  # empty assistant turn skipped
    assert len(store.calls) == 2
    assert all(c["origin_class"] == ORIGIN_FIRST_PARTY for c in store.calls)
    assert all(c["memory_type"] == "episodic" for c in store.calls)


@pytest.mark.asyncio
async def test_ingest_prepends_session_date_to_content():
    # the reader needs the timestamp to reason about time (Codex P1 fix)
    store = _FakeStore()
    await ingest_haystack(store, _instance())
    first = store.calls[0]["content"]
    assert first.startswith("[2023/05/01 (Mon) 10:00] ")
    assert "[user]" in first
    # valid_at is the normalized ISO form
    assert store.calls[0]["valid_at"] == "2023-05-01T10:00:00"


@pytest.mark.asyncio
async def test_ingest_tracks_evidence_ids():
    store = _FakeStore()
    result = await ingest_haystack(store, _instance())
    # only the first turn has_answer=True
    assert result.evidence_memory_ids == {"mem-1"}
