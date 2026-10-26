"""DuckDB views over the durable per-source records.

Readers take no lock: each source record is resolved once through its
``current`` link, so a record mid-replace is the old record or the new one,
never a mix. Sources that fail validation are excluded, not half-read.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import sys
import tempfile
import weakref
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from genesis.transcript_analytics import history, store
from genesis.transcript_analytics.extract import TABLES

VIEWS_VERSION = "24"
_READS = weakref.WeakKeyDictionary()
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
    # Reconciliation outputs are part of the shared query surface, so the
    # disposable snapshot carries them alongside the deduplicated views.
    "observations",
    "provider_candidates",
    "turn_events",
    "event_states",
    "canonical_fragments",
    "fragments",
    "message_sequences",
    "representation",
    "message_copy",
    "inherited",
    "event_ownership",
    "turn_evidence",
    "assembled_fragments",
    "result_candidates",
    "producers",
    "companion_assertions",
    "spawn_associations",
    "tool_ownership",
)


def _snapshot(data, selection):
    # The disposable layout is a real derived/ directory whose current
    # selector is a symlink into it; anything else is malformed cache and
    # the reader falls back to the live views.
    raw_root = data / "derived"
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise ValueError("invalid snapshot root")
    root = raw_root.resolve()
    selector = root / "current"
    if not selector.is_symlink():
        raise ValueError("invalid snapshot selector")
    directory = selector.resolve(strict=True)
    if directory.parent != root or not directory.is_dir():
        raise ValueError("invalid snapshot selector")
    # A FIFO or device in the cache must not block the reader (or a rebuild
    # holding the writer lock); only a regular file is read.
    with store._open_regular(directory / "MANIFEST.json") as handle:
        manifest = json.loads(handle.read())
    if (
        not isinstance(manifest, dict)
        or manifest.get("views_version") != VIEWS_VERSION
        or manifest.get("inputs_signature") != store.inputs_signature(data, selection=selection)
        or manifest.get("views") != list(_DERIVED)
        or not isinstance(manifest.get("schemas"), dict)
    ):
        raise ValueError("snapshot is not current")
    return directory, manifest


def _load_snapshot(con, data, selection):
    started = False
    try:
        directory, manifest = _snapshot(data, selection)
        con.execute("BEGIN")
        started = True
        for view in _DERIVED:
            table_file = directory / f"{view}.parquet"
            # As for the manifest: a special file would block the full read.
            if not stat.S_ISREG(os.lstat(table_file).st_mode):
                raise ValueError("snapshot table is not a regular file")
            path = str(table_file).replace("'", "''")
            # CTAS reads every page now; returned connections never lazily
            # depend on a disposable snapshot that another build can remove.
            con.execute(
                f"CREATE TEMP TABLE _derived_{view} AS SELECT * FROM read_parquet('{path}')"  # noqa: S608 — fixed view names, escaped path
            )
            con.execute(f"CREATE VIEW {view} AS SELECT * FROM _derived_{view}")  # noqa: S608 — fixed view names
            schema = [list(row[:2]) for row in con.execute(f"DESCRIBE {view}").fetchall()]  # noqa: S608 — fixed view names
            if schema != manifest["schemas"].get(view):
                raise ValueError("incompatible snapshot schema")
        con.execute("COMMIT")
        return manifest
    except Exception as exc:
        if started:
            con.execute("ROLLBACK")
        print(json.dumps({"event": "snapshot_fallback", "reason": type(exc).__name__}), file=sys.stderr)
        return None


def derived_current(data, *, selection=None):
    selection = store.select_sources(data) if selection is None else selection
    try:
        _snapshot(data, selection)
        # snapshot_only: answering "is the cache current?" must not pay for the
        # full live computation a missing snapshot otherwise triggers.
        with contextlib.closing(connect(data, selection=selection, snapshot_only=True)) as con:
            return _READS[con].get("snapshot") is not None
    except Exception:
        return False


def _spill_directory():
    root = Path(os.environ.get("TA_DUCKDB_TMP", Path.home() / "tmp/transcript-analytics/duckdb")).absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()):
        raise ValueError("DuckDB temporary root is not an owned directory")
    if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
        raise ValueError("DuckDB temporary root is writable without the sticky bit")
    # Each connection gets an atomically created 0700 child, independently of
    # umask and any permissive pre-existing root on trusted local storage.
    temporary = tempfile.TemporaryDirectory(prefix="private-", dir=root)
    child = Path(temporary.name).lstat()
    if not stat.S_ISDIR(child.st_mode) or child.st_uid != os.getuid() or child.st_mode & 0o077:
        temporary.cleanup()
        raise ValueError("DuckDB spill directory is not private")
    return temporary


def _configured(memory_limit, threads):
    """A DuckDB connection with the resource settings and its own private spill."""
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
    except BaseException:
        con.close()
        raise
    return con


def _install_live(con, records):
    """The raw_* views over ``records``, then reconciliation and the derived views."""
    import pyarrow as pa

    from genesis.transcript_analytics.schema import SCHEMAS

    for table in TABLES:
        groups = {}
        for record in records:
            groups.setdefault(record.adapter, []).append(str(record.path(table)))
        parts = []
        for adapter, files in groups.items():
            relation = con.from_parquet(files, union_by_name=False)
            present = history.schema_for(table, adapter).names
            types = {"string": "VARCHAR", "int64": "BIGINT", "bool": "BOOLEAN"}
            columns = [
                f'"{f.name}"' if f.name in present else
                f'NULL::{types.get(str(f.type), "VARCHAR[]")} AS "{f.name}"'
                for f in SCHEMAS[table]
            ]
            parts.append(relation.project(",".join(columns)))
        if parts:
            relation = parts[0]
            for part in parts[1:]:
                relation = relation.union(part)
            relation.create_view(f"raw_{table}")
        else:
            con.register(f"_empty_{table}", pa.Table.from_pylist([], schema=SCHEMAS[table]))
            con.execute(f"CREATE VIEW raw_{table} AS SELECT * FROM _empty_{table}")  # noqa: S608 — fixed table names
    from .reconcile import install

    install(con)
    con.execute(_VIEWS)


_UNITS = {
    "": 1, "B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
    "KIB": 2**10, "MIB": 2**20, "GIB": 2**30, "TIB": 2**40,
}


def _half(limit):
    """Half a DuckDB memory limit string (``1500000000B``, ``16MB``, ``2GiB``), in bytes."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?I?B)?\s*", str(limit), re.IGNORECASE)
    if not match:
        raise ValueError(f"unreadable memory limit {limit!r}")
    unit = _UNITS[(match.group(2) or "").upper()]
    return f"{max(1, int(float(match.group(1)) * unit / 2))}B"


