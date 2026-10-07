"""Materialized view snapshots: built after an ingest that changed something,
swapped in atomically via a ``current`` symlink, used only when their views
version matches the code, and identical to the live views."""

import json
import os

import pytest

from genesis.transcript_analytics import derive, query, store

SID = "11111111-2222-3333-4444-555555555555"


def _asst(mid, ts, out, stop="end_turn", uuid=None, tools=()):
    blocks = [
        {"type": "tool_use", "id": t, "name": "Bash", "input": {"command": "ls"}} for t in tools
    ] or [{"type": "text", "text": "x"}]
    return {
        "type": "assistant",
        "uuid": uuid or f"{mid}-{ts}",
        "sessionId": SID,
        "timestamp": ts,
        "message": {
            "id": mid,
            "model": "m",
            "stop_reason": stop,
            "content": blocks,
            "usage": {"input_tokens": 1, "output_tokens": out},
        },
    }


def _res(tid, ts, content="ok", is_error=None):
    b = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error is not None:
        b["is_error"] = is_error
    return {
        "type": "user",
        "uuid": f"r-{tid}",
        "sessionId": SID,
        "timestamp": ts,
        "message": {"role": "user", "content": [b]},
    }


@pytest.fixture
def built(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    f = projects / "p" / f"{SID}.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text(
        "".join(
            json.dumps(r) + "\n"
            for r in [
                _asst("m1", "2026-10-01T00:00:00Z", 5, stop="tool_use", tools=["t1"]),
                _res("t1", "2026-10-01T00:00:01Z", content="Exit code 2\nx", is_error=True),
                _asst("m2", "2026-10-01T00:00:02Z", 7),
            ]
        )
    )
    store.ingest(projects, data)
    return projects, data, f


def _view_sql(con, name):
    return con.sql("select sql from duckdb_views() where view_name = ?", params=[name]).fetchone()[
        0
    ]


def test_derive_builds_snapshot_and_connect_uses_it(built):
    projects, data, _ = built
    info = derive.build(data)
    cur = data / "derived" / "current"
    assert (
        cur.is_symlink() and (cur / "turns.parquet").exists() and (cur / "MANIFEST.json").exists()
    )
    assert json.loads((cur / "MANIFEST.json").read_text())["views_version"] == query.VIEWS_VERSION
    assert info["views"] == sorted(derive.DERIVED_VIEWS)
    con = query.connect(data)
    assert "derived" in _view_sql(con, "turns")
    assert "derived" not in _view_sql(query.connect(data, live=True), "turns")


def test_snapshot_equals_live_views(built):
    projects, data, _ = built
    derive.build(data)
    snap, live = query.connect(data), query.connect(data, live=True)
    for v in derive.DERIVED_VIEWS:
        cols = ", ".join(c for c in live.sql(f"select * from {v} limit 0").columns)
        a = snap.sql(f"select {cols} from {v} order by all").fetchall()
        b = live.sql(f"select {cols} from {v} order by all").fetchall()
        assert a == b, v


def test_stale_views_version_falls_back_to_live(built, monkeypatch):
    projects, data, _ = built
    derive.build(data)
    monkeypatch.setattr(query, "VIEWS_VERSION", "999")
    assert "derived" not in _view_sql(query.connect(data), "turns")


def test_swap_keeps_previous_snapshot_and_drops_older(built):
    projects, data, _ = built
    first = derive.build(data)["dir"]
    second = derive.build(data)["dir"]
    third = derive.build(data)["dir"]
    remaining = sorted(
        p.name for p in (data / "derived").iterdir() if p.name not in ("current", ".staging")
    )
    assert remaining == sorted({os.path.basename(second), os.path.basename(third)})
    assert os.path.basename(first) not in remaining
    assert os.readlink(data / "derived" / "current") == os.path.basename(third)




# Re-audit fixes (SF-1..SF-5) -------------------------------------------------
def test_reaudit_sf1_snapshot_goes_stale_when_inputs_change_or_prune_runs(built):
    projects, data, f = built
    derive.build(data)
    assert derive.is_current(data)
    with open(f, "a") as fh:
        fh.write(json.dumps(_asst("m9", "2026-10-01T00:00:09Z", 1)) + "\n")
    store.ingest(projects, data)  # rebuilds a source but does not derive
    assert not derive.is_current(data)
    derive.build(data)
    assert derive.is_current(data)


def test_reaudit_sf2_prune_date_is_normalized(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    gone, keep = projects / "p" / "gone.jsonl", projects / "p" / "keep.jsonl"
    gone.parent.mkdir(parents=True)
    gone.write_text(json.dumps(_asst("g1", "2026-09-01T00:00:00Z", 1)) + "\n")
    keep.write_text(json.dumps(_asst("k1", "2026-09-01T00:00:00Z", 1)) + "\n")
    store.ingest(projects, data)
    gone.unlink()
    assert (
        store.prune(data, before="20260101", projects=projects) == 0
    )  # 2026-09-01 is NOT before 2026-01-01


def test_reaudit_sf3_derive_on_empty_store_is_skipped_not_fatal(tmp_path):
    (tmp_path / "data").mkdir()
    assert derive.build(tmp_path / "data").get("skipped") == "empty store"




def test_reaudit_sf4_build_sweeps_leftover_staging_and_temp_links(built):
    projects, data, _ = built
    root = data / "derived"
    (root / ".staging" / "dead").mkdir(parents=True)
    (root / ".staging" / "dead" / "x.parquet").write_bytes(b"x")
    os.symlink("nowhere", root / ".current.424242")
    derive.build(data)
    assert list((root / ".staging").iterdir()) == []
    assert not (root / ".current.424242").is_symlink()


def test_reaudit_sf5_scrub_version_matches_the_loaded_code(tmp_path, monkeypatch):
    import importlib

    from genesis.transcript_analytics import scrub

    repo = tmp_path / "repo" / "scripts" / "hooks"
    repo.mkdir(parents=True)
    (repo / "secret_scrub.py").write_text("def scrub(t):\n    return t.replace('S', '*')\n")
    monkeypatch.setenv("GENESIS_REPO", str(tmp_path / "repo"))
    mod = importlib.reload(scrub)
    try:
        v = mod.version()
        (repo / "secret_scrub.py").write_text("def scrub(t):\n    return t\n")  # changes after load
        assert mod.version() == v  # the stamp describes the code actually in use
    finally:
        monkeypatch.delenv("GENESIS_REPO")
        importlib.reload(scrub)


def test_failed_derive_retains_stale_snapshot_and_original_provenance(built, monkeypatch, tmp_path):
    projects, data, source = built
    derive.build(data)
    first = (data / "derived/current").resolve()
    extra = projects / "p/new.jsonl"
    extra.write_text(json.dumps(_asst("new-message", "2026-10-02T00:00:00Z", 99)) + "\n")
    store.ingest(projects, data)
    real_connect = query.connect
    monkeypatch.setattr(
        query, "connect", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("build fails"))
    )
    with pytest.raises(RuntimeError):
        derive.build(data)
    assert (data / "derived/current").resolve() == first
    monkeypatch.setattr(query, "connect", real_connect)
    manifest = tmp_path / "report.json"
    _, rows = query.run_query(data, "SELECT count(*) FROM turns", manifest_path=manifest)
    assert rows == [(2,)]
    recorded = json.loads(manifest.read_text())
    assert recorded["snapshot_current"] is False
    assert recorded["coverage"]["included"] == 1
    assert recorded["source_references"] == ["p/" + source.name]
    assert query.run_query(data, "SELECT count(*) FROM turns", live=True)[1] == [(3,)]
