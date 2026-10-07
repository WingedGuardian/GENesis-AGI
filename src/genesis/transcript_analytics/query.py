"""DuckDB views over compatible source generations or a published snapshot.

connect is an internal builder primitive: callers must hold the writer lock.
Public query execution holds writer and publication locks until fetching ends.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import duckdb

from genesis.transcript_analytics import store
from genesis.transcript_analytics.extract import TABLES
from genesis.transcript_analytics.locks import publication, restore_epoch

VIEWS_VERSION = "8"
_DERIVED = ("tool_calls", "turns", "hooks", "events", "session_meta", "agents", "sessions")


def snapshot_manifest(data: Path) -> dict:
    try:
        value = json.loads((data / "derived/current/MANIFEST.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def snapshot_compatible(data: Path) -> bool:
    cur = data / "derived/current"
    manifest = snapshot_manifest(data)
    return (
        manifest.get("views_version") == VIEWS_VERSION
        and manifest.get("restore_epoch") == restore_epoch()
        and manifest.get("semantics")
        == {k.decode(): v.decode() for k, v in store.semantics().items()}
        and all((cur / f"{v}.parquet").is_file() for v in _DERIVED)
    )


def derived_current(data: Path) -> bool:
    return (
        snapshot_compatible(data)
        and snapshot_manifest(data).get("input_fp") == data.stat().st_mtime_ns
    )


def connect(data: Path, *, memory_limit=None, threads=None, live=False):
    """Internal unlocked connection; only compatible sources enter live views."""
    import pyarrow as pa

    from genesis.transcript_analytics.schema import SCHEMAS

    # CLI has verified this scope against kernel limits before importing us.
    budget = os.environ.get("GENESIS_TRANSCRIPT_RESOURCE_CHILD")
    ram, cpu = (float(v) for v in budget.split(",")) if budget else (3_000_000_000, 200)
    memory_limit = memory_limit or f"{max(1, int(ram * 0.5))}B"
    threads = threads or max(1, int(cpu / 100))
    con = duckdb.connect()
    try:
        tmp = Path(os.environ.get("TA_DUCKDB_TMP", Path.home() / "tmp/transcript-analytics/duckdb"))
        tmp.mkdir(parents=True, exist_ok=True)
        con.execute("SET memory_limit = ?", [memory_limit])
        con.execute(f"SET threads={int(threads)}")
        con.execute("SET temp_directory = ?", [str(tmp)])
        con.execute("SET preserve_insertion_order=false")
        con.execute("SET autoinstall_known_extensions=false")
        con.execute("SET autoload_known_extensions=false")
        if not live and snapshot_compatible(data):
            snap = (data / "derived/current").resolve()
            for view in _DERIVED:
                path = str(snap / f"{view}.parquet").replace("'", "''")
                con.execute(f"CREATE VIEW {view} AS SELECT * FROM read_parquet('{path}')")  # noqa: S608 — fixed view names, escaped local path
        else:
            keys, _excluded = store.compatible_sources(data)
            for table in TABLES:
                files = [str(store._table_path(data, table, key)) for key in keys]
                if files:
                    con.from_parquet(files, union_by_name=False).create_view(f"raw_{table}")
                else:
                    con.register(f"_empty_{table}", pa.Table.from_pylist([], schema=SCHEMAS[table]))
                    con.execute(f"CREATE VIEW raw_{table} AS SELECT * FROM _empty_{table}")  # noqa: S608 — fixed table names
            con.execute(_VIEWS)
        con.execute("SET lock_configuration=true")
        return con
    except BaseException:
        con.close()
        raise


@contextlib.contextmanager
def read_connection(data, *, live=False):
    if not live:
        with publication():
            if snapshot_compatible(data):
                with contextlib.closing(connect(data)) as con:
                    yield con
                return
    # Release publication before acquiring writer: same order as restore/build.
    with (
        store._locked(store.DEFAULT_LOCK),
        publication(),
        contextlib.closing(connect(data, live=True)) as con,
    ):
        yield con


def _execute_query(con, sql, params, snapshot):
    try:
        return con.sql(sql, params=params)
    except duckdb.CatalogException as exc:
        if snapshot and "raw_" in str(exc):
            raise ValueError("raw_* views require --live") from exc
        raise


def _scrub_labels(sql, window, baseline):
    from . import scrub

    values = [scrub.scrub_text(value) for value in (sql, window, baseline)]
    return {
        "query": values[0][0],
        "window": values[1][0],
        "deployment_baseline": values[2][0],
        "scrub_failed": any(v[1] for v in values),
    }


def run_query(
    data: Path, sql: str, params=None, *, live=False, manifest_path=None, window=None, baseline=None
):
    """Read a consistent generation; stale compatible snapshots remain usable."""
    with read_connection(data, live=live) as con:
        snapshot = snapshot_manifest(data) if not live and snapshot_compatible(data) else None
        if snapshot:
            coverage, inventory = snapshot.get("coverage", {}), snapshot.get("inventory", {})
            stale = not derived_current(data)
        else:
            keys, excluded = store.compatible_sources(data)
            coverage, inventory = (
                {"included": len(keys), "excluded": excluded},
                store.inventory(data),
            )
            stale = False
        print(
            json.dumps(
                {
                    "mode": "snapshot" if snapshot else "live",
                    "stale": stale,
                    "coverage": coverage,
                    "inventory": inventory,
                    "latest_collection": store.inventory(data),
                }
            ),
            file=sys.stderr,
        )
        rel = _execute_query(con, sql, params, snapshot)
        result = (rel.columns, rel.fetchall()) if rel is not None else ([], [])
        if manifest_path:
            from .provenance import write_manifest

            snapshot = snapshot_manifest(data) if not live and snapshot_compatible(data) else None
            keys, excluded = ([], []) if snapshot else store.compatible_sources(data)
            write_manifest(
                manifest_path,
                {
                    **_scrub_labels(sql, window, baseline),
                    "snapshot": snapshot,
                    "snapshot_current": derived_current(data),
                    "inventory": inventory,
                    "live": live,
                    "versions": {k.decode(): v.decode() for k, v in store.semantics().items()},
                    "coverage": snapshot["coverage"]
                    if snapshot
                    else {"included": len(keys), "excluded": excluded},
                    "denominators": {
                        v: con.execute(f"SELECT count(*) FROM {v}").fetchone()[0]  # noqa: S608 — fixed view names
                        for v in ("tool_calls", "turns", "sessions")
                    },
                    "result_rows": len(result[1]),
                    "source_references": [s["ta.source"] for s in snapshot["sources"]]
                    if snapshot
                    else [
                        store.source_identity(
                            os.fsdecode(store.source_metadata(data, key)[0].get(b"ta.source", b""))
                        )
                        for key in keys
                    ],
                },
            )
        return result


_VIEWS = """
CREATE OR REPLACE VIEW file_span AS
SELECT source_file, min(ts) AS file_first, max(ts) AS file_last FROM raw_fragments GROUP BY source_file;