def _load_live_derived(con, records, memory_limit, threads):
    """Without a current snapshot, live=False still exposes only the derived views.

    The live graph is built on a private connection and each derived view is read
    completely into this connection, as the snapshot loader does. Its raw_* views
    and reconciliation intermediates never reach the caller, so live=False offers
    the same relations whether or not the snapshot loaded, and no SQL text check is
    needed to keep raw data behind ``live=True``.

    Both DuckDB instances are alive during the copy, so each gets half the
    caller's memory limit until it ends. Lossless Arrow conversion keeps JSON,
    HUGEINT and UUID columns their own types instead of VARCHAR or DECIMAL.
    """
    half = _half(memory_limit)
    con.execute("SET memory_limit = ?", [half])
    try:
        with contextlib.closing(_configured(half, threads)) as private:
            private.execute("SET arrow_lossless_conversion = true")
            _install_live(private, records)
            for view in _DERIVED:
                rows = private.sql(f"SELECT * FROM {view}").arrow()  # noqa: S608 — fixed view names
                con.register("_derived_source", rows)
                try:
                    con.execute(f"CREATE TEMP TABLE _derived_{view} AS SELECT * FROM _derived_source")  # noqa: S608 — fixed view names
                finally:
                    con.unregister("_derived_source")
                con.execute(f"CREATE VIEW {view} AS SELECT * FROM _derived_{view}")  # noqa: S608 — fixed view names
    finally:
        con.execute("SET memory_limit = ?", [memory_limit])


