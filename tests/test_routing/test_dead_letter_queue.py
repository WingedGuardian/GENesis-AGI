"""Tests for DeadLetterQueue."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from genesis.db.crud import dead_letter as dl_crud
from genesis.routing.dead_letter import DeadLetterQueue


@dataclass
class _FakeRoutingResult:
    success: bool = True
    error: str | None = None


@pytest.fixture
def clock():
    now = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
    return lambda: now


@pytest.fixture
def dlq(db, clock):
    return DeadLetterQueue(db, clock=clock)


@pytest.mark.asyncio
async def test_enqueue_creates_record(dlq, db):
    item_id = await dlq.enqueue("llm_call", {"prompt": "hi"}, "anthropic", "rate_limit")
    row = await dl_crud.get_by_id(db, item_id)
    assert row is not None
    assert row["operation_type"] == "llm_call"
    assert row["status"] == "pending"
    assert row["target_provider"] == "anthropic"


@pytest.mark.asyncio
async def test_enqueue_string_payload(dlq, db):
    item_id = await dlq.enqueue("llm_call", "raw text", "openai", "timeout")
    row = await dl_crud.get_by_id(db, item_id)
    assert row["payload"] == "raw text"


@pytest.mark.asyncio
async def test_get_pending_count(dlq):
    await dlq.enqueue("a", "{}", "anthropic", "err")
    await dlq.enqueue("b", "{}", "openai", "err")
    await dlq.enqueue("c", "{}", "anthropic", "err")

    assert await dlq.get_pending_count() == 3
    assert await dlq.get_pending_count(target_provider="anthropic") == 2
    assert await dlq.get_pending_count(target_provider="openai") == 1


@pytest.mark.asyncio
async def test_replay_marks_replayed(dlq, db):
    id1 = await dlq.enqueue("a", "{}", "anthropic", "err")
    await dlq.enqueue("b", "{}", "openai", "err")

    count = await dlq.replay_pending("anthropic")
    assert count == 1

    row = await dl_crud.get_by_id(db, id1)
    assert row["status"] == "replayed"

    # openai item still pending
    assert await dlq.get_pending_count(target_provider="openai") == 1


@pytest.mark.asyncio
async def test_expire_old(db):
    old_time = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)

    dlq_old = DeadLetterQueue(db, clock=lambda: old_time)
    old_id = await dlq_old.enqueue("a", "{}", "anthropic", "err")

    dlq_new = DeadLetterQueue(db, clock=lambda: now)
    new_id = await dlq_new.enqueue("b", "{}", "anthropic", "err")

    expired = await dlq_new.expire_old(max_age_hours=72)
    assert expired == 1

    old_row = await dl_crud.get_by_id(db, old_id)
    assert old_row["status"] == "expired"

    new_row = await dl_crud.get_by_id(db, new_id)
    assert new_row["status"] == "pending"


# ── Redispatch tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redispatch_succeeds(dlq, db):
    """Items with call_site_id are re-dispatched via dispatch_fn."""
    payload = {"call_site_id": "4_light_reflection", "messages": [{"role": "user", "content": "hi"}]}
    item_id = await dlq.enqueue("chain_exhausted:4_light_reflection", payload, "all", "exhausted")

    async def dispatch_fn(call_site_id, messages, **kwargs):
        assert call_site_id == "4_light_reflection"
        assert kwargs.get("suppress_dead_letter") is True
        return _FakeRoutingResult(success=True)

    succeeded, failed = await dlq.redispatch(dispatch_fn)
    assert succeeded == 1
    assert failed == 0

    row = await dl_crud.get_by_id(db, item_id)
    assert row["status"] == "replayed"


@pytest.mark.asyncio
async def test_redispatch_failure_increments_retry(dlq, db):
    """Failed re-dispatch increments retry count, leaves pending."""
    payload = {"call_site_id": "4_light_reflection", "messages": []}
    item_id = await dlq.enqueue("chain_exhausted:4_light_reflection", payload, "all", "exhausted")

    async def dispatch_fn(call_site_id, messages, **kwargs):
        return _FakeRoutingResult(success=False)

    succeeded, failed = await dlq.redispatch(dispatch_fn)
    assert succeeded == 0
    assert failed == 1

    row = await dl_crud.get_by_id(db, item_id)
    assert row["status"] == "pending"
    assert row["retry_count"] == 1


@pytest.mark.asyncio
async def test_redispatch_expires_unknown_call_site(dlq, db):
    """Items whose call_site_id no longer exists in config are expired, not retried.

    Regression: two stuck DLQ entries for call_site_id `contingency_inbox`
    sat at retry_count=15 for hours because redispatch() treated "unknown
    call site" as just another failure instead of a permanent one.
    Example scenario: config reload renames or removes a call site.
    """
    from genesis.routing.router import UNKNOWN_CALL_SITE_ERROR_PREFIX

    payload = {"call_site_id": "ghost_call_site", "messages": []}
    item_id = await dlq.enqueue(
        "chain_exhausted:ghost_call_site", payload, "all", "exhausted",
    )

    calls: list[str] = []

    async def dispatch_fn(call_site_id, messages, **kwargs):
        calls.append(call_site_id)
        return _FakeRoutingResult(
            success=False,
            error=f"{UNKNOWN_CALL_SITE_ERROR_PREFIX} {call_site_id}",
        )

    succeeded, failed = await dlq.redispatch(dispatch_fn)
    # Expiry doesn't count as success OR failure — it's a terminal state.
    assert succeeded == 0
    assert failed == 0
    assert calls == ["ghost_call_site"]  # dispatched exactly once

    row = await dl_crud.get_by_id(db, item_id)
    assert row["status"] == "expired"
    # Retry count should NOT be incremented — we gave up, not retried.
    assert row["retry_count"] == 0


@pytest.mark.asyncio
async def test_redispatch_expires_legacy_items(dlq, db):
    """Items without call_site_id (legacy truncated) are marked expired."""
    item_id = await dlq.enqueue(
        "chain_exhausted:4_light_reflection",
        json.dumps({"messages": ["truncated..."]}),
        "all", "exhausted",
    )

    async def dispatch_fn(call_site_id, messages, **kwargs):
        raise AssertionError("should not be called for legacy items")

    succeeded, failed = await dlq.redispatch(dispatch_fn)
    assert succeeded == 0
    assert failed == 0

    row = await dl_crud.get_by_id(db, item_id)
    assert row["status"] == "expired"


@pytest.mark.asyncio
async def test_redispatch_handles_dispatch_exception(dlq, db):
    """Dispatch exceptions increment retry, don't crash."""
    payload = {"call_site_id": "test_site", "messages": []}
    item_id = await dlq.enqueue("test_op", payload, "all", "err")

    async def dispatch_fn(call_site_id, messages, **kwargs):
        raise ConnectionError("provider down")

    succeeded, failed = await dlq.redispatch(dispatch_fn)
    assert succeeded == 0
    assert failed == 1

    row = await dl_crud.get_by_id(db, item_id)
    assert row["status"] == "pending"
    assert row["retry_count"] == 1


