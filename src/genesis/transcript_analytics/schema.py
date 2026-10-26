"""Explicit Parquet schemas, one per table.

Explicit (not inferred) so that an empty table still writes a file with the
right columns, and so every source's file for a table is union-compatible.
Bump ``SCHEMA_VERSION`` whenever a column is added, removed or retyped: the
staleness check rebuilds every source written under an older version.
"""

import pyarrow as pa

SCHEMA_VERSION = "5"

S, INT, B = pa.string(), pa.int64(), pa.bool_()

_COMMON = [
    ("source_file", S),
    ("session_id", S),
    ("agent_id", S),
    ("record_session_id", S),
    ("context_session_id", S),
    ("actor_id", S),
    ("source_role", S),
    ("context_conflict", B),
]

SCHEMAS: dict[str, pa.Schema] = {
    "tool_calls": pa.schema(
        _COMMON
        + [
            ("tool_use_id", S),
            ("message_id", S),
            ("line_no_call", INT),
            ("call_uuid", S),
            ("call_hash", S),
            ("result_hash", S),
            ("result_agent_id", S),
            ("result_agent_ids", pa.list_(S)),
            ("line_no_result", INT),
            ("ts_call", S),
            ("ts_result", S),
            ("latency_ms", INT),
            ("tool", S),
            ("mcp_server", S),
            ("mcp_tool", S),
            ("command", S),
            ("file_path", S),
            ("subagent_type", S),
            ("skill_arg", S),
            ("description", S),
            ("input_len", INT),
            ("is_sidechain", B),
            ("attribution_skill", S),
            ("entrypoint", S),
            ("cwd", S),
            ("git_branch", S),
            ("has_result", B),
            ("is_error", B),
            ("error_source", S),
            ("error_class", S),
            ("error_text", S),
            ("tool_use_result_text", S),
            ("result_len", INT),
            ("exit_code", INT),
            ("interrupted", B),
            ("denial_kind", S),
            ("hook_event", S),
            ("hook_tool", S),
            ("hook_command", S),
            ("hook_script", S),
            ("scrub_failed", B),
        ]
    ),
    "fragments": pa.schema(
        _COMMON
        + [
            ("line_no", INT),
            ("uuid", S),
            ("message_id", S),
            ("request_id", S),
            ("request_id_hash", S),
            ("content_hash", S),
            ("payload_hash", S),
            ("immutable_hash", S),
            ("usage_hash", S),
            ("terminal_hash", S),
            ("has_usage", B),
            ("ts", S),
            ("model", S),
            ("stop_reason", S),
            ("input_tokens", INT),
            ("output_tokens", INT),
            ("cache_read", INT),
            ("cache_create", INT),
            ("cache_create_5m", INT),
            ("cache_create_1h", INT),
            ("thinking_tokens", INT),
            ("is_sidechain", B),
            ("entrypoint", S),
            ("attribution_skill", S),
            ("attribution_mcp_server", S),
            ("attribution_mcp_tool", S),
            ("effort", S),
            ("is_api_error", B),
            ("api_error_status", INT),
            ("cwd", S),
            ("git_branch", S),
            ("version", S),
            ("n_tool_use", INT),
            ("has_text", B),
            ("has_thinking", B),
            ("scrub_failed", B),
        ]
    ),
    "hooks": pa.schema(
        _COMMON
        + [
            ("line_no", INT),
            ("uuid", S),
            ("ts", S),
            ("kind", S),
            ("record_hash", S),
            ("hook_event", S),
            ("hook_name", S),
            ("command", S),
            ("exit_code", INT),
            ("duration_ms", INT),
            ("timed_out", B),
            ("tool_use_id", S),
            ("content_len", INT),
            ("stdout_len", INT),
            ("stderr_len", INT),
            ("scrub_failed", B),
        ]
    ),
    "events": pa.schema(
        _COMMON
        + [
            ("line_no", INT),
            ("uuid", S),
            ("ts", S),
            ("subtype", S),
            ("record_hash", S),
            ("level", S),
            ("duration_ms", INT),
            ("message_count", INT),
            ("status", INT),
            ("pre_tokens", INT),
            ("post_tokens", INT),
            ("trigger", S),
            ("detail", S),
            ("scrub_failed", B),
        ]
    ),
    "session_meta": pa.schema(
        [
            ("source_file", S),
            ("line_no", INT),
            ("session_id", S),
            ("kind", S),
            ("value", S),
            ("pr_repository", S),
            ("ts", S),
            ("scrub_failed", B),
        ]
    ),
    "agents": pa.schema(
        [
            ("source_file", S),
            ("agent_id", S),
            ("session_id", S),
            ("parent_agent_id", S),
            ("context_session_id", S),
            ("actor_id", S),
            ("agent_type", S),
            ("description", S),
            ("tool_use_id", S),
            ("spawn_depth", INT),
            ("request_shape", S),
            ("non_interactive", B),
            ("scrub_failed", B),
        ]
    ),
}