-- A record repeated inside ONE file counts once (review SF-5: 630 such pairs).
CREATE OR REPLACE VIEW fragments AS
SELECT * FROM raw_fragments
QUALIFY row_number() OVER (PARTITION BY source_file, uuid IS NULL, coalesce(uuid, line_no::VARCHAR) ORDER BY line_no) = 1;

-- Which file's copy of a message is the original. Copies arise two ways, both
-- measured: a resumed session re-writes earlier records WITH their original
-- timestamps, and a spawned child agent's file begins with a copy of the ONE
-- parent fragment that spawned it. The original holds the most fragments of
-- the message; among equally complete copies, the file that started earliest,
-- then ended earliest (the original stops when the resumed one continues).
CREATE OR REPLACE VIEW message_copy AS
SELECT message_id, source_file FROM (
  SELECT f.message_id, f.source_file, count(*) AS n, min(s.file_first) AS file_first,
         max(s.file_last) AS file_last
  FROM fragments f JOIN file_span s USING (source_file)
  WHERE f.message_id IS NOT NULL
  GROUP BY f.message_id, f.source_file)
QUALIFY row_number() OVER (PARTITION BY message_id
                           ORDER BY n DESC, file_first NULLS LAST, file_last NULLS LAST, source_file) = 1;

-- One row per tool_use_id, ASSEMBLED: call columns from the call row in the
-- message's original file (else the earliest file), result columns from the
-- result row in that same file (else the earliest file holding one). Picking a
-- single row let a result-only orphan in another file replace the real call.
CREATE OR REPLACE VIEW tool_calls AS
WITH calls AS (
  SELECT c.* FROM raw_tool_calls c
  LEFT JOIN message_copy m ON m.message_id = c.message_id
  LEFT JOIN file_span s ON s.source_file = c.source_file
  WHERE c.line_no_call IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY c.tool_use_id
                             ORDER BY (c.source_file = m.source_file) DESC NULLS LAST,
                                      s.file_first NULLS LAST, s.file_last NULLS LAST, c.source_file,
                                      c.line_no_call) = 1),