def connect(
    data: Path, *, memory_limit=None, threads=None, selection=None, live=False, snapshot_only=False
):
    """Use a fully read current cache, or live views over one source selection.

    ``live=False`` exposes the derived views only: from the snapshot when it is
    current, otherwise computed live and read in full. ``live=True`` adds the
    raw_* views and reconciliation intermediates. ``snapshot_only`` raises instead
    of computing the derived views live when no current snapshot loads.
    """
    import pyarrow as pa

    # CLI has verified this scope against kernel limits before importing us.
    budget = os.environ.get("GENESIS_TRANSCRIPT_RESOURCE_CHILD")
    ram, cpu = (float(v) for v in budget.split(",")) if budget else (3_000_000_000, 200)
    memory_limit = memory_limit or f"{max(1, int(ram * 0.5))}B"
    threads = threads or max(1, int(cpu / 100))
    con = _configured(memory_limit, threads)
    try:
        selection = store.select_sources(data) if selection is None else selection
        records, excluded = selection
        _READS[con] = {
            "selection": selection,
            "inputs_signature": store.inputs_signature(data, selection=selection),
            "coverage": {
                "included": len(records), "excluded": excluded,
                "historical": [
                    {
                        "source": store.source_identity(os.fsdecode(r.metadata[b"ta.source"])),
                        "adapter": r.adapter, "unknown": history.unknown_facts(r.adapter),
                        "provenance": {
                            k.decode(): os.fsdecode(v) for k, v in r.metadata.items()
                            if k.startswith(b"ta.")
                        },
                    } for r in records if r.adapter
                ],
            },
        }
        coverage_rows = [
            {
                "source_file": store.source_identity(os.fsdecode(r.metadata[b"ta.source"])),
                "historical_format": r.adapter,
                "provenance": json.dumps({
                    k.decode(): os.fsdecode(v) for k, v in r.metadata.items()
                    if k.startswith(b"ta.")
                }, ensure_ascii=True),
                "unknown_facts": json.dumps(history.unknown_facts(r.adapter)) if r.adapter else None,
            } for r in records
        ]
        con.register("_source_coverage", pa.Table.from_pylist(
            coverage_rows, schema=pa.schema([
                (name, pa.string()) for name in
                ("source_file", "historical_format", "provenance", "unknown_facts")
            ])
        ))
        con.execute("CREATE VIEW source_coverage AS SELECT * FROM _source_coverage")
        if not live:
            snapshot = _load_snapshot(con, data, selection)
            if snapshot is not None:
                _READS[con]["snapshot"] = snapshot
            elif snapshot_only:
                raise ValueError("no current snapshot")
            else:
                _load_live_derived(con, records, memory_limit, threads)
        else:
            _install_live(con, records)
        con.execute("SET lock_configuration=true")
        return con
    except BaseException:
        con.close()
        raise


@contextlib.contextmanager
def read_connection(data, *, live=False):
    with contextlib.closing(connect(data, live=live)) as con:
        yield con


