"""Derived metadata must be independent of physical input/aggregate ordering."""

import contextlib
import hashlib
import json
from decimal import Decimal

import duckdb
import pyarrow as pa

from genesis.transcript_analytics import derive, query, store
from genesis.transcript_analytics.extract import TABLES
from genesis.transcript_analytics.schema import SCHEMAS


def _rows():
    tables = {name: [] for name in TABLES}
    for index in range(24):
        for fragment in (1, 2):
            tables["fragments"].append(
                {
                    "source_file": f"source-{index:02}.jsonl",
                    "session_id": "s",
                    "message_id": f"m-{index:02}",
                    "line_no": fragment,
                    "uuid": f"u-{index}-{fragment}",
                    "ts": f"2026-01-01T00:00:{index:02}Z",
                    "entrypoint": f"entry-{fragment}",
                    "cwd": f"cwd-{index}-{fragment}",
                    "git_branch": f"branch-{index}-{fragment}",
                    "effort": f"effort-{fragment}",
                    "attribution_skill": f"skill-{fragment}",
                    "attribution_mcp_server": f"server-{fragment}",
                    "attribution_mcp_tool": f"tool-{fragment}",
                    "version": f"v-{fragment}",
                    "model": f"model-{index}",
                    "stop_reason": "end_turn" if fragment == 2 else None,
                    "output_tokens": 2 if fragment == 2 else 1,
                    "n_tool_use": 0,
                    "is_sidechain": False,
                    "is_api_error": False,
                    "scrub_failed": False,
                }
            )
        tables["session_meta"].extend(
            [
                {
                    "source_file": f"source-{index:02}.jsonl",
                    "session_id": "s",
                    "kind": "ai-title",
                    "value": f"title-{index:02}",
                    "scrub_failed": False,
                },
                {
                    "source_file": f"source-{index:02}.jsonl",
                    "session_id": "s",
                    "kind": "pr-link",
                    "value": str(index),
                    "pr_repository": f"org/repo-{index % 3}",
                    "scrub_failed": False,
                },
            ]
        )
    return tables


def _connection(rows, reverse=False):
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET preserve_insertion_order=false")
    for name in TABLES:
        source = list(reversed(rows[name])) if reverse else rows[name]
        con.register(f"raw_{name}", pa.Table.from_pylist(source, schema=SCHEMAS[name]))
    con.execute(query._VIEWS)
    return con


def test_all_derived_metadata_has_stable_input_order_and_tie_rules():
    rows = _rows()
    with (
        contextlib.closing(_connection(rows)) as normal,
        contextlib.closing(_connection(rows, True)) as reversed_con,
    ):
        for view in query._DERIVED:
            first = normal.execute(f"SELECT * FROM {view} ORDER BY ALL").fetchall()
            for connection in (normal, reversed_con):
                for _ in range(3):
                    assert (
                        connection.execute(f"SELECT * FROM {view} ORDER BY ALL").fetchall() == first
                    ), view
        turns = normal.execute(
            "SELECT entrypoint,cwd,git_branch,effort,version,output_tokens FROM turns ORDER BY message_id"
        ).fetchall()
        assert turns[0] == ("entry-1", "cwd-0-1", "branch-0-1", "effort-1", "v-1", 2)
        session = normal.execute(
            "SELECT cwd,git_branch,ai_title,models,pr_numbers,pr_links FROM sessions"
        ).fetchone()
        assert session[:3] == ("cwd-0-1", "branch-0-1", "title-23")
        assert session[3] == sorted(session[3])
        assert session[4] == sorted(session[4])
        assert session[5] == sorted(
            session[5], key=lambda value: (value["repository"], value["number"])
        )


def test_metadata_selection_keeps_first_nonnull_and_latest_title_timestamp():
    rows = _rows()
    rows["fragments"][0]["entrypoint"] = None
    rows["fragments"][0]["cwd"] = None
    rows["fragments"][0]["git_branch"] = None
    rows["session_meta"].append(
        {
            "session_id": "s",
            "kind": "ai-title",
            "value": "dated",
            "ts": "2026-02-01T00:00:00Z",
            "scrub_failed": False,
        }
    )
    with contextlib.closing(_connection(rows, True)) as con:
        assert con.execute(
            "SELECT entrypoint,cwd,git_branch FROM turns WHERE message_id='m-00'"
        ).fetchone() == ("entry-2", "cwd-0-2", "branch-0-2")
        assert con.execute("SELECT ai_title FROM sessions").fetchone() == ("dated",)