results AS (
  SELECT r.* FROM raw_tool_calls r
  LEFT JOIN calls k ON k.tool_use_id = r.tool_use_id
  LEFT JOIN file_span s ON s.source_file = r.source_file
  WHERE r.has_result
  QUALIFY row_number() OVER (PARTITION BY r.tool_use_id
                             ORDER BY (r.source_file = k.source_file) DESC NULLS LAST,
                                      s.file_first NULLS LAST, s.file_last NULLS LAST, r.source_file,
                                      r.line_no_result) = 1)
SELECT
  coalesce(k.source_file, r.source_file) AS source_file, coalesce(k.session_id, r.session_id) AS session_id,
  coalesce(k.agent_id, r.agent_id) AS agent_id, coalesce(k.tool_use_id, r.tool_use_id) AS tool_use_id,
  k.message_id, k.line_no_call, r.line_no_result, k.ts_call, r.ts_result,
  coalesce(CAST(date_diff('millisecond', try_cast(k.ts_call AS TIMESTAMPTZ), try_cast(r.ts_result AS TIMESTAMPTZ))
                AS BIGINT), CASE WHEN k.source_file = r.source_file THEN r.latency_ms END) AS latency_ms,
  k.tool, k.mcp_server, k.mcp_tool, k.command, k.file_path, k.subagent_type, k.skill_arg, k.description,
  k.input_len, coalesce(k.is_sidechain, r.is_sidechain) AS is_sidechain, k.attribution_skill, k.entrypoint,
  coalesce(k.cwd, r.cwd) AS cwd, coalesce(k.git_branch, r.git_branch) AS git_branch,
  coalesce(r.has_result, false) AS has_result, r.is_error, r.error_source, r.error_class, r.error_text,
  r.tool_use_result_text, r.result_len, r.exit_code, r.interrupted, r.denial_kind,
  r.hook_event, r.hook_tool, r.hook_command, r.hook_script,
  coalesce(k.scrub_failed, false) OR coalesce(r.scrub_failed, false) AS scrub_failed,
  r.source_file AS result_source_file
FROM calls k FULL OUTER JOIN results r ON r.tool_use_id = k.tool_use_id;

CREATE OR REPLACE VIEW turns AS
WITH selected AS (
 SELECT fr.* FROM fragments fr JOIN message_copy USING (message_id, source_file)
 UNION ALL SELECT * FROM fragments WHERE message_id IS NULL
 QUALIFY row_number() OVER (PARTITION BY uuid IS NULL, coalesce(uuid, source_file || ':' || line_no)
 ORDER BY source_file, line_no) = 1
), f AS (SELECT *, CASE WHEN message_id IS NOT NULL THEN 'provider:' || message_id
 ELSE coalesce('uuid:' || uuid, 'local:' || source_file || ':' || line_no) END AS turn_key FROM selected),
