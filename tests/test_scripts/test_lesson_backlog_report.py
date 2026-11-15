"""scripts/lesson_backlog_report.py on a synthetic procedural_memory table.

Vectors are built from orthogonal unit directions, so every expected cosine is
known exactly: L1 and D1 nearly coincide, D2 and D3 sit at 0.958, D4 and L2
stand alone.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

from genesis.db.schema import create_all_tables
from genesis.learning.procedural.embedding import EMBEDDING_DIM

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lesson_backlog_report.py"


@pytest.fixture(scope="module")
def rep():
    spec = importlib.util.spec_from_file_location("lesson_backlog_report", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _e(*parts):
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    for idx, weight in parts:
        v[idx] = weight
    return v.astype("<f4").tobytes()


# (id, draft, created_at, embedding); created_at order is the "earlier" order.
_ROWS = [
    ("L1", 0, "2026-01-01T00:00:00+00:00", _e((0, 1.0))),
    ("D1", 1, "2026-01-02T00:00:00+00:00", _e((0, 1.0), (9, 0.05))),
    ("D2", 1, "2026-01-03T00:00:00+00:00", _e((1, 1.0))),
    ("D3", 1, "2026-01-04T00:00:00+00:00", _e((1, 1.0), (2, 0.3))),
    ("D4", 1, "2026-01-05T00:00:00+00:00", _e((5, 1.0))),
    ("L2", 0, "2026-01-06T00:00:00+00:00", _e((6, 1.0))),
    ("NOEMB", 1, "2026-01-07T00:00:00+00:00", None),
]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "genesis.db"

    async def _make():
        import aiosqlite

        async with aiosqlite.connect(path) as db:
            await create_all_tables(db)
            for pid, draft, created, emb in _ROWS:
                await db.execute(
                    "INSERT INTO procedural_memory (id, task_type, principle, steps, tools_used,"
                    " context_tags, created_at, draft, principle_embedding)"
                    " VALUES (?, ?, ?, '[]', '[]', '[]', ?, ?, ?)",
                    (pid, f"type-{pid}", f"principle {pid} | with a pipe", created, draft, emb),
                )
            await db.commit()

    asyncio.run(_make())
    return path


def _by_id(lessons, result, key):
    out = {}
    for i, pid in enumerate(lessons.ids):
        j = int(result[key][i])
        out[pid] = lessons.ids[j] if j >= 0 else None
    return out


def test_nearest_earlier_live_and_alone(rep, db_path):
    lessons = rep.load(db_path)
    assert lessons.skipped == 1 and "NOEMB" not in lessons.ids
    result = rep.analyse(lessons, block=100)
    earlier = _by_id(lessons, result, "nearest_earlier")
    assert earlier["D1"] == "L1" and earlier["D3"] == "D2"
    i = lessons.ids.index("D3")
    assert result["nearest_earlier_cos"][i] == pytest.approx(1 / np.sqrt(1.09), abs=1e-4)
    assert _by_id(lessons, result, "nearest_live")["D1"] == "L1"
    alone = [
        lessons.ids[i]
        for i in np.nonzero(lessons.draft)[0]
        if result["best_cos"][i] < rep.ALONE_COSINE
    ]
    assert alone == ["D4"]


def test_topics_are_threshold_components(rep, db_path):
    lessons = rep.load(db_path)
    topics = rep.analyse(lessons, block=100)["topics"]
    named = sorted(sorted(lessons.ids[i] for i in t) for t in topics)
    assert named == [["D1", "L1"], ["D2", "D3"], ["D4"], ["L2"]]


@pytest.mark.parametrize("block", [1, 2, 3, 100])
def test_block_size_does_not_change_the_result(rep, db_path, block):
    lessons = rep.load(db_path)
    ref = rep.analyse(lessons, block=100)
    got = rep.analyse(lessons, block=block)
    for key in ("nearest_earlier", "nearest_live"):
        assert np.array_equal(ref[key], got[key])
    for key in ("nearest_earlier_cos", "nearest_live_cos", "best_cos"):
        assert np.allclose(ref[key], got[key], equal_nan=True)
    assert ref["topics"] == got["topics"]


def test_block_rows_stays_within_bounds(rep):
    assert rep.block_rows(0) == 1
    assert 1 <= rep.block_rows(5000) <= 5000


def test_main_writes_report_and_csv_and_leaves_the_database_unchanged(rep, db_path, tmp_path):
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    out = tmp_path / "out" / "report.md"
    assert rep.main(["--db", str(db_path), "--out", str(out), "--block", "2"]) == 0
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    md = out.read_text()
    assert "Drafts with no neighbour at or above 0.75: 1 of 4 (25.0%)." in md
    assert "Of the 2 drafts whose nearest earlier lesson is at or above 0.85, 0 share" in md
    assert "1 of 4 drafts have such a live lesson" in md
    assert "left out: 1" in md
    assert "principle L1 \\| with a pipe" in md  # a pipe cannot break the table
    rows = list(csv.DictReader(out.with_suffix(".csv").open()))
    assert [r["draft_id"] for r in rows] == ["D1", "D2", "D3", "D4"]
    d1 = rows[0]
    assert d1["nearest_earlier_id"] == "L1" and d1["nearest_live_id"] == "L1"


def test_a_fenced_database_is_refused_without_opening_it(rep, db_path, tmp_path, monkeypatch):
    import db_admission_check

    monkeypatch.setattr(db_admission_check, "database_is_fenced", lambda p: True)
    opened = []
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: opened.append(a))
    out = tmp_path / "r.md"
    assert rep.main(["--db", str(db_path), "--out", str(out)]) == 2
    assert opened == [] and not out.exists()


def test_a_missing_database_is_reported(rep, tmp_path):
    assert rep.main(["--db", str(tmp_path / "none.db"), "--out", str(tmp_path / "r.md")]) == 2


def test_load_passes_a_read_only_uri(rep, db_path, monkeypatch):
    calls = []
    real = sqlite3.connect

    def spy(*a, **k):
        calls.append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    rep.load(db_path)
    (uri,), kwargs = calls[0]
    assert uri.startswith("file:") and uri.endswith("?mode=ro") and kwargs.get("uri") is True


def _single_table(tmp_path, rows):
    path = tmp_path / "one.db"

    async def _make():
        import aiosqlite

        async with aiosqlite.connect(path) as db:
            await create_all_tables(db)
            for pid, draft, emb in rows:
                await db.execute(
                    "INSERT INTO procedural_memory (id, task_type, principle, steps, tools_used,"
                    " context_tags, created_at, draft, principle_embedding)"
                    " VALUES (?, 't', 'p', '[]', '[]', '[]', '2026-01-01T00:00:00+00:00', ?, ?)",
                    (pid, draft, emb),
                )
            await db.commit()

    asyncio.run(_make())
    return path


def test_a_lone_draft_counts_as_alone(rep, tmp_path):
    path = _single_table(tmp_path, [("D", 1, _e((0, 1.0)))])
    out = tmp_path / "r.md"
    assert rep.main(["--db", str(path), "--out", str(out)]) == 0
    assert "at or above 0.75: 1 of 1 (100.0%)." in out.read_text()


def test_nan_and_zero_vectors_are_left_out_not_poisoning_the_rest(rep, tmp_path):
    nan = np.zeros(EMBEDDING_DIM, dtype="<f4")
    nan[0] = np.nan
    path = _single_table(
        tmp_path,
        [
            ("BAD", 1, nan.tobytes()),
            ("ZERO", 1, _e()),
            ("A", 1, _e((0, 1.0))),
            ("B", 1, _e((0, 1.0), (1, 0.1))),
        ],
    )
    lessons = rep.load(path)
    assert lessons.skipped == 2 and sorted(lessons.ids) == ["A", "B"]
    result = rep.analyse(lessons, block=10)
    assert np.all(result["best_cos"] > 0.9)


@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_block_must_be_a_positive_integer(rep, db_path, tmp_path, value):
    with pytest.raises(SystemExit):
        rep.main(["--db", str(db_path), "--out", str(tmp_path / "r.md"), "--block", value])
