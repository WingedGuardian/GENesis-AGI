"""DuckDB views over compatible source generations or a published snapshot.

connect is an internal builder primitive: callers must hold the writer lock.
Public query execution holds writer and publication locks until fetching ends.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import tempfile
import weakref
from pathlib import Path

import duckdb

from genesis.transcript_analytics import catalog, store
from genesis.transcript_analytics.extract import TABLES
from genesis.transcript_analytics.locks import publication, restore_epoch

VIEWS_VERSION = "16"
_DERIVED = (
    "tool_calls",
    "turns",
    "hooks",
    "events",
    "session_meta",
    "agents",
    "sessions",
    "actor_lineage",
    "executors",
    "delegation_rollups",
    "context_rollups",
    "metric_coverage",
)


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
        and isinstance(manifest.get("semantics"), dict)
        and store.semantics_compatible(
            {
                k.encode(): v.encode()
                for k, v in manifest["semantics"].items()
                if isinstance(k, str) and isinstance(v, str)
            }
        )
        and all((cur / f"{v}.parquet").is_file() for v in _DERIVED)
    )


def derived_current(data: Path, selected=store._READ_CATALOG) -> bool:
    selected = store._selection(data, selected)
    manifest = snapshot_manifest(data)
    return snapshot_compatible(data) and (
        manifest.get("catalog_revision") == selected.revision
        if selected is not None
        else manifest.get("input_fp") == data.stat().st_mtime_ns
    )


def _spill_directory():
    root = Path(os.environ.get("TA_DUCKDB_TMP", Path.home() / "tmp/transcript-analytics/duckdb")).absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()):
        raise ValueError("DuckDB temporary root is not an owned directory")
    # Each connection gets an atomically created 0700 child, independently of
    # umask and any permissive pre-existing root on trusted local storage.
    temporary = tempfile.TemporaryDirectory(prefix="private-", dir=root)
    child = Path(temporary.name).lstat()
    if not stat.S_ISDIR(child.st_mode) or child.st_uid != os.getuid() or child.st_mode & 0o077:
        temporary.cleanup()
        raise ValueError("DuckDB spill directory is not private")
    return temporary


def connect(
    data: Path, *, memory_limit=None, threads=None, live=False, selected=store._READ_CATALOG
):
    """Internal unlocked connection; only compatible sources enter live views."""
    import pyarrow as pa

    from genesis.transcript_analytics.schema import SCHEMAS

    # CLI has verified this scope against kernel limits before importing us.
    budget = os.environ.get("GENESIS_TRANSCRIPT_RESOURCE_CHILD")
    ram, cpu = (float(v) for v in budget.split(",")) if budget else (3_000_000_000, 200)
    memory_limit = memory_limit or f"{max(1, int(ram * 0.5))}B"
    threads = threads or max(1, int(cpu / 100))
    temporary = _spill_directory()
    con = duckdb.connect()
    weakref.finalize(con, temporary.cleanup)
    try:
        con.execute("SET memory_limit = ?", [memory_limit])
        con.execute(f"SET threads={int(threads)}")
        con.execute("SET temp_directory = ?", [temporary.name])
        con.execute("SET preserve_insertion_order=false")
        con.execute("SET autoinstall_known_extensions=false")
        con.execute("SET autoload_known_extensions=false")
        if not live and snapshot_compatible(data):
            snap = (data / "derived/current").resolve()
            for view in _DERIVED:
                path = str(snap / f"{view}.parquet").replace("'", "''")
                con.execute(f"CREATE VIEW {view} AS SELECT * FROM read_parquet('{path}')")  # noqa: S608 — fixed view names, escaped local path
        else:
            selected = store._selection(data, selected)
            keys, _excluded = store.compatible_sources(data, selected)
            for table in TABLES:
                files = [str(store._table_path(data, table, key, selected)) for key in keys]
                if files:
                    con.from_parquet(files, union_by_name=False).create_view(f"raw_{table}")
                else:
                    con.register(f"_empty_{table}", pa.Table.from_pylist([], schema=SCHEMAS[table]))
                    con.execute(f"CREATE VIEW raw_{table} AS SELECT * FROM _empty_{table}")  # noqa: S608 — fixed table names
            from .reconcile import install

            install(con)
            con.execute(_VIEWS)
        con.execute("SET lock_configuration=true")
        return con
    except BaseException:
        con.close()
        raise


@contextlib.contextmanager
def _read_state(data, *, live=False):
    if not live:
        with publication():
            selected = catalog.load(data)
            if snapshot_compatible(data):
                with contextlib.closing(connect(data, selected=selected)) as con:
                    yield con, selected, snapshot_manifest(data)
                return
    # Release publication before acquiring writer: same order as restore/build.
    with (
        store._locked(store.DEFAULT_LOCK),
        publication(),
    ):
        selected = catalog.load(data)
        with contextlib.closing(connect(data, live=True, selected=selected)) as con:
            yield con, selected, None


@contextlib.contextmanager
def read_connection(data, *, live=False):
    with _read_state(data, live=live) as (con, _selected, _snapshot):
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


def turn_identity_coverage(con):
    """Count candidate rows without claiming an exact unique-message total."""
    candidates, uncertain = con.execute(
        "SELECT count(*), count(*) FILTER(WHERE count_uncertain) FROM turns"
    ).fetchone()
    return {
        "candidate_rows": candidates,
        "uncertain_candidate_rows": uncertain,
        "unambiguous_candidate_rows": candidates - uncertain,
    }


def run_query(
    data: Path, sql: str, params=None, *, live=False, manifest_path=None, window=None, baseline=None
):
    """Read a consistent generation; stale compatible snapshots remain usable."""
    with _read_state(data, live=live) as (con, selected, snapshot):
        if snapshot:
            coverage, inventory = snapshot.get("coverage", {}), snapshot.get("inventory", {})
            stale = not derived_current(data, selected)
        else:
            keys, excluded = store.compatible_sources(data, selected)
            coverage, inventory = (
                {"included": len(keys), "excluded": excluded},
                store.inventory(data, selected),
            )
            stale = False
        print(
            json.dumps(
                {
                    "mode": "snapshot" if snapshot else "live",
                    "stale": stale,
                    "coverage": coverage,
                    "inventory": inventory,
                    "latest_collection": store.inventory(data, selected),
                    "catalog_revision": snapshot.get("catalog_revision")
                    if snapshot
                    else (selected.revision if selected is not None else None),
                    "latest_catalog_revision": selected.revision if selected is not None else None,
                }
            ),
            file=sys.stderr,
        )
        rel = _execute_query(con, sql, params, snapshot)
        result = (rel.columns, rel.fetchall()) if rel is not None else ([], [])
        if manifest_path:
            from .provenance import write_manifest

            keys, excluded = ([], []) if snapshot else store.compatible_sources(data, selected)
            identity_coverage = turn_identity_coverage(con)
            write_manifest(
                manifest_path,
                {
                    **_scrub_labels(sql, window, baseline),
                    "snapshot": snapshot,
                    "snapshot_current": derived_current(data, selected),
                    "catalog_revision": snapshot.get("catalog_revision")
                    if snapshot
                    else (selected.revision if selected is not None else None),
                    "population_revision": snapshot.get("population_revision")
                    if snapshot
                    else (selected.population_revision if selected is not None else None),
                    "latest_catalog_revision": selected.revision if selected is not None else None,
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
                    "denominator_basis": {"turns": "candidate_turn_rows"},
                    "turn_identity_coverage": identity_coverage,
                    "result_rows": len(result[1]),
                    "source_references": [s["ta.source"] for s in snapshot["sources"]]
                    if snapshot
                    else [
                        store.source_identity(
                            os.fsdecode(
                                store.source_metadata(data, key, selected)[0].get(b"ta.source", b"")
                            )
                        )
                        for key in keys
                    ],
                },
            )
        return result


_TOOL_VIEWS = """
CREATE OR REPLACE VIEW tool_calls AS
WITH calls AS (
  SELECT c.* FROM raw_tool_calls c
  LEFT JOIN message_copy m ON m.turn_key = 'provider:' || c.message_id

  WHERE c.line_no_call IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY c.tool_use_id
                             ORDER BY (c.source_file = m.source_file) DESC NULLS LAST,
                                      c.source_file,
                                      c.line_no_call) = 1),
