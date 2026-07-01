"""ShadowStoreConsumer persistence (via the crud layer) + the refs-not-text firewall."""
import json
import sqlite3

import pytest

from genesis.attention.config import AttentionConfig, default_config_dict
from genesis.attention.consumers import ShadowStoreConsumer
from genesis.attention.engine import evaluate
from genesis.attention.types import AmbientUtterance, EngineState
from genesis.db.schema._tables import INDEXES, TABLES


def _make_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(TABLES["attention_events"])
    for idx in INDEXES:
        if "attention_events" in idx:
            conn.execute(idx)
    conn.commit()
    conn.close()


def _utt(id, ts, text, **kw) -> AmbientUtterance:
    d = dict(duration_s=5.0, is_user=1, speaker_total=2, n_tokens=20,
             frac_lt_1=0.0, rms=0.2, mode_state="unknown", source="t")
    d.update(kw)
    return AmbientUtterance(id=id, ts=ts, text=text, **d)


def _run_events(utts, cfg):
    state, evs = EngineState(), []
    for u in utts:
        state, ev = evaluate(u, state, cfg)
        if ev is not None:
            evs.append(ev)
    return evs


def _rows(db) -> int:
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM attention_events").fetchone()[0]
    conn.close()
    return n


@pytest.mark.asyncio
async def test_persist_writes_rows_and_no_text_column(tmp_path):
    db = tmp_path / "g.db"
    _make_db(db)
    cfg = AttentionConfig.from_dict(default_config_dict())
    evs = _run_events([_utt(1, 100.0, "what do you think about it?")], cfg)
    assert evs
    c = ShadowStoreConsumer(db, snapshot_id="snapX", config_version=cfg.version)
    for ev in evs:
        c.add(ev)
    assert await c.flush() == len(evs)

    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(attention_events)")]
    rows = conn.execute("SELECT window_ref, snapshot_id, config_version FROM attention_events").fetchall()
    conn.close()
    assert "text" not in cols                 # firewall: no transcript column at all
    assert len(rows) == len(evs)
    wr = json.loads(rows[0][0])
    assert wr["utt_ids"] and wr["snapshot_id"] == "snapX"
    assert rows[0][2] == cfg.version


@pytest.mark.asyncio
async def test_firewall_no_transcript_text_persisted(tmp_path):
    db = tmp_path / "g.db"
    _make_db(db)
    cfg = AttentionConfig.from_dict(default_config_dict())
    secret = "zzsecretphrasezz"
    evs = _run_events([_utt(1, 100.0, f"what do you think? {secret}")], cfg)
    assert evs
    c = ShadowStoreConsumer(db, snapshot_id="snapX", config_version=cfg.version)
    for ev in evs:
        c.add(ev)
    await c.flush()
    conn = sqlite3.connect(db)
    dump = " ".join(str(v) for row in conn.execute("SELECT * FROM attention_events") for v in row)
    conn.close()
    assert secret not in dump  # transcript text NEVER reaches genesis.db


@pytest.mark.asyncio
async def test_idempotent_reflush_same_snapshot_and_config(tmp_path):
    db = tmp_path / "g.db"
    _make_db(db)
    cfg = AttentionConfig.from_dict(default_config_dict())
    evs = _run_events([_utt(1, 100.0, "what do you think?")], cfg)
    for _ in range(2):  # re-running the same snapshot+config must not duplicate rows
        c = ShadowStoreConsumer(db, snapshot_id="s", config_version=cfg.version)
        for ev in evs:
            c.add(ev)
        await c.flush()
    assert _rows(db) == len(evs)  # label-preserving upsert keyed on snapshot:config:utt


@pytest.mark.asyncio
async def test_reflush_preserves_human_label(tmp_path):
    # B1 regression: persist -> a human labels a row -> re-run the SAME snapshot+config
    # (the runner always writes acceptance_signal=NULL) must NOT clobber the label.
    db = tmp_path / "g.db"
    _make_db(db)
    cfg = AttentionConfig.from_dict(default_config_dict())
    evs = _run_events([_utt(1, 100.0, "what do you think?")], cfg)
    assert evs

    async def _persist():
        c = ShadowStoreConsumer(db, snapshot_id="s", config_version=cfg.version)
        for ev in evs:
            c.add(ev)
        await c.flush()

    await _persist()
    conn = sqlite3.connect(db)
    conn.execute("UPDATE attention_events SET acceptance_signal = 'should'")
    conn.commit()
    conn.close()

    await _persist()  # re-run over the same snapshot+config
    conn = sqlite3.connect(db)
    labels = [r[0] for r in conn.execute("SELECT acceptance_signal FROM attention_events")]
    conn.close()
    assert labels and all(v == "should" for v in labels)  # label survived the re-run


@pytest.mark.asyncio
async def test_new_config_version_writes_new_rows(tmp_path):
    db = tmp_path / "g.db"
    _make_db(db)
    utts = [_utt(1, 100.0, "what do you think?")]
    for ver in ("v1", "v2"):
        cfg = AttentionConfig.from_dict({**default_config_dict(), "version": ver})
        evs = _run_events(utts, cfg)
        c = ShadowStoreConsumer(db, snapshot_id="s", config_version=ver)
        for ev in evs:
            c.add(ev)
        await c.flush()
    assert _rows(db) == 2  # different config_version -> distinct rows (labels preserved)
