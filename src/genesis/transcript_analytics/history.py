"""Read-only adapters for audited historical formats; never current semantics.

Audited producers: 27a1513b (flat schema 1), f693174b (schema 5 rework),
and 1a9c9c22 (#3002 schema 5). Original schemas, stamps and flags are retained.
"""

import pyarrow as pa
import pyarrow.parquet as pq

from .schema import SCHEMAS

# Exact producer signatures and scrub policies, not a schema-version wildcard.
_FORMATS = {
    (
        b"1",
        b"a61bb523389105b68dd9a7a29f461f0633b9572d66460de79e8f8bc4d0232a31",  # pragma: allowlist secret — historical extraction fingerprint
        b"b1501d5b2d7a",
    ): "flat-v1",
    (
        b"5",
        b"5c864e56212e8cd3970105505fc5fc4de7299558b4764f7b0edda5cfef1ce376",  # pragma: allowlist secret — historical extraction fingerprint
        b"8d476988235f:scoped-json-4",
    ): "record-v5",
    (
        b"5",
        b"8a232bd90fbc6680f2c726c6ec048e47ab1866492ccd47acabed02c8c75aef8d",  # pragma: allowlist secret — historical extraction fingerprint
        b"8d476988235f:scoped-json-4",
    ): "record-v5",
}
_IDENTITY = {
    "record_session_id",
    "context_session_id",
    "actor_id",
    "source_role",
    "context_conflict",
}
_MISSING = {
    "tool_calls": _IDENTITY
    | {"call_uuid", "call_hash", "result_hash", "result_agent_id", "result_agent_ids"},
    "fragments": _IDENTITY
    | {
        "request_id_hash",
        "content_hash",
        "payload_hash",
        "immutable_hash",
        "usage_hash",
        "terminal_hash",
        "has_usage",
        "scrub_failed",
    },
    "hooks": _IDENTITY | {"record_hash"},
    "events": _IDENTITY | {"record_hash"},
    "session_meta": {"scrub_failed"},
    "agents": {"parent_agent_id", "context_session_id", "actor_id", "scrub_failed"},
}


def format_for(metadata):
    return _FORMATS.get(tuple(metadata.get(k) for k in (b"ta.schema", b"ta.extract", b"ta.scrub")))


def schema_for(table, adapter):
    return pa.schema(
        [
            field
            for field in SCHEMAS[table]
            if adapter != "flat-v1" or field.name not in _MISSING[table]
        ]
    )


def unknown_facts(adapter):
    missing = (
        {table: sorted(fields) for table, fields in _MISSING.items()}
        if adapter == "flat-v1"
        else {}
    )
    return {
        "missing_columns": missing,
        "unrecoverable": [
            "stored text was scrubbed by the original policy, not the current policy",
            "result-ID scrub-failure evidence",
            "current hook-name and nonnegative-measure semantics",
            "refusal diagnostics omitted by the historical producer",
        ],
    }


def read_table(path, table, adapter):
    """Add typed nulls only; do not synthesize identities, evidence or success."""
    body = pq.ParquetFile(path).read()
    columns = [
        body[field.name]
        if field.name in body.column_names
        else pa.nulls(body.num_rows, type=field.type)
        for field in SCHEMAS[table]
    ]
    return pa.Table.from_arrays(columns, schema=SCHEMAS[table]).replace_schema_metadata(
        body.schema.metadata
    )