results AS (
  SELECT r.* FROM raw_tool_calls r
  LEFT JOIN calls k ON k.tool_use_id = r.tool_use_id

  WHERE r.has_result
  QUALIFY row_number() OVER (PARTITION BY r.tool_use_id
                             ORDER BY (r.source_file = k.source_file) DESC NULLS LAST,
                                      r.source_file,
                                      r.line_no_result) = 1)
SELECT
  coalesce(k.source_file, r.source_file) AS source_file,
 CASE WHEN len(own.context_sessions)=1 THEN own.context_sessions[1]
 WHEN own.context_sessions IS NULL THEN coalesce(k.session_id,r.session_id) END AS session_id,
  json_extract_string(own.executor_id,'$[1]') AS agent_id, coalesce(k.tool_use_id, r.tool_use_id) AS tool_use_id,
  CASE WHEN NOT coalesce(states.identity_conflict,false) THEN k.message_id END AS message_id,
  coalesce(states.identity_conflict,false) AS message_identity_conflict,
  k.line_no_call, r.line_no_result, k.ts_call, r.ts_result,
  coalesce(CAST(date_diff('millisecond', try_cast(k.ts_call AS TIMESTAMPTZ), try_cast(r.ts_result AS TIMESTAMPTZ))
                AS BIGINT), CASE WHEN k.source_file = r.source_file THEN r.latency_ms END) AS latency_ms,
  k.tool, k.mcp_server, k.mcp_tool, k.command, k.file_path, k.subagent_type, k.skill_arg, k.description,
  k.input_len, coalesce(k.is_sidechain, r.is_sidechain) AS is_sidechain, k.attribution_skill, k.entrypoint,
  coalesce(k.cwd, r.cwd) AS cwd, coalesce(k.git_branch, r.git_branch) AS git_branch,
  coalesce(r.has_result, false) AS has_result, r.is_error, r.error_source, r.error_class, r.error_text,
  r.tool_use_result_text, r.result_len, r.exit_code, r.interrupted, r.denial_kind,
  r.hook_event, r.hook_tool, r.hook_command, r.hook_script,
  coalesce(k.scrub_failed, false) OR coalesce(r.scrub_failed, false) AS scrub_failed,
  r.source_file AS result_source_file, k.call_uuid,
  own.executor_id, own.attribution_status, own.attribution_reason, own.context_sessions,
  lineage.parent_actor_id AS delegator_id, lineage.lineage_status,
 coalesce(counts.call_variants=1 AND NOT states.content_conflict,false) AS call_valid,
 coalesce(counts.result_variants=1,false) AS result_valid,
  counts.call_variants > 1 AS content_conflict, counts.result_variants > 1 AS result_conflict,
  counts.source_references