def _scrub_manifest_json(value, key=None):
    from . import scrub

    source_keys = {"source", "source_file", "ta.source", "failed_sources", "unbuilt", "source_references"}
    if isinstance(value, dict):
        return {k: _scrub_manifest_json(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_manifest_json(v, key) for v in value]
    if key == "projects_root":
        return "[redacted]"
    if key in source_keys and isinstance(value, str):
        try:
            return store.srckey(store.source_path(value))
        except (TypeError, ValueError):
            return "[redacted]"
    if isinstance(value, str):
        return scrub.scrub_text(value)[0]
    return value


def _write_manifest(path, value: dict):
    """Private atomic evidence manifest publication; no outcome database."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"generated_at": datetime.now(UTC).isoformat(), **_scrub_manifest_json(value)}
    fd, temporary = tempfile.mkstemp(prefix=".evidence-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


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
    data: Path, sql: str, params=None, *, live: bool = False, manifest_path=None, window=None, baseline=None
):
    """Read a current cache or the captured live views; never serve stale cache."""
    with contextlib.closing(connect(data, live=live)) as con:
        coverage = _READS[con]["coverage"]
        snapshot = _READS[con].get("snapshot")
        # The inventory is diagnostic and read only after the immutable source
        # selection has been captured; manifest consumers should use coverage.
        inventory = store.inventory(data)
        print(
            json.dumps(
                {
                    "mode": "snapshot" if snapshot is not None else "live",
                    "coverage": coverage,
                    "inventory": inventory,
                }
            ),
            file=sys.stderr,
        )
        # With live=False the connection holds only the derived views (connect),
        # so a raw_* reference fails as an unknown table in either mode. The hint
        # is added without reading the SQL text.
        try:
            rel = con.sql(sql, params=params)
        except duckdb.CatalogException as exc:
            if live:
                raise
            raise duckdb.CatalogException(
                f"{exc}\nlive=False reads derived views only; raw_* views and"
                " reconciliation intermediates need live=True"
            ) from exc
        result = (rel.columns, rel.fetchall()) if rel is not None else ([], [])
        if manifest_path:
            identity_coverage = turn_identity_coverage(con)
            _write_manifest(
                manifest_path,
                {
                    **_scrub_labels(sql, window, baseline),
                    "snapshot": snapshot,
                    "snapshot_current": snapshot is not None,
                    "live": live,
                    "inventory": inventory,
                    "inputs_signature": _READS[con]["inputs_signature"],
                    "versions": {
                        **{k.decode(): v.decode() for k, v in store.semantics().items()},
                        "views_version": VIEWS_VERSION,
                    },
                    "coverage": coverage,
                    "denominators": {
                        v: con.execute(f"SELECT count(*) FROM {v}").fetchone()[0]  # noqa: S608 — fixed view names
                        for v in ("tool_calls", "turns", "sessions")
                    },
                    "denominator_basis": {"turns": "candidate_turn_rows"},
                    "turn_identity_coverage": identity_coverage,
                    "result_rows": len(result[1]),
                    "source_references": [
                        store.source_identity(
                            os.fsdecode(
                                record.metadata.get(b"ta.source", b"")
                            )
                        )
                        for record in _READS[con]["selection"][0]
                    ],
                },
            )
        return result


_TOOL_VIEWS = """
CREATE OR REPLACE VIEW tool_calls AS
-- tool_use_id is only local to a transcript: copies of one call share its
-- call observation key (uuid or source+line), while unrelated calls may
-- reuse the id. Dedup on the observation key, then pair each result row to
-- its best call on same-source/session evidence.
WITH calls AS (
  SELECT * EXCLUDE (pick) FROM (
    SELECT c.*,
      s.content_conflict AS call_content_conflict,
      bool_or(c.scrub_failed) OVER (PARTITION BY c.call_key) AS call_scrub_failed,
      list(DISTINCT struct_pack(source_file:=c.source_file,line_no_call:=c.line_no_call,
        line_no_result:=c.line_no_result)
        ORDER BY struct_pack(source_file:=c.source_file,line_no_call:=c.line_no_call,
        line_no_result:=c.line_no_result)) OVER (PARTITION BY c.call_key) AS call_references,
      row_number() OVER (PARTITION BY c.call_key
                         ORDER BY (c.source_file = m.source_file) DESC NULLS LAST,
                                  c.source_file,
                                  c.line_no_call, c.call_hash) AS pick
    FROM call_observations c JOIN call_states s USING(call_key)
    LEFT JOIN message_copy m ON m.turn_key = 'provider:' || c.message_id
    WHERE c.line_no_call IS NOT NULL
  ) WHERE pick = 1
),
paired AS (
  SELECT * EXCLUDE (pick) FROM (
    SELECT r.* EXCLUDE(scrub_failed),
      bool_or(r.scrub_failed) OVER (PARTITION BY r.source_file,r.line_no_result,r.tool_use_id,r.result_hash) AS scrub_failed,
      CASE WHEN count(DISTINCT k.call_key) OVER (
        PARTITION BY r.source_file, r.line_no_result, r.tool_use_id, r.result_hash
      ) = 1 THEN k.call_key END AS call_key,
      to_json([r.source_file, CAST(r.line_no_result AS VARCHAR), r.tool_use_id]) AS result_key,
      row_number() OVER (PARTITION BY r.source_file, r.line_no_result, r.tool_use_id, r.result_hash
                         ORDER BY (r.source_file = k.source_file) DESC NULLS LAST,
                                  (r.session_id = k.session_id) DESC NULLS LAST,
                                  k.source_file,
                                  k.line_no_call) AS pick
    FROM raw_tool_calls r
    LEFT JOIN calls k ON k.tool_use_id = r.tool_use_id AND (
      (r.call_uuid IS NOT NULL AND r.call_uuid = k.call_uuid) OR
      (r.call_uuid IS NULL AND (r.source_file = k.source_file OR
        (r.context_session_id IS NOT NULL AND r.context_session_id = k.context_session_id))))
    WHERE r.has_result
  ) WHERE pick = 1
),
results AS (
  SELECT * FROM paired
  QUALIFY row_number() OVER (
    PARTITION BY coalesce(call_key, result_key)
    ORDER BY source_file, line_no_result, result_hash) = 1
),
envelopes AS (
  SELECT coalesce(call_key, result_key) AS envelope_key,
    CASE WHEN count(DISTINCT result_hash)>1 THEN TRUE
      WHEN count(*) FILTER(WHERE result_hash IS NULL)>0 THEN NULL ELSE FALSE END AS result_content_conflict,
    bool_or(scrub_failed) AS result_scrub_failed,
    list(DISTINCT struct_pack(source_file:=source_file,line_no_call:=line_no_call,
      line_no_result:=line_no_result)
      ORDER BY struct_pack(source_file:=source_file,line_no_call:=line_no_call,
      line_no_result:=line_no_result)) AS result_references
  FROM paired GROUP BY coalesce(call_key, result_key)
)
SELECT
  coalesce(k.source_file, r.source_file) AS source_file,
 CASE WHEN len(own.context_sessions)=1 THEN own.context_sessions[1]
 WHEN own.context_sessions IS NULL THEN coalesce(k.session_id,r.session_id) END AS session_id,
  json_extract_string(own.executor_id,'$[1]') AS agent_id, coalesce(k.tool_use_id, r.tool_use_id) AS tool_use_id,
  CASE WHEN NOT coalesce(states.identity_conflict,false) THEN k.message_id END AS message_id,
  coalesce(states.identity_conflict,false) AS message_identity_conflict,
  k.line_no_call, r.line_no_result, k.ts_call, r.ts_result,
  CASE WHEN try_cast(r.ts_result AS TIMESTAMPTZ) >= try_cast(k.ts_call AS TIMESTAMPTZ)
       THEN CAST(date_diff('millisecond', try_cast(k.ts_call AS TIMESTAMPTZ), try_cast(r.ts_result AS TIMESTAMPTZ)) AS BIGINT)
       WHEN try_cast(r.ts_result AS TIMESTAMPTZ) IS NULL OR try_cast(k.ts_call AS TIMESTAMPTZ) IS NULL
       THEN CASE WHEN k.source_file = r.source_file AND r.latency_ms >= 0 THEN r.latency_ms END END AS latency_ms,
  k.tool, k.mcp_server, k.mcp_tool, k.command, k.file_path, k.subagent_type, k.skill_arg, k.description,
  k.input_len, coalesce(k.is_sidechain, r.is_sidechain) AS is_sidechain, k.attribution_skill, k.entrypoint,
  coalesce(k.cwd, r.cwd) AS cwd, coalesce(k.git_branch, r.git_branch) AS git_branch,
  coalesce(r.has_result, false) AS has_result, r.is_error, r.error_source, r.error_class, r.error_text,
  r.tool_use_result_text, r.result_len, r.exit_code, r.interrupted, r.denial_kind,
  r.hook_event, r.hook_tool, r.hook_command, r.hook_script,
  list_bool_or([k.call_scrub_failed, env.result_scrub_failed, r.scrub_failed, k.scrub_failed]) AS scrub_failed,
  r.source_file AS result_source_file, k.call_uuid,
  own.executor_id, own.attribution_status, own.attribution_reason, own.context_sessions,
  lineage.parent_actor_id AS delegator_id, lineage.lineage_status,
  (k.call_content_conflict IS FALSE AND states.content_conflict IS FALSE) AS call_valid,
  (env.result_content_conflict IS FALSE AND r.has_result IS TRUE) AS result_valid,
  CASE WHEN k.call_key IS NULL THEN FALSE ELSE k.call_content_conflict END AS content_conflict,
  CASE WHEN env.envelope_key IS NULL THEN FALSE ELSE env.result_content_conflict END AS result_conflict,
  list_sort(list_distinct(list_concat(coalesce(k.call_references, []), coalesce(env.result_references, [])))) AS source_references
FROM calls k FULL OUTER JOIN results r ON r.call_key = k.call_key
LEFT JOIN envelopes env ON env.envelope_key = coalesce(k.call_key, r.call_key, r.result_key)
LEFT JOIN tool_ownership own ON own.tool_use_id=k.tool_use_id AND own.source_file=k.source_file
LEFT JOIN event_states states ON states.event_key=coalesce('uuid:' || k.call_uuid,'local:' || k.source_file || ':' || k.line_no_call)
LEFT JOIN actor_lineage lineage ON lineage.actor_id=own.executor_id;

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
 CASE WHEN bool_and(content_conflict IS FALSE) AND NOT bool_or(identity_conflict) AND bool_and(assembly_complete) THEN CAST(sum(n_tool_use) AS DECIMAL(38,0)) END AS n_tool_use,bool_or(is_sidechain) AS is_sidechain,
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
 (g.assembly_complete AND e.content_conflict IS FALSE AND NOT e.state_conflict
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
 SELECT event_key,CASE WHEN count(DISTINCT record_hash)>1 THEN TRUE
 WHEN count(*) FILTER (WHERE record_hash IS NULL)>0 THEN NULL ELSE FALSE END AS content_conflict,
 CASE WHEN count(DISTINCT actor_id)=1 AND count(actor_id)=count(*)
 AND count(DISTINCT record_hash)=1 AND count(record_hash)=count(*) AND NOT bool_or(context_conflict)
 THEN any_value(actor_id ORDER BY actor_id) END AS executor_id,
 list(DISTINCT context_session_id ORDER BY context_session_id) AS context_sessions,
 bool_or(context_conflict) AS context_conflict,
 list(DISTINCT struct_pack(source_file:=source_file,line_no:=line_no)
 ORDER BY struct_pack(source_file:=source_file,line_no:=line_no)) AS source_references,
 -- Dedup keeps row 1's columns; scrub_failed must survive any kept row (#21).
 bool_or(scrub_failed) AS scrub_failed
 FROM keyed GROUP BY event_key
)
SELECT k.* EXCLUDE(event_key,agent_id,session_id,scrub_failed),s.content_conflict,s.scrub_failed,s.source_references,
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
 SELECT event_key,CASE WHEN count(DISTINCT record_hash)>1 THEN TRUE
 WHEN count(*) FILTER (WHERE record_hash IS NULL)>0 THEN NULL ELSE FALSE END AS content_conflict,
 CASE WHEN count(DISTINCT actor_id)=1 AND count(actor_id)=count(*)
 AND count(DISTINCT record_hash)=1 AND count(record_hash)=count(*) AND NOT bool_or(context_conflict)
 THEN any_value(actor_id ORDER BY actor_id) END AS executor_id,
 list(DISTINCT context_session_id ORDER BY context_session_id) AS context_sessions,
 bool_or(context_conflict) AS context_conflict,
 list(DISTINCT struct_pack(source_file:=source_file,line_no:=line_no)
 ORDER BY struct_pack(source_file:=source_file,line_no:=line_no)) AS source_references,
 -- Dedup keeps row 1's columns; scrub_failed must survive any kept row (#21).
 bool_or(scrub_failed) AS scrub_failed
 FROM keyed GROUP BY event_key
)
SELECT k.* EXCLUDE(event_key,agent_id,session_id,scrub_failed),s.content_conflict,s.scrub_failed,s.source_references,
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
-- Historical adapters leave actor_id unknown; unknown actors must not be
-- merged into one row, so unknown identities partition on a source-local key.
WITH keyed AS (
 SELECT *,coalesce(actor_id,'unknown:' || to_json(struct_pack(source_file:=source_file,
   agent_id:=agent_id,tool_use_id:=tool_use_id,agent_type:=agent_type,description:=description))) AS dedup_key
 FROM raw_agents
)
SELECT a.* EXCLUDE (rn,dedup_key,scrub_failed),s.scrub_failed FROM (
 SELECT *,row_number() OVER(PARTITION BY dedup_key ORDER BY source_file) AS rn FROM keyed) a
