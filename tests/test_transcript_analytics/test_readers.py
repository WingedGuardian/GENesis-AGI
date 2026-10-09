"""Snapshot readers lease publication, independently of collection."""

import json
import threading

from genesis.transcript_analytics import derive, locks, query, store


def test_snapshot_query_runs_while_writer_held_and_blocks_publication(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    (projects / "one.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "one",
                    "model": "example",
                    "content": [],
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n"
    )
    store.ingest(projects, data)
    derive.build(data)
    with store._locked(store.DEFAULT_LOCK):
        assert query.run_query(data, "SELECT count(*) FROM turns")[1] == [(1,)]
    started, acquired = threading.Event(), threading.Event()

    def publish():
        started.set()
        with locks.publication(exclusive=True):
            acquired.set()

    with query.read_connection(data) as con:
        worker = threading.Thread(target=publish)
        worker.start()
        assert started.wait(1)
        assert not acquired.wait(0.1)
        assert con.execute("SELECT count(*) FROM turns").fetchone() == (1,)
    worker.join(2)
    assert acquired.is_set() and not worker.is_alive()


def test_restore_epoch_invalidates_snapshots_for_any_data_directory(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "custom-analytics"
    projects.mkdir()
    (projects / "one.jsonl").write_text('{"type":"progress"}\n')
    store.ingest(projects, data)
    derive.build(data)
    assert query.snapshot_compatible(data)
    epoch = locks.PUBLICATION_LOCK.parent / "transcript-analytics-restore-epoch"
    epoch.write_text("restore-started-before-first-mutation")
    assert not query.snapshot_compatible(data)
    derive.build(data)
    assert query.snapshot_compatible(data)