FROM calls k FULL OUTER JOIN results r ON r.tool_use_id = k.tool_use_id
LEFT JOIN tool_ownership own ON own.tool_use_id=k.tool_use_id
LEFT JOIN event_states states ON states.event_key=coalesce('uuid:' || k.call_uuid,'local:' || k.source_file || ':' || k.line_no_call)
LEFT JOIN actor_lineage lineage ON lineage.actor_id=own.executor_id
LEFT JOIN (SELECT tool_use_id, count(DISTINCT call_hash) AS call_variants,
 count(DISTINCT result_hash) AS result_variants,
 list(DISTINCT struct_pack(source_file:=source_file,line_no_call:=line_no_call,line_no_result:=line_no_result)
 ORDER BY struct_pack(source_file:=source_file,line_no_call:=line_no_call,line_no_result:=line_no_result)) AS source_references
 FROM raw_tool_calls GROUP BY tool_use_id) counts
 ON counts.tool_use_id=coalesce(k.tool_use_id,r.tool_use_id);

"""

_TURN_VIEWS = """
CREATE OR REPLACE VIEW turns AS
WITH grouped AS (
 SELECT turn_key, any_value(message_id ORDER BY sequence_line,source_file) AS message_id,
 any_value(session_id ORDER BY sequence_line,source_file) AS observed_session_id,
 any_value(agent_id ORDER BY sequence_line,source_file) AS observed_agent_id,
 any_value(representation_source ORDER BY sequence_line,source_file) AS source_file,
 arg_min(ts,try_cast(ts AS TIMESTAMPTZ)) AS ts,arg_max(ts,try_cast(ts AS TIMESTAMPTZ)) AS ts_last,count(*) AS n_records,
 bool_and(assembly_complete) AS assembly_complete,bool_or(content_conflict) AS content_conflict,
 CASE WHEN count(DISTINCT executor_id)=1 AND count(*) FILTER(WHERE executor_id IS NULL)=0
 THEN any_value(executor_id) END AS executor_id,
 CASE WHEN bool_or(attribution_status='conflicting') THEN 'conflicting'
 WHEN count(DISTINCT executor_id)!=1 OR count(*) FILTER(WHERE executor_id IS NULL)>0 THEN 'unresolved'
 WHEN bool_or(attribution_status='inferred') THEN 'inferred' ELSE 'confirmed' END AS attribution_status,
 list(DISTINCT attribution_reason ORDER BY attribution_reason) AS attribution_reasons,
 flatten(list(source_references ORDER BY sequence_line)) AS source_references,
 list_sort(list_distinct(flatten(list(context_sessions ORDER BY sequence_line)))) AS context_sessions,
 CASE WHEN NOT bool_or(content_conflict) AND NOT bool_or(identity_conflict) AND bool_and(assembly_complete) THEN CAST(sum(n_tool_use) AS DECIMAL(38,0)) END AS n_tool_use,bool_or(is_sidechain) AS is_sidechain,
 any_value(entrypoint ORDER BY sequence_line,source_file) AS entrypoint,
 any_value(attribution_skill ORDER BY sequence_line,source_file) AS attribution_skill,
 any_value(attribution_mcp_server ORDER BY sequence_line,source_file) AS attribution_mcp_server,
 any_value(attribution_mcp_tool ORDER BY sequence_line,source_file) AS attribution_mcp_tool,
 any_value(effort ORDER BY sequence_line,source_file) AS effort,
 any_value(cwd ORDER BY sequence_line,source_file) AS cwd,
 any_value(git_branch ORDER BY sequence_line,source_file) AS git_branch,
 any_value(version ORDER BY sequence_line,source_file) AS version
 FROM assembled_fragments GROUP BY turn_key
), final AS (
 SELECT * FROM observations
 QUALIFY row_number() OVER(PARTITION BY turn_key ORDER BY
 (stop_reason IS NOT NULL) DESC,source_file,line_no,payload_hash)=1
), base AS (
 SELECT g.* EXCLUDE(content_conflict,executor_id,attribution_status,attribution_reasons,
 source_references,context_sessions),
 e.content_conflict,e.executor_id,e.attribution_status,e.attribution_reasons,
 e.source_references,e.context_sessions,e.is_api_error,e.scrub_failed,
 e.distinct_event_count,e.physical_observation_count,e.terminal_variants,e.identity_conflict,e.count_uncertain,
 final.stop_reason,final.model,final.request_id,
 (g.assembly_complete AND NOT e.content_conflict AND NOT e.state_conflict
 AND NOT e.identity_conflict AND e.executor_id IS NOT NULL AND e.attribution_status IN ('confirmed','inferred')
 AND e.terminal_variants=1
 AND final.stop_reason IS NOT NULL AND final.has_usage AND NOT final.is_api_error) AS usage_available,
 (e.identity_conflict OR e.state_conflict OR e.terminal_variants>1) AS final_usage_conflict,
 final.input_tokens,final.output_tokens,final.cache_read,final.cache_create,
 final.cache_create_5m,final.cache_create_1h,final.thinking_tokens
 FROM grouped g JOIN turn_evidence e USING(turn_key) JOIN final USING(turn_key)
)
SELECT b.* EXCLUDE(observed_agent_id,observed_session_id,input_tokens,output_tokens,cache_read,cache_create,cache_create_5m,cache_create_1h,thinking_tokens),
 CASE WHEN usage_available THEN input_tokens END AS input_tokens,
 CASE WHEN usage_available THEN output_tokens END AS output_tokens,
 CASE WHEN usage_available THEN cache_read END AS cache_read,
 CASE WHEN usage_available THEN cache_create END AS cache_create,
 CASE WHEN usage_available THEN cache_create_5m END AS cache_create_5m,
 CASE WHEN usage_available THEN cache_create_1h END AS cache_create_1h,
 CASE WHEN usage_available THEN thinking_tokens END AS thinking_tokens,
 CASE WHEN len(b.context_sessions)=1 THEN b.context_sessions[1] END AS session_id,
 json_extract_string(b.executor_id,'$[1]') AS agent_id,
 l.parent_actor_id AS delegator_id,l.lineage_status,l.lineage_reason
