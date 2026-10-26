"""Derived metadata must be independent of physical input/aggregate ordering."""

import contextlib
import hashlib
import json
from decimal import Decimal

import duckdb
import pyarrow as pa
import pytest

from genesis.transcript_analytics import query, reconcile
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
                    "record_session_id": "s",
                    "context_session_id": "s",
                    "actor_id": '["s",null]',
                    "source_role": "main",
                    "context_conflict": False,
                    "content_hash": f"content-{index}-{fragment}",
                    "payload_hash": f"payload-{index}-{fragment}",
                    "immutable_hash": f"immutable-{index}-{fragment}",
                    "request_id_hash": "no-request",
                    "terminal_hash": f"terminal-{index}",
                    "has_usage": True,
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
                    "context_session_id": "s",
                    "actor_id": '["s",null]',
                    "context_conflict": False,
                    "line_no": index + 3,
                    "kind": "ai-title",
                    "value": f"title-{index:02}",
                    "scrub_failed": False,
                },
                {
                    "source_file": f"source-{index:02}.jsonl",
                    "session_id": "s",
                    "context_session_id": "s",
                    "actor_id": '["s",null]',
                    "context_conflict": False,
                    "line_no": index + 3,
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
    reconcile.install(con)
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
            "source_file": "dated.jsonl",
            "line_no": 1,
            "session_id": "s",
            "context_session_id": "s",
            "actor_id": '["s",null]',
            "context_conflict": False,
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


def _default_row(table, **values):
    row = {field.name: None for field in SCHEMAS[table]}
    row.update(values)
    return row


@pytest.mark.parametrize("terminal", [False, True])
def test_unknown_historical_actor_blocks_executor_ownership(terminal):
    rows = _rows()
    observed = rows["fragments"][1 if terminal else 0]
    rows["fragments"].append(
        {
            **observed,
            "source_file": "historical.jsonl",
            "actor_id": None,
            "line_no": observed["line_no"] + 100,
        }
    )
    with contextlib.closing(_connection(rows)) as con:
        executor, status, reasons = con.execute(
            "SELECT executor_id,attribution_status,attribution_reasons "
            "FROM turns WHERE message_id='m-00'"
        ).fetchone()
        assert (executor, status) == (None, "unresolved")
        assert "missing observed actor" in reasons
        assert con.execute("SELECT usage_available FROM turns WHERE message_id='m-00'").fetchone() == (False,)


@pytest.mark.parametrize("view", ["tool_calls", "agents"])
def test_scrub_failure_survives_across_all_discarded_copies(view):
    rows = _rows()
    actor = '["s",null]'
    if view == "tool_calls":
        rows["tool_calls"] = [
            _default_row(
                "tool_calls",
                source_file="a.jsonl",
                tool_use_id="tool",
                line_no_call=1,
                call_uuid="call-u",
                actor_id=actor,
                context_session_id="s",
                source_role="main",
                context_conflict=False,
                has_result=False,
                scrub_failed=False,
            ),
            _default_row(
                "tool_calls",
                source_file="z.jsonl",
                tool_use_id="tool",
                line_no_call=1,
                call_uuid="call-u",
                actor_id=actor,
                context_session_id="s",
                source_role="main",
                context_conflict=False,
                has_result=False,
                scrub_failed=True,
            ),
        ]
    else:
        rows["agents"] = [
            _default_row(
                "agents",
                source_file="a.jsonl",
                actor_id=actor,
                agent_id="child",
                session_id="s",
                scrub_failed=False,
            ),
            _default_row(
                "agents",
                source_file="z.jsonl",
                actor_id=actor,
                agent_id="child",
                session_id="s",
                scrub_failed=True,
            ),
        ]
    with contextlib.closing(_connection(rows)) as con:
        assert con.execute(f"SELECT scrub_failed FROM {view}").fetchall() == [(True,)]


def test_sessions_preserve_unknown_scrub_state():
    rows = _rows()
    for table in rows.values():
        for row in table:
            row["scrub_failed"] = None
    with contextlib.closing(_connection(rows)) as con:
        assert con.execute("SELECT scrub_failed FROM sessions").fetchall() == [(None,)]
