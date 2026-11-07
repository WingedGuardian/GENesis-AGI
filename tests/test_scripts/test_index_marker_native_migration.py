"""Exercise the real schema rebuild and every executed failure boundary."""

import sqlite3
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401


def _old(db, queue, populated):
    db.execute("PRAGMA foreign_keys=ON")
    for statement in queue._legs._SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("CREATE TABLE repo_state(hash TEXT PRIMARY KEY,last_full REAL,full_backoff REAL)")
    db.execute("CREATE TABLE queue_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.execute("INSERT INTO queue_meta VALUES('automatic_protocol_ready','0')")
    data = ("a" * 16, "/literal'\n雪", "cbm", 2, "claim", "b" * 32, "cbm", "full", 10, 4, 0, "[]", "generated")
    columns = "hash,repo_path,provider,protocol,entry_kind,claim_id,original_tools,mode,requested_at,attempts,legacy_unqualified,legacy_shared_attempts,nonce_origin"
    db.execute(f"INSERT INTO executions({columns}) VALUES({','.join('?' for _ in data)})", data)
    db.execute("UPDATE executions SET execution_seq=99,legacy_raw=?,conflict_raw=?", (b"\x00\xff", b"old raw"))
    db.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='executions'")
    if populated:
        db.execute("INSERT INTO inflight(hash,repo_path,tools,provider,protocol,mode,requested_at,attempts,claim_id,execution_seq,legacy_unqualified,legacy_shared_attempts) "
                   "VALUES(?,?,'cbm','cbm',2,'full',10,4,?,99,0,'[]')", (data[0], data[1], data[5]))
    else:
        db.execute("DELETE FROM executions")
    db.execute("PRAGMA user_version=3")
    db.commit()


class _Boundary:
    def __init__(self, db, fail):
        self.db, self.fail, self.count = db, fail, 0

    def execute(self, sql, *args):
        self.count += 1
        if self.count == self.fail:
            raise OSError("injected migration boundary")
        return self.db.execute(sql, *args)


@pytest.mark.parametrize("populated", [False, True])
def test_every_real_upgrade_boundary_rolls_back_logical_schema_and_high_water(queue, populated):
    with closing(sqlite3.connect(":memory:")) as db:
        _old(db, queue, populated)
        original = list(db.iterdump())
        boundary = 1
        while True:
            wrapper = _Boundary(db, boundary)
            try:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    queue._native.upgrade(wrapper, queue._legs._SCHEMA)
                    db.execute("PRAGMA user_version=4")
            except OSError:
                assert list(db.iterdump()) == original
                assert db.execute("PRAGMA foreign_key_check").fetchall() == []
                boundary += 1
                continue
            assert wrapper.count < boundary and boundary > 10
            assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='executions'").fetchone() == (100,)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            if populated:
                assert db.execute("SELECT execution_seq,legacy_raw,conflict_raw FROM executions").fetchone() == (99, b"\x00\xff", b"old raw")
                assert db.execute("SELECT execution_seq FROM inflight").fetchone() == (99,)
            break