FROM base b LEFT JOIN actor_lineage l ON b.executor_id=l.actor_id;

"""

_SOURCE_VIEWS = """
CREATE OR REPLACE VIEW hooks AS
WITH keyed AS (
 SELECT *,coalesce('uuid:' || uuid,'local:' || source_file || ':' || line_no) AS event_key
 FROM raw_hooks
), states AS (
 SELECT event_key,count(DISTINCT record_hash)>1 AS content_conflict,
 CASE WHEN count(DISTINCT actor_id)=1 AND NOT bool_or(context_conflict)
 THEN any_value(actor_id ORDER BY actor_id) END AS executor_id,
 list(DISTINCT context_session_id ORDER BY context_session_id) AS context_sessions,
 bool_or(context_conflict) AS context_conflict,
 list(DISTINCT struct_pack(source_file:=source_file,line_no:=line_no)
 ORDER BY struct_pack(source_file:=source_file,line_no:=line_no)) AS source_references
 FROM keyed GROUP BY event_key
)
SELECT k.* EXCLUDE(event_key,agent_id,session_id),s.content_conflict,s.source_references,
 CASE WHEN NOT s.content_conflict THEN s.executor_id END AS executor_id,
 CASE WHEN s.content_conflict OR s.context_conflict THEN 'conflicting'
 WHEN s.executor_id IS NULL THEN 'unresolved' ELSE 'confirmed' END AS attribution_status,
 s.context_sessions,CASE WHEN len(s.context_sessions)=1 THEN s.context_sessions[1] END AS session_id,
 CASE WHEN NOT s.content_conflict THEN json_extract_string(s.executor_id,'$[1]') END AS agent_id