-- Metadata selects the first non-null value in source record order.
g AS (
  SELECT turn_key, any_value(message_id ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, message_id ASC NULLS LAST) AS message_id,
         any_value(session_id ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, session_id ASC NULLS LAST) AS session_id, any_value(agent_id ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, agent_id ASC NULLS LAST) AS agent_id,
         any_value(source_file ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, source_file ASC NULLS LAST) AS source_file, min(ts) AS ts, max(ts) AS ts_last,
         count(*) AS n_records, bool_or(stop_reason IS NOT NULL) AS usage_available,
         CAST(sum(n_tool_use) AS DECIMAL(38,0)) AS n_tool_use, bool_or(is_sidechain) AS is_sidechain,
         any_value(entrypoint ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, entrypoint ASC NULLS LAST) AS entrypoint, any_value(attribution_skill ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, attribution_skill ASC NULLS LAST) AS attribution_skill,
         any_value(attribution_mcp_server ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, attribution_mcp_server ASC NULLS LAST) AS attribution_mcp_server,
         any_value(attribution_mcp_tool ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, attribution_mcp_tool ASC NULLS LAST) AS attribution_mcp_tool, any_value(effort ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, effort ASC NULLS LAST) AS effort,
         bool_or(is_api_error) AS is_api_error, bool_or(scrub_failed) AS scrub_failed, any_value(cwd ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, cwd ASC NULLS LAST) AS cwd, any_value(git_branch ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, git_branch ASC NULLS LAST) AS git_branch,
         any_value(version ORDER BY line_no ASC NULLS LAST, source_file ASC NULLS LAST, version ASC NULLS LAST) AS version
  FROM f GROUP BY turn_key),
-- The LAST fragment in line order supplies usage, NULLs included: arg_max skips
-- NULL values, which would mix columns from different fragments (review N-2).
l AS (
  SELECT * EXCLUDE (rn) FROM (
    SELECT turn_key, stop_reason, model, request_id, input_tokens, output_tokens, cache_read,
           cache_create, cache_create_5m, cache_create_1h, thinking_tokens,
           row_number() OVER (PARTITION BY turn_key ORDER BY line_no DESC) AS rn
    FROM f) WHERE rn = 1)
SELECT g.* EXCLUDE (turn_key), l.stop_reason, l.model, l.request_id,
       CASE WHEN g.usage_available THEN l.input_tokens END AS input_tokens,
       CASE WHEN g.usage_available THEN l.output_tokens END AS output_tokens,
       CASE WHEN g.usage_available THEN l.cache_read END AS cache_read,
       CASE WHEN g.usage_available THEN l.cache_create END AS cache_create,
       CASE WHEN g.usage_available THEN l.cache_create_5m END AS cache_create_5m,
       CASE WHEN g.usage_available THEN l.cache_create_1h END AS cache_create_1h,
       CASE WHEN g.usage_available THEN l.thinking_tokens END AS thinking_tokens
FROM g JOIN l USING (turn_key);

CREATE OR REPLACE VIEW hooks AS
SELECT * EXCLUDE (rn) FROM (
  SELECT *, row_number() OVER (PARTITION BY uuid IS NULL, coalesce(uuid, source_file || ':' || line_no)
                               ORDER BY source_file, line_no) AS rn FROM raw_hooks) WHERE rn = 1;

CREATE OR REPLACE VIEW events AS
SELECT * EXCLUDE (rn) FROM (
  SELECT *, row_number() OVER (PARTITION BY uuid IS NULL, coalesce(uuid, source_file || ':' || line_no)
                               ORDER BY source_file, line_no) AS rn FROM raw_events) WHERE rn = 1;