LEFT JOIN (SELECT dedup_key,bool_or(scrub_failed) AS scrub_failed FROM keyed GROUP BY 1) s
ON s.dedup_key = a.dedup_key
WHERE rn = 1;

CREATE OR REPLACE VIEW sessions AS
WITH keys AS (
  SELECT session_id FROM turns UNION
  SELECT session_id FROM tool_calls UNION
  SELECT session_id FROM hooks UNION
  SELECT session_id FROM events UNION
  SELECT session_id FROM session_meta UNION
  SELECT session_id FROM agents
), t AS (
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
SELECT k.session_id, t.first_ts, t.last_ts,
       coalesce(t.n_turns,0) AS n_turns, coalesce(t.n_turns_count_uncertain,0) AS n_turns_count_uncertain,
       coalesce(t.n_main_turns,0) AS n_main_turns, coalesce(t.n_unattributed_turns,0) AS n_unattributed_turns,
       coalesce(t.n_subagents,0) AS n_subagents, t.output_tokens, t.input_tokens, t.cache_read, t.cache_create,
       coalesce(t.n_turns_no_usage,0) AS n_turns_no_usage, t.models, t.cwd, t.git_branch,
       coalesce(c.n_tool_calls, 0) AS n_tool_calls, coalesce(c.n_tool_calls_excluded,0) AS n_tool_calls_excluded,
       coalesce(c.n_errors_flagged, 0) AS n_errors_flagged,
       coalesce(c.n_errors_text, 0) AS n_errors_text, coalesce(c.n_hook_blocks, 0) AS n_hook_blocks,
       m.ai_title, m.custom_title, m.pr_numbers, m.pr_links, s.scrub_failed
FROM keys k LEFT JOIN t ON t.session_id IS NOT DISTINCT FROM k.session_id
LEFT JOIN c ON c.session_id IS NOT DISTINCT FROM k.session_id
LEFT JOIN m ON m.session_id IS NOT DISTINCT FROM k.session_id
LEFT JOIN s ON s.session_id IS NOT DISTINCT FROM k.session_id;

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
SELECT 'turn_content' AS metric,count(*) FILTER(WHERE content_conflict IS FALSE) AS included,
 count(*) FILTER(WHERE content_conflict IS NOT FALSE) AS excluded FROM turns
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
UNION ALL SELECT 'hook_content',count(*) FILTER(WHERE content_conflict IS FALSE),count(*) FILTER(WHERE content_conflict IS NOT FALSE) FROM hooks
UNION ALL SELECT 'event_content',count(*) FILTER(WHERE content_conflict IS FALSE),count(*) FILTER(WHERE content_conflict IS NOT FALSE) FROM events
UNION ALL SELECT 'tool_result_content',count(*) FILTER(WHERE result_valid),count(*) FILTER(WHERE NOT result_valid) FROM tool_calls
UNION ALL SELECT 'spawn_association',count(*) FILTER(WHERE association_status IN ('confirmed','inferred')),
 count(*) FILTER(WHERE association_status NOT IN ('confirmed','inferred')) FROM spawn_associations;
"""


_VIEWS = _TOOL_VIEWS + _TURN_VIEWS + _SOURCE_VIEWS + _COVERAGE_VIEWS