FROM keyed k JOIN states s USING(event_key)
QUALIFY row_number() OVER(PARTITION BY event_key ORDER BY source_file,line_no)=1;

CREATE OR REPLACE VIEW events AS
WITH keyed AS (
 SELECT *,coalesce('uuid:' || uuid,'local:' || source_file || ':' || line_no) AS event_key
 FROM raw_events
), states AS (
 SELECT event_key,count(DISTINCT record_hash)>1 AS content_conflict,
 CASE WHEN count(DISTINCT actor_id)=1 AND NOT bool_or(context_conflict)
 THEN any_value(actor_id ORDER BY actor_id) END AS executor_id,
 list(DISTINCT context_session_id ORDER BY context_session_id) AS context_sessions,
 bool_or(context_conflict) AS context_conflict,
 list(DISTINCT struct_pack(source_file:=source_file,line_no:=line_no)
 ORDER BY struct_pack(source_file:=source_file,line_no:=line_no)) AS source_references
 FROM keyed GROUP BY event_key
)
SELECT k.* EXCLUDE(event_key,agent_id,session_id),s.content_conflict,s.source_references,
 CASE WHEN NOT s.content_conflict THEN s.executor_id END AS executor_id,
 CASE WHEN s.content_conflict OR s.context_conflict THEN 'conflicting'
 WHEN s.executor_id IS NULL THEN 'unresolved' ELSE 'confirmed' END AS attribution_status,
 s.context_sessions,CASE WHEN len(s.context_sessions)=1 THEN s.context_sessions[1] END AS session_id,
 CASE WHEN NOT s.content_conflict THEN json_extract_string(s.executor_id,'$[1]') END AS agent_id
