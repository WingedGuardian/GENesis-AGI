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
    config_version="0.1.0-default",
    source="",
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
        acceptance, snapshot_id, config_version, "2026-07-01T00:00:00+00:00", source,
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


# ── PR3c-1: config_version filter (list + all 4 aggregates), config_versions, backfill, differ read ──

@pytest.mark.asyncio
async def test_list_and_stats_scope_to_config_version(tmp_path):
    """R4: the version filter must reach list AND every aggregate — else a 2nd version
    persisting makes the stats panel show mixed counts while the list filters correctly."""
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [
        _row(1, activation="soft", triggers=(("multi_speaker", "soft", 0.4),),
             suppressors=("explicit_dismissal",), config_version="0.1.0-default"),
        _row(2, activation="hard", triggers=(("ambient_name", "hard", 0.0),),
             acceptance="should", config_version="0.1.0-default"),
        _row(3, activation="soft", triggers=(("decision", "soft", 0.3),),
             config_version="0.2.0-taxonomy"),
    ])
    # list
    assert {e["id"] for e in await crud.list_events(db, config_version="0.1.0-default")} == {"id-1", "id-2"}
    assert {e["id"] for e in await crud.list_events(db, config_version="0.2.0-taxonomy")} == {"id-3"}
    # aggregates, scoped
    assert await crud.activation_stats(db, "0.1.0-default") == {"soft": 1, "hard": 1}
    assert await crud.activation_stats(db, "0.2.0-taxonomy") == {"soft": 1}
    assert await crud.trigger_stats(db, "0.1.0-default") == {"multi_speaker": 1, "ambient_name": 1}
    assert await crud.trigger_stats(db, "0.2.0-taxonomy") == {"decision": 1}
    assert await crud.suppressor_stats(db, "0.1.0-default") == {"explicit_dismissal": 1}
    assert await crud.suppressor_stats(db, "0.2.0-taxonomy") == {}
    assert (await crud.label_counts(db, "0.1.0-default"))["labeled"] == 1
    assert (await crud.label_counts(db, "0.2.0-taxonomy")) == {
        "total": 1, "labeled": 0, "unlabeled": 1, "by_signal": {},
    }
    # unfiltered still aggregates across both versions (back-compat)
    assert await crud.activation_stats(db) == {"soft": 2, "hard": 1}
    await db.close()


@pytest.mark.asyncio
async def test_config_versions_is_distinct_sorted_unfiltered(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [
        _row(1, config_version="0.2.0-taxonomy"),
        _row(2, config_version="0.1.0-default"),
        _row(3, config_version="0.2.0-taxonomy"),
    ])
    assert await crud.config_versions(db) == ["0.1.0-default", "0.2.0-taxonomy"]
    await db.close()


# ── device provenance: source filter + sources dropdown (PR-4) ─────────────────────

@pytest.mark.asyncio
async def test_list_events_filters_by_source(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [_row(1, source="omi"), _row(2, source="ambient-edge")])
    assert [e["id"] for e in await crud.list_events(db, source="omi")] == ["id-1"]
    got = await crud.list_events(db)                        # no filter -> both
    assert {e["source"] for e in got} == {"omi", "ambient-edge"}
    await db.close()


@pytest.mark.asyncio
async def test_sources_is_distinct_sorted_nonempty(tmp_path):
    # mirrors config_versions(): populates the device-filter dropdown; blank/NULL excluded
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [
        _row(1, source="omi"), _row(2, source="omi"), _row(3, source="ambient-edge"), _row(4),
    ])
    assert await crud.sources(db) == ["ambient-edge", "omi"]
    await db.close()


@pytest.mark.asyncio
async def test_load_fire_records_returns_all_rows_for_version(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [
        _row(1, utt_ids=(11,), config_version="0.1.0-default"),
        _row(2, utt_ids=(22,), config_version="0.1.0-default"),
        _row(3, utt_ids=(33,), config_version="0.2.0-taxonomy"),
    ])
    rows = await crud.load_fire_records(db, "0.1.0-default")
    assert {r["id"] for r in rows} == {"id-1", "id-2"}          # version-scoped
    assert all("triggers_fired" in r and "window_ref" in r for r in rows)  # JSON cols present (still strings)
    await db.close()