def _verification_digest(con, view):
    # Same serialized-row contract as PR3 verify._digest, available without
    # importing a module that the standalone store PR does not provide.
    digest = hashlib.sha256()
    rows = con.execute(f"SELECT * FROM {view} ORDER BY ALL").fetchall()
    for row in rows:
        digest.update(json.dumps(row, default=str, ensure_ascii=True).encode() + b"\n")
    return len(rows), digest.hexdigest()


def test_unchanged_corpus_parallel_live_and_snapshot_verification_hashes(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    for index in range(24):
        records = []
        for fragment in range(8):
            records.append(
                {
                    "type": "assistant",
                    "sessionId": "s",
                    "uuid": f"{index}-{fragment}",
                    "timestamp": f"2026-01-01T00:00:{index:02}Z",
                    "cwd": f"cwd-{index}",
                    "gitBranch": f"branch-{index}",
                    "message": {
                        "id": f"message-{index}-{fragment}",
                        "model": f"model-{index}",
                        "stop_reason": "end_turn",
                        "usage": {"output_tokens": fragment},
                        "content": [],
                    },
                }
            )
        records.extend(
            [
                {"type": "ai-title", "sessionId": "s", "aiTitle": f"title-{index:02}"},
                {
                    "type": "pr-link",
                    "sessionId": "s",
                    "prNumber": index,
                    "prRepository": f"org/repo-{index % 3}",
                },
            ]
        )
        (projects / f"source-{index:02}.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )
    assert store.ingest(projects, data)["rebuilt"] == 24
    derive.build(data)
    with (
        contextlib.closing(query.connect(data)) as snapshot,
        contextlib.closing(query.connect(data, threads=4, live=True)) as live,
    ):
        expected = {view: _verification_digest(snapshot, view) for view in query._DERIVED}
        for view in query._DERIVED:
            actual = _verification_digest(live, view)
            if actual != expected[view]:
                a = snapshot.execute(f"SELECT * FROM {view} ORDER BY ALL").fetchone()
                b = live.execute(f"SELECT * FROM {view} ORDER BY ALL").fetchone()
                detail = [
                    (type(x).__name__, str(x), type(y).__name__, str(y))
                    for x, y in zip(a, b, strict=True)
                    if type(x) is not type(y) or x != y
                ]
            else:
                detail = []
            assert actual == expected[view], (view, detail)
        for _ in range(8):
            assert _verification_digest(live, "sessions") == expected["sessions"]
            assert _verification_digest(live, "turns") == expected["turns"]


def test_sum_population_has_exact_decimal_parquet_roundtrip(tmp_path):
    rows = _rows()
    maximum = 2**63 - 1
    # Distinct turns preserve the last-fragment usage contract while exercising
    # sums beyond both binary64 precision and the signed-int64 range.
    for fragment in rows["fragments"]:
        for name in ("output_tokens", "input_tokens", "cache_read", "cache_create", "n_tool_use"):
            fragment[name] = maximum
    with contextlib.closing(_connection(rows)) as con:
        for view, columns, expected in (
            ("turns", ["n_tool_use"], maximum * 2),
            (
                "sessions",
                ["output_tokens", "input_tokens", "cache_read", "cache_create"],
                maximum * 24,
            ),
        ):
            path = tmp_path / f"{view}.parquet"
            con.execute(f"COPY (SELECT * FROM {view}) TO '{path}' (FORMAT PARQUET)")
            live = con.execute(f"SELECT {','.join(columns)} FROM {view} ORDER BY ALL").fetchall()
            stored = con.execute(
                f"SELECT {','.join(columns)} FROM read_parquet('{path}') ORDER BY ALL"
            ).fetchall()
            assert live == stored
            assert all(
                type(value) is Decimal and value == expected for row in live for value in row
            )
        for fragment in rows["fragments"]:
            for name in (
                "output_tokens",
                "input_tokens",
                "cache_read",
                "cache_create",
                "n_tool_use",
            ):
                fragment[name] = None
    with contextlib.closing(_connection(rows)) as con:
        assert con.execute(
            "SELECT output_tokens,input_tokens,cache_read,cache_create FROM sessions"
        ).fetchone() == (None, None, None, None)
        assert all(row == (None,) for row in con.execute("SELECT n_tool_use FROM turns").fetchall())