CREATE OR REPLACE VIEW session_meta AS
SELECT session_id, kind, value, pr_repository, min(ts) AS ts, bool_or(scrub_failed) AS scrub_failed
FROM raw_session_meta GROUP BY session_id, kind, value, pr_repository;

CREATE OR REPLACE VIEW agents AS
SELECT * EXCLUDE (rn) FROM (
  SELECT *, row_number() OVER (PARTITION BY agent_id ORDER BY source_file) AS rn FROM raw_agents) WHERE rn = 1;

CREATE OR REPLACE VIEW sessions AS
WITH t AS (
  SELECT session_id, min(ts) AS first_ts, max(ts_last) AS last_ts, count(*) AS n_turns,
         count(*) FILTER (WHERE agent_id IS NULL) AS n_main_turns, count(DISTINCT agent_id) AS n_subagents,
         CAST(sum(output_tokens) AS DECIMAL(38,0)) AS output_tokens, CAST(sum(input_tokens) AS DECIMAL(38,0)) AS input_tokens,
         CAST(sum(cache_read) AS DECIMAL(38,0)) AS cache_read, CAST(sum(cache_create) AS DECIMAL(38,0)) AS cache_create,
         count(*) FILTER (WHERE NOT usage_available) AS n_turns_no_usage,
         list(DISTINCT model ORDER BY model ASC NULLS LAST) AS models,
         any_value(cwd ORDER BY ts ASC NULLS LAST, source_file ASC NULLS LAST, message_id ASC NULLS LAST, cwd ASC NULLS LAST) AS cwd,
         any_value(git_branch ORDER BY ts ASC NULLS LAST, source_file ASC NULLS LAST, message_id ASC NULLS LAST, git_branch ASC NULLS LAST) AS git_branch
  FROM turns GROUP BY session_id),
c AS (
  SELECT session_id, count(*) AS n_tool_calls,
         count(*) FILTER (WHERE error_source = 'flag') AS n_errors_flagged,
         count(*) FILTER (WHERE error_source = 'text') AS n_errors_text,
         count(*) FILTER (WHERE error_class = 'hook_block') AS n_hook_blocks
  FROM tool_calls GROUP BY session_id),
m AS (
  -- Untimed titles retain the empty timestamp key; equal keys choose the
  -- lexically greatest non-null title, independent of scan/parallel order.
  SELECT session_id, arg_max(value, coalesce(ts, '') ORDER BY value DESC NULLS LAST) FILTER (WHERE kind = 'ai-title') AS ai_title,
         list(DISTINCT value ORDER BY value ASC NULLS LAST) FILTER (WHERE kind = 'pr-link') AS pr_numbers,
         list(DISTINCT struct_pack(repository := pr_repository, number := value)
              ORDER BY struct_pack(repository := pr_repository, number := value) ASC NULLS LAST) FILTER (WHERE kind = 'pr-link') AS pr_links
  FROM session_meta GROUP BY session_id),
s AS (SELECT session_id, bool_or(scrub_failed) AS scrub_failed FROM (
 SELECT session_id,scrub_failed FROM turns UNION ALL SELECT session_id,scrub_failed FROM tool_calls
 UNION ALL SELECT session_id,scrub_failed FROM hooks UNION ALL SELECT session_id,scrub_failed FROM events
 UNION ALL SELECT session_id,scrub_failed FROM session_meta UNION ALL SELECT session_id,scrub_failed FROM agents
) GROUP BY session_id)
SELECT t.*, coalesce(c.n_tool_calls, 0) AS n_tool_calls, coalesce(c.n_errors_flagged, 0) AS n_errors_flagged,
       coalesce(c.n_errors_text, 0) AS n_errors_text, coalesce(c.n_hook_blocks, 0) AS n_hook_blocks,
       m.ai_title, m.pr_numbers, m.pr_links, coalesce(s.scrub_failed, false) AS scrub_failed
FROM t LEFT JOIN c USING (session_id) LEFT JOIN m USING (session_id) LEFT JOIN s USING (session_id);
"""
