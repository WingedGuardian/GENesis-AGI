"""Unknown hashes are exclusions; observed contradictions remain conflicts."""

import contextlib

import pytest
from test_deterministic_views import _connection

from genesis.transcript_analytics.extract import TABLES


@pytest.mark.parametrize("table,metric", [("hooks", "hook_content"), ("events", "event_content")])
@pytest.mark.parametrize("hashes,expected", [
    ([None], None), (["a", None], None), (["a", "b", None], True), (["a"], False),
])
def test_content_hash_coverage_has_a_complete_denominator(table, metric, hashes, expected):
    rows = {name: [] for name in TABLES}
    rows[table] = [
        {"source_file": f"copy-{index}.jsonl", "line_no": 1, "uuid": "shared",
         "record_hash": value, "actor_id": '["s",null]', "context_session_id": "s",
         "context_conflict": False}
        for index, value in enumerate(hashes)
    ]
    with contextlib.closing(_connection(rows)) as con:
        assert con.sql(f"SELECT content_conflict FROM {table}").fetchone() == (expected,)
        assert con.sql("SELECT included,excluded FROM metric_coverage WHERE metric=?", params=[metric]).fetchone() == (
            int(expected is False), int(expected is not False),
        )
