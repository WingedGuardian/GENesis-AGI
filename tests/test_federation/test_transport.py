"""MockTransport — the at-least-once + idempotency contract the receive path
relies on, plus payload opacity (the relay never inspects the envelope)."""

from __future__ import annotations

import pytest

from genesis.federation.transport import MockTransport


@pytest.mark.asyncio
async def test_send_then_poll_roundtrip():
    t = MockTransport()
    await t.send("mbB", {"envelope_id": "e1", "payload": {"ct": "xyz"}})
    items = await t.poll("mbB")
    assert len(items) == 1
    assert items[0]["envelope"]["envelope_id"] == "e1"
    assert items[0]["envelope"]["payload"] == {"ct": "xyz"}


@pytest.mark.asyncio
async def test_send_is_idempotent_on_envelope_id():
    t = MockTransport()
    await t.send("mbB", {"envelope_id": "e1", "payload": "a"})
    await t.send("mbB", {"envelope_id": "e1", "payload": "a"})  # re-send: no dup
    assert len(await t.poll("mbB")) == 1


@pytest.mark.asyncio
async def test_poll_after_cursor_returns_only_newer():
    t = MockTransport()
    await t.send("mbB", {"envelope_id": "e1", "payload": 1})
    await t.send("mbB", {"envelope_id": "e2", "payload": 2})
    first = await t.poll("mbB")
    cur = first[0]["cursor"]
    rest = await t.poll("mbB", after_cursor=cur)
    assert [i["envelope"]["envelope_id"] for i in rest] == ["e2"]
    # polling after the last cursor yields nothing
    assert await t.poll("mbB", after_cursor=rest[-1]["cursor"]) == []


@pytest.mark.asyncio
async def test_send_requires_envelope_id():
    t = MockTransport()
    with pytest.raises(ValueError):
        await t.send("mbB", {"payload": "no id"})


@pytest.mark.asyncio
async def test_mailboxes_are_independent():
    t = MockTransport()
    await t.send("mbA", {"envelope_id": "a1", "payload": "x"})
    await t.send("mbB", {"envelope_id": "b1", "payload": "y"})
    assert [i["envelope"]["envelope_id"] for i in await t.poll("mbA")] == ["a1"]
    assert [i["envelope"]["envelope_id"] for i in await t.poll("mbB")] == ["b1"]


@pytest.mark.asyncio
async def test_ack_does_not_lose_messages_for_the_reader():
    t = MockTransport()
    await t.send("mbB", {"envelope_id": "e1", "payload": "x"})
    items = await t.poll("mbB")
    await t.ack("mbB", items[0]["cursor"])
    # ack tracks a watermark; the reader still drives its own cursor
    assert await t.poll("mbB") == items  # re-poll from start still returns it


@pytest.mark.asyncio
async def test_stored_envelope_is_a_copy():
    t = MockTransport()
    env = {"envelope_id": "e1", "payload": "x"}
    await t.send("mbB", env)
    env["payload"] = "MUTATED"  # mutate caller's dict after send
    got = await t.poll("mbB")
    assert got[0]["envelope"]["payload"] == "x"  # transport kept its own copy