FROM keyed k JOIN states s USING(event_key)
QUALIFY row_number() OVER(PARTITION BY event_key ORDER BY source_file,line_no)=1;

CREATE OR REPLACE VIEW session_meta AS
SELECT session_id, kind, value, pr_repository, arg_max(ts,try_cast(ts AS TIMESTAMPTZ)) AS ts, bool_or(scrub_failed) AS scrub_failed
FROM raw_session_meta GROUP BY session_id, kind, value, pr_repository;

CREATE OR REPLACE VIEW agents AS
SELECT * EXCLUDE (rn) FROM (
  SELECT *, row_number() OVER (PARTITION BY actor_id ORDER BY source_file) AS rn FROM raw_agents) WHERE rn = 1;

CREATE OR REPLACE VIEW sessions AS
WITH t AS (
  SELECT session_id, arg_min(ts,try_cast(ts AS TIMESTAMPTZ)) AS first_ts, arg_max(ts_last,try_cast(ts_last AS TIMESTAMPTZ)) AS last_ts, count(*) AS n_turns,
 count(*) FILTER(WHERE count_uncertain) AS n_turns_count_uncertain,
         count(*) FILTER (WHERE executor_id IS NOT NULL AND agent_id IS NULL) AS n_main_turns,
         count(*) FILTER(WHERE executor_id IS NULL) AS n_unattributed_turns, count(DISTINCT agent_id) AS n_subagents,
         CAST(sum(output_tokens) AS DECIMAL(38,0)) AS output_tokens, CAST(sum(input_tokens) AS DECIMAL(38,0)) AS input_tokens,
         CAST(sum(cache_read) AS DECIMAL(38,0)) AS cache_read, CAST(sum(cache_create) AS DECIMAL(38,0)) AS cache_create,
         count(*) FILTER (WHERE NOT usage_available) AS n_turns_no_usage,
         list(DISTINCT model ORDER BY model ASC NULLS LAST) AS models,
         any_value(cwd ORDER BY try_cast(ts AS TIMESTAMPTZ) ASC NULLS LAST, source_file ASC NULLS LAST, message_id ASC NULLS LAST, cwd ASC NULLS LAST) AS cwd,
         any_value(git_branch ORDER BY try_cast(ts AS TIMESTAMPTZ) ASC NULLS LAST, source_file ASC NULLS LAST, message_id ASC NULLS LAST, git_branch ASC NULLS LAST) AS git_branch
  FROM turns GROUP BY session_id),
c AS (
  SELECT session_id, count(*) FILTER(WHERE call_valid) AS n_tool_calls,
         count(*) FILTER(WHERE NOT call_valid) AS n_tool_calls_excluded,
         count(*) FILTER (WHERE call_valid AND result_valid AND error_source = 'flag') AS n_errors_flagged,
         count(*) FILTER (WHERE call_valid AND result_valid AND error_source = 'text') AS n_errors_text,
         count(*) FILTER (WHERE call_valid AND result_valid AND error_class = 'hook_block') AS n_hook_blocks
  FROM tool_calls GROUP BY session_id),