@pytest.mark.asyncio
async def test_load_labeled_fires_scopes_labeled_nonskip_with_score_and_signal(tmp_path):
    """PR3c-2a: only should/shouldnt rows of ONE version, carrying score+clarity+acceptance_signal."""
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [
        _row(1, utt_ids=(11,), acceptance="should", score=0.71, config_version="0.2.0-taxonomy"),
        _row(2, utt_ids=(22,), acceptance="shouldnt", config_version="0.2.0-taxonomy"),
        _row(3, utt_ids=(33,), acceptance="skip", config_version="0.2.0-taxonomy"),      # excluded
        _row(4, utt_ids=(44,), acceptance=None, config_version="0.2.0-taxonomy"),        # excluded
        _row(5, utt_ids=(55,), acceptance="should", config_version="0.1.0-default"),     # other version
    ])
    rows = await crud.load_labeled_fires(db, "0.2.0-taxonomy")
    assert {r["id"] for r in rows} == {"id-1", "id-2"}                       # labeled non-skip, this version
    r1 = next(r for r in rows if r["id"] == "id-1")
    assert r1["acceptance_signal"] == "should" and r1["score"] == 0.71 and r1["clarity"] == 0.9
    await db.close()


@pytest.mark.asyncio
async def test_update_l15_verdict_backfills_even_on_labeled_row(tmp_path):
    """B3: l15_verdict is a MACHINE field — unconditional, unlike the label-guarded upsert."""
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [_row(1)])
    await crud.update_acceptance_signal(db, "id-1", "should")  # LABEL the row
    assert await crud.update_l15_verdict(db, "id-1", {"real": 0.8, "perk": 0.6}) is True
    ev = await crud.get_event(db, "id-1")
    assert json.loads(ev["l15_verdict"]) == {"real": 0.8, "perk": 0.6}
    assert ev["acceptance_signal"] == "should"                 # label untouched
    # None clears it; missing id returns False
    assert await crud.update_l15_verdict(db, "id-1", None) is True
    assert (await crud.get_event(db, "id-1"))["l15_verdict"] is None
    assert await crud.update_l15_verdict(db, "missing", {"real": 1.0, "perk": 1.0}) is False
    await db.close()


@pytest.mark.asyncio
async def test_bulk_update_l15_verdicts_writes_even_on_labeled_rows(tmp_path):
    """The shadow runner's backfill path: attach verdicts to MANY rows in one pass, including
    already-LABELED rows (which bulk_upsert's label guard freezes). Verdict is a machine field."""
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [_row(1), _row(2)])
    await crud.update_acceptance_signal(db, "id-1", "should")   # LABEL id-1 (frozen to upsert)
    v1 = json.dumps({"real": 0.8, "perk": 0.6, "category": "question"})
    v2 = json.dumps({"real": 0.2, "perk": 0.1, "category": "garble"})
    n = await crud.bulk_update_l15_verdicts(db, [("id-1", v1), ("id-2", v2)])
    assert n == 2
    e1 = await crud.get_event(db, "id-1")
    assert json.loads(e1["l15_verdict"])["category"] == "question"   # verdict landed on labeled row
    assert e1["acceptance_signal"] == "should"                       # label untouched
    e2 = await crud.get_event(db, "id-2")
    assert json.loads(e2["l15_verdict"])["real"] == 0.2
    await db.close()


@pytest.mark.asyncio
async def test_bulk_update_l15_verdicts_empty_is_noop(tmp_path):
    db = await _db(tmp_path / "g.db")
    assert await crud.bulk_update_l15_verdicts(db, []) == 0
    await db.close()


@pytest.mark.asyncio
async def test_label_with_note_persists_reviewer_reasoning(tmp_path):
    """PR3d: the reviewer's WHY is the point of the review loop — a label can carry a note."""
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [_row(1)])
    ok, prior = await crud.update_acceptance_signal(
        db, "id-1", "shouldnt", note="trivial + self-answerable; didn't want interrupting"
    )
    assert ok and prior is None
    ev = await crud.get_event(db, "id-1")
    assert ev["acceptance_signal"] == "shouldnt"
    assert ev["acceptance_note"] == "trivial + self-answerable; didn't want interrupting"


@pytest.mark.asyncio
async def test_relabel_without_note_leaves_existing_note_untouched(tmp_path):
    """Omitting the note (sentinel) must PRESERVE a prior note — only an explicit value changes it."""
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [_row(1)])
    await crud.update_acceptance_signal(db, "id-1", "should", note="original reasoning")
    await crud.update_acceptance_signal(db, "id-1", "shouldnt")          # no note arg
    ev = await crud.get_event(db, "id-1")
    assert ev["acceptance_signal"] == "shouldnt"
    assert ev["acceptance_note"] == "original reasoning"                 # preserved


@pytest.mark.asyncio
async def test_label_note_can_be_explicitly_cleared(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.bulk_upsert_events(db, [_row(1)])
    await crud.update_acceptance_signal(db, "id-1", "should", note="x")
    await crud.update_acceptance_signal(db, "id-1", "should", note=None)  # explicit clear
    assert (await crud.get_event(db, "id-1"))["acceptance_note"] is None
    await db.close()