@pytest.mark.asyncio
async def test_redispatch_recovers_legacy_str_messages(dlq, db):
    """Legacy entries with str(dict) messages are parsed back to dicts."""
    # Simulate legacy format: str(dict) produces Python repr with single quotes
    legacy_msg = str({"role": "user", "content": "hello"})
    payload = {"call_site_id": "test_site", "messages": [legacy_msg]}
    await dlq.enqueue("test_op", payload, "all", "exhausted")

    received_messages = []

    async def dispatch_fn(call_site_id, messages, **kwargs):
        received_messages.extend(messages)
        return _FakeRoutingResult(success=True)

    succeeded, failed = await dlq.redispatch(dispatch_fn)
    assert succeeded == 1
    assert failed == 0
    assert len(received_messages) == 1
    assert received_messages[0]["role"] == "user"
    assert received_messages[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_redispatch_handles_dict_messages(dlq, db):
    """New entries with proper dict messages pass through correctly."""
    payload = {
        "call_site_id": "test_site",
        "messages": [{"role": "system", "content": "You are a helpful assistant"}],
    }
    await dlq.enqueue("test_op", payload, "all", "exhausted")

    received_messages = []

    async def dispatch_fn(call_site_id, messages, **kwargs):
        received_messages.extend(messages)
        return _FakeRoutingResult(success=True)

    succeeded, failed = await dlq.redispatch(dispatch_fn)
    assert succeeded == 1
    assert len(received_messages) == 1
    assert received_messages[0]["role"] == "system"