m AS (
  -- Untimed titles retain the empty timestamp key; equal keys choose the
  -- lexically greatest non-null title, independent of scan/parallel order.
  SELECT session_id, arg_max(value, coalesce(try_cast(ts AS TIMESTAMPTZ), '-infinity'::TIMESTAMPTZ) ORDER BY value DESC NULLS LAST) FILTER (WHERE kind = 'ai-title') AS ai_title,
         arg_max(value, coalesce(try_cast(ts AS TIMESTAMPTZ), '-infinity'::TIMESTAMPTZ) ORDER BY value DESC NULLS LAST) FILTER(WHERE kind='custom-title') AS custom_title,
         list(DISTINCT value ORDER BY value ASC NULLS LAST) FILTER (WHERE kind = 'pr-link') AS pr_numbers,
         list(DISTINCT struct_pack(repository := pr_repository, number := value)
              ORDER BY struct_pack(repository := pr_repository, number := value) ASC NULLS LAST) FILTER (WHERE kind = 'pr-link') AS pr_links
  FROM session_meta GROUP BY session_id),
s AS (SELECT session_id, bool_or(scrub_failed) AS scrub_failed FROM (
 SELECT session_id,scrub_failed FROM turns UNION ALL SELECT session_id,scrub_failed FROM tool_calls
 UNION ALL SELECT session_id,scrub_failed FROM hooks UNION ALL SELECT session_id,scrub_failed FROM events
 UNION ALL SELECT session_id,scrub_failed FROM session_meta UNION ALL SELECT session_id,scrub_failed FROM agents
) GROUP BY session_id)
SELECT t.*, coalesce(c.n_tool_calls, 0) AS n_tool_calls, coalesce(c.n_tool_calls_excluded,0) AS n_tool_calls_excluded, coalesce(c.n_errors_flagged, 0) AS n_errors_flagged,
       coalesce(c.n_errors_text, 0) AS n_errors_text, coalesce(c.n_hook_blocks, 0) AS n_hook_blocks,
       m.ai_title, m.custom_title, m.pr_numbers, m.pr_links, coalesce(s.scrub_failed, false) AS scrub_failed
FROM t LEFT JOIN c ON c.session_id IS NOT DISTINCT FROM t.session_id
LEFT JOIN m ON m.session_id IS NOT DISTINCT FROM t.session_id
LEFT JOIN s ON s.session_id IS NOT DISTINCT FROM t.session_id;

"""

_COVERAGE_VIEWS = """
CREATE OR REPLACE VIEW executors AS
SELECT executor_id,attribution_status,count(*) AS n_turns,
 count(*) FILTER(WHERE count_uncertain) AS n_turns_count_uncertain,
 count(*) FILTER(WHERE usage_available) AS n_turns_with_usage,
 CAST(sum(output_tokens) AS DECIMAL(38,0)) AS output_tokens
FROM turns GROUP BY executor_id,attribution_status;

CREATE OR REPLACE VIEW delegation_rollups AS
WITH RECURSIVE membership(turn_key,actor_id) AS (
 SELECT turn_key,executor_id FROM turns WHERE executor_id IS NOT NULL
 UNION
 SELECT m.turn_key,l.parent_actor_id FROM membership m JOIN actor_lineage l ON l.actor_id=m.actor_id
 WHERE l.parent_actor_id IS NOT NULL AND l.lineage_status IN ('confirmed','inferred')
)
SELECT actor_id,count(DISTINCT turn_key) AS n_turns_inclusive,
 count(DISTINCT turn_key) FILTER(WHERE usage_available) AS n_turns_with_usage,
 CAST(sum(output_tokens) AS DECIMAL(38,0)) AS output_tokens_inclusive
FROM membership JOIN turns USING(turn_key) GROUP BY actor_id;

