"""crud.attention: label-preserving upsert (B1), exact json_each filters (B2), aggregates, labeling."""
import json

import aiosqlite
import pytest

from genesis.db.crud import attention as crud
from genesis.db.schema._tables import INDEXES, TABLES


async def _db(path):
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute(TABLES["attention_events"])
    for idx in INDEXES:
        if "attention_events" in idx:
            await conn.execute(idx)
    await conn.commit()
    return conn


def _row(
    n,
    *,
    activation="soft",
    score=0.6,
    triggers=(("multi_speaker", "soft", 0.4),),
    suppressors=(),
    acceptance=None,
    ts="2026-07-01T00:00:00+00:00",
    session_id="s1",
    snapshot_id="20260701T013412Z",
    utt_ids=(1, 2, 3),
):
    """A full attention_events tuple in COLUMNS order."""
    triggers_json = json.dumps([{"name": nm, "kind": k, "contribution": c} for nm, k, c in triggers])
    window_ref = json.dumps({
        "snapshot_id": snapshot_id, "session_id": session_id,
        "utt_ids": list(utt_ids), "ts_start": 0.0, "ts_end": 1.0,
    })
    return (
        f"id-{n}", ts, session_id, activation, score, triggers_json,
        json.dumps(list(suppressors)), window_ref, "unknown", 0.9, None,
        acceptance, snapshot_id, "0.1.0-default", "2026-07-01T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_bulk_upsert_preserves_label_on_reflush(tmp_path):
    """B1: re-running the runner over a labeled snapshot must NOT clobber the label."""
    db = await _db(tmp_path / "g.db")
    assert await crud.bulk_upsert_events(db, [_row(1)]) == 1
    ok, prior = await crud.update_acceptance_signal(db, "id-1", "should")
    assert ok and prior is None
    # re-persist the SAME id (same snapshot+config) with a changed derived column
    await crud.bulk_upsert_events(db, [_row(1, score=0.99)])
    ev = await crud.get_event(db, "id-1")
    assert ev["acceptance_signal"] == "should"   # label PRESERVED
    assert ev["score"] == 0.6                     # labeled row is FROZEN (runner can't mutate it)
    await db.close()


@pytest.mark.asyncio
async def test_unlabeled_row_refreshes_on_reflush(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [_row(1, score=0.5)])
    await crud.bulk_upsert_events(db, [_row(1, score=0.8)])
    ev = await crud.get_event(db, "id-1")
    assert ev["score"] == 0.8 and ev["acceptance_signal"] is None
    await db.close()


@pytest.mark.asyncio
async def test_list_filter_trigger_exact_not_like(tmp_path):
    """B2: filtering trigger='soft' (a kind, not a name) must match nothing."""
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [
        _row(1, triggers=(("multi_speaker", "soft", 0.4),)),
        _row(2, triggers=(("question", "soft", 0.3), ("is_user", "soft", 0.1))),
    ])
    assert {e["id"] for e in await crud.list_events(db, trigger="multi_speaker")} == {"id-1"}
    assert {e["id"] for e in await crud.list_events(db, trigger="question")} == {"id-2"}
    assert await crud.list_events(db, trigger="soft") == []      # kind, not a name
    assert await crud.list_events(db, trigger="contribution") == []  # JSON key, not a name
    await db.close()


@pytest.mark.asyncio
async def test_list_filter_is_user_and_activation_and_unlabeled(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [
        _row(1, activation="soft", triggers=(("is_user", "soft", 0.1),)),
        _row(2, activation="hard", triggers=(("ambient_name", "hard", 0.0),)),
        _row(3, activation="soft", triggers=(("multi_speaker", "soft", 0.4),), acceptance="skip"),
    ])
    assert {e["id"] for e in await crud.list_events(db, is_user=True)} == {"id-1"}
    assert {e["id"] for e in await crud.list_events(db, activation="hard")} == {"id-2"}
    assert {e["id"] for e in await crud.list_events(db, unlabeled=True)} == {"id-1", "id-2"}
    # combined trigger + is_user (AND semantics): id-1 has is_user, not multi_speaker
    assert await crud.list_events(db, trigger="multi_speaker", is_user=True) == []
    await db.close()


@pytest.mark.asyncio
async def test_list_orders_desc_and_paginates(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [
        _row(1, ts="2026-07-01T00:00:01+00:00"),
        _row(2, ts="2026-07-01T00:00:03+00:00"),
        _row(3, ts="2026-07-01T00:00:02+00:00"),
    ])
    ordered = [e["id"] for e in await crud.list_events(db)]
    assert ordered == ["id-2", "id-3", "id-1"]           # newest first
    assert [e["id"] for e in await crud.list_events(db, limit=1)] == ["id-2"]
    assert [e["id"] for e in await crud.list_events(db, limit=1, offset=1)] == ["id-3"]
    await db.close()


@pytest.mark.asyncio
async def test_stats_aggregates(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [
        _row(1, activation="soft", triggers=(("multi_speaker", "soft", 0.4), ("is_user", "soft", 0.1))),
        _row(2, activation="soft", triggers=(("multi_speaker", "soft", 0.4),), suppressors=("explicit_dismissal",)),
        _row(3, activation="hard", triggers=(("ambient_name", "hard", 0.0),), acceptance="should"),
    ])
    assert await crud.activation_stats(db) == {"soft": 2, "hard": 1}
    trig = await crud.trigger_stats(db)
    assert trig["multi_speaker"] == 2 and trig["is_user"] == 1 and trig["ambient_name"] == 1
    assert list(trig)[0] == "multi_speaker"              # ORDER BY n DESC -> dominance first
    assert await crud.suppressor_stats(db) == {"explicit_dismissal": 1}
    lc = await crud.label_counts(db)
    assert lc == {"total": 3, "labeled": 1, "unlabeled": 2, "by_signal": {"should": 1}}
    await db.close()


@pytest.mark.asyncio
async def test_update_acceptance_signal_validation_and_missing(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [_row(1)])
    with pytest.raises(ValueError):
        await crud.update_acceptance_signal(db, "id-1", "maybe")
    assert await crud.update_acceptance_signal(db, "missing", "should") == (False, None)
    # relabel returns the prior value
    await crud.update_acceptance_signal(db, "id-1", "should")
    ok, prior = await crud.update_acceptance_signal(db, "id-1", "shouldnt")
    assert ok and prior == "should"
    assert (await crud.get_event(db, "id-1"))["acceptance_signal"] == "shouldnt"
    await db.close()


@pytest.mark.asyncio
async def test_get_event_missing_returns_none(tmp_path):
    db = await _db(tmp_path / "g.db")
    assert await crud.get_event(db, "nope") is None
    await db.close()