CREATE OR REPLACE VIEW context_rollups AS
SELECT context_session_id,count(DISTINCT turn_key) AS n_turns_inclusive,
 count(DISTINCT turn_key) FILTER(WHERE count_uncertain) AS n_turns_count_uncertain,
 count(DISTINCT turn_key) FILTER(WHERE usage_available) AS n_turns_with_usage,
 CAST(sum(output_tokens) AS DECIMAL(38,0)) AS output_tokens_inclusive
FROM turns,unnest(context_sessions) c(context_session_id) GROUP BY context_session_id;

CREATE OR REPLACE VIEW metric_coverage AS
SELECT 'turn_content' AS metric,count(*) FILTER(WHERE NOT content_conflict) AS included,
 count(*) FILTER(WHERE content_conflict) AS excluded FROM turns
UNION ALL SELECT 'turn_identity',count(*) FILTER(WHERE NOT count_uncertain),count(*) FILTER(WHERE count_uncertain) FROM turns
UNION ALL SELECT 'turn_assembly',count(*) FILTER(WHERE assembly_complete),count(*) FILTER(WHERE NOT assembly_complete) FROM turns
UNION ALL SELECT 'turn_usage',count(*) FILTER(WHERE usage_available),count(*) FILTER(WHERE NOT usage_available) FROM turns
UNION ALL SELECT 'turn_executor',count(*) FILTER(WHERE executor_id IS NOT NULL),count(*) FILTER(WHERE executor_id IS NULL) FROM turns
UNION ALL SELECT 'turn_input_tokens',count(*) FILTER(WHERE input_tokens IS NOT NULL),count(*) FILTER(WHERE input_tokens IS NULL) FROM turns
UNION ALL SELECT 'turn_output_tokens',count(*) FILTER(WHERE output_tokens IS NOT NULL),count(*) FILTER(WHERE output_tokens IS NULL) FROM turns
UNION ALL SELECT 'turn_cache_read',count(*) FILTER(WHERE cache_read IS NOT NULL),count(*) FILTER(WHERE cache_read IS NULL) FROM turns
UNION ALL SELECT 'turn_cache_create',count(*) FILTER(WHERE cache_create IS NOT NULL),count(*) FILTER(WHERE cache_create IS NULL) FROM turns
UNION ALL SELECT 'turn_cache_create_5m',count(*) FILTER(WHERE cache_create_5m IS NOT NULL),count(*) FILTER(WHERE cache_create_5m IS NULL) FROM turns
UNION ALL SELECT 'turn_cache_create_1h',count(*) FILTER(WHERE cache_create_1h IS NOT NULL),count(*) FILTER(WHERE cache_create_1h IS NULL) FROM turns
UNION ALL SELECT 'turn_thinking_tokens',count(*) FILTER(WHERE thinking_tokens IS NOT NULL),count(*) FILTER(WHERE thinking_tokens IS NULL) FROM turns
UNION ALL SELECT 'tool_call_content',count(*) FILTER(WHERE call_valid),count(*) FILTER(WHERE NOT call_valid) FROM tool_calls
UNION ALL SELECT 'hook_content',count(*) FILTER(WHERE NOT content_conflict),count(*) FILTER(WHERE content_conflict) FROM hooks
UNION ALL SELECT 'event_content',count(*) FILTER(WHERE NOT content_conflict),count(*) FILTER(WHERE content_conflict) FROM events
UNION ALL SELECT 'tool_result_content',count(*) FILTER(WHERE result_valid),count(*) FILTER(WHERE NOT result_valid) FROM tool_calls
UNION ALL SELECT 'spawn_association',count(*) FILTER(WHERE association_status IN ('confirmed','inferred')),
 count(*) FILTER(WHERE association_status NOT IN ('confirmed','inferred')) FROM spawn_associations;
"""


_VIEWS = _TOOL_VIEWS + _TURN_VIEWS + _SOURCE_VIEWS + _COVERAGE_VIEWS
