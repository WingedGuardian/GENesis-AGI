"""Turn ONE Claude Code transcript file into table rows.

Every rule here is a measured fact about the transcript format (plan §2-§14,
2026-10-04), not an assumption:

* Pairing is per file; derived views reconcile cross-source copies separately.
* One assistant message is split across several records (one per content
  block); each record becomes a ``fragments`` row and ``turns`` is derived in
  a view after identity/state and ordered-sequence reconciliation.
* ``is_error`` is tri-state; absent is not success. Failure text is classified
  from the CONTENT block; ``toolUseResult`` is kept only when it adds
  information beyond ``"Error: " + content``.
* ``<synthetic>`` records have no provider usage; API-error records are retained.
* Only complete lines are read, up to a byte limit taken before reading, so a
  file still being appended never yields a torn record.
* Only success LENGTHS are stored; text is stored for failures only, scrubbed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from genesis.transcript_analytics.classify import _hook_script, classify, parse_hook_block
from genesis.transcript_analytics.identity import encode_identity, normalize_rows
from genesis.transcript_analytics.scrub import scrub_json, scrub_text

TABLES = ("tool_calls", "fragments", "hooks", "events", "session_meta", "agents")

# These are user/configuration supplied labels as well as free text. Identity
# fields and source_file stay exact so joins and raw evidence remain addressable.
_TEXT_FIELDS = frozenset(
    {
        "ts",
        "ts_call",
        "ts_result",
        "file_path",
        "cwd",
        "git_branch",
        "subagent_type",
        "skill_arg",
        "attribution_skill",
        "attribution_mcp_server",
        "attribution_mcp_tool",
        "mcp_server",
        "mcp_tool",
        "tool",
        "agent_type",
        "request_shape",
        "hook_name",
        "pr_repository",
        "entrypoint",
        "model",
        "version",
        "effort",
        "value",
        "denial_kind",
        "level",
        "error_class",
        "kind",
        "hook_event",
        "hook_tool",
        "hook_script",
        "stop_reason",
        "trigger",
    }
)

_EVENT_SUBTYPES = frozenset(
    {
        "turn_duration",
        "stop_hook_summary",
        "api_error",
        "compact_boundary",
        "model_refusal_fallback",
        "informational",
    }
)
_META_KINDS = {
    "custom-title": "customTitle",
    "ai-title": "aiTitle",
    "permission-mode": "permissionMode",
    "mode": "mode",
    "agent-color": "agentColor",
}


@dataclass
class ExtractResult:
    tables: dict[str, list[dict]] = field(default_factory=lambda: {t: [] for t in TABLES})
    content_sha256: str = ""
    last_timestamp: str | None = None
    chronology_known: bool = True
    stats: dict[str, int] = field(
        default_factory=lambda: {"lines": 0, "malformed": 0, "bytes_read": 0}
    )


def utc_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _ms_between(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        da = datetime.fromisoformat(a.replace("Z", "+00:00"))
        db = datetime.fromisoformat(b.replace("Z", "+00:00"))
        return round((db - da).total_seconds() * 1000)
    except (ValueError, TypeError):  # TypeError: naive minus aware timestamp
        return None


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def _result_agent_ids(content, structured) -> list[str]:
    """Retain every recognized ID before association, preserving text boundaries."""
    candidates = set()
    if isinstance(structured, dict):
        value = _str(structured.get("agentId"))
        if value:
            candidates.add(value)
    texts = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        texts.extend(part["text"] for part in content if isinstance(part, dict)
                     and part.get("type") == "text" and isinstance(part.get("text"), str))
    if isinstance(structured, str):
        texts.append(structured)
    for text in texts:
        candidates.update(match.group(1) for match in re.finditer(
            r"(?:^|\n)agentId: ([A-Za-z0-9_-]+)(?=\s|$)", text
        ))
    return sorted(candidates)


def _complete_lines(path: Path, stop_at: int | None, counter: dict):
    """Yield complete lines (newline stripped) within the first ``stop_at`` bytes.

    Streams one line at a time (a 400 MB transcript is never held whole). A
    trailing line with no newline is still being written and is left for the
    next read; ``stop_at`` is the size observed BEFORE reading, so bytes appended
    during the read are not half-consumed. ``counter["consumed"]`` ends at the
    byte offset just past the last line yielded.
    """
    budget = os.path.getsize(path) if stop_at is None else stop_at
    with open(path, "rb") as fh:
        while True:
            remaining = budget - counter["consumed"]
            if remaining <= 0:
                return
            line = fh.readline(remaining)
            if "digest" in counter:
                counter["digest"].update(line)
            if not line or not line.endswith(b"\n") or counter["consumed"] + len(line) > budget:
                return
            counter["consumed"] += len(line)
            yield line[:-1]


def _str(v) -> str | None:
    return v if isinstance(v, str) else None


_INT64 = 2**63


def _int(v) -> int | None:
    # Out-of-range values are not representable in int64 Parquet: NULL, not a crash (review SF-4).
    return v if isinstance(v, int) and not isinstance(v, bool) and -_INT64 <= v < _INT64 else None


def _token(v) -> int | None:
    value = _int(v)
    return value if value is not None and value >= 0 else None


def _hash(value):
    """Compare complete producer payloads without persisting success text."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _message_state(record, message, *, immutable=False):
    # Source/context labels can be rewritten in inherited transcripts. These
    # behavioral flags alter extraction and cannot be discarded as placement.
    body = (
        {key: value for key, value in message.items() if key not in {"usage", "stop_reason"}}
        if immutable
        else message
    )
    return _hash(
        {
            "message": body,
            "is_api_error": record.get("isApiErrorMessage") is True,
            "api_error_status": _int(record.get("apiErrorStatus")),
        }
    )


def _request_state(record):
    value = record.get("requestId")
    return _hash({"unknown": True} if value is None else {"value": value})


def _terminal_state(record, message):
    # Distinct content blocks can legitimately share one terminal assertion.
    # All remaining message state and exact typed request evidence must agree.
    body = {key: value for key, value in message.items() if key != "content"}
    return _hash({"message_state": _message_state(record, body), "request": _request_state(record)})


def source_context(rel, record_sid=None):
    parts = Path(rel).parts
    if "subagents" in parts:
        index = parts.index("subagents")
        return parts[index - 1] if index else "source:" + rel
    return record_sid or "source:" + rel


def actor_identity(context, agent):
    return json.dumps(
        [encode_identity(context), encode_identity(agent) if agent is not None else None],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def extract_source(path: Path, rel: str, stop_at: int | None = None) -> ExtractResult:
    res = ExtractResult()
    t = res.tables
    pending: dict[str, list[dict]] = {}  # tool_use_id -> row awaiting its result
    agent_id_from_name = (
        path.name[len("agent-") : -len(".jsonl")] if path.name.startswith("agent-") else None
    )

    first_sid: str | None = None  # from ANY record, even ones no table keeps
    counter = {"consumed": 0, "digest": hashlib.sha256()}
    for line_no, raw in enumerate(_complete_lines(path, stop_at, counter), start=1):
        res.stats["lines"] += 1
        if not raw.strip():
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            res.chronology_known = False
            res.stats["malformed"] += 1
            continue
        if not isinstance(r, dict):
            res.chronology_known = False
            res.stats["malformed"] += 1
            continue

        # Retention sees every complete record, including types not persisted.
        timestamp = utc_timestamp(r.get("timestamp"))
        if timestamp is None:
            res.chronology_known = False
        elif res.last_timestamp is None or timestamp > utc_timestamp(res.last_timestamp):
            res.last_timestamp = timestamp.isoformat()

        rtype = _str(r.get("type"))
        sid = _str(r.get("sessionId")) or None
        if first_sid is None and sid:
            first_sid = sid
        ts = timestamp.isoformat().replace("+00:00", "Z") if timestamp is not None else None
        agent_id = _str(r.get("agentId")) or agent_id_from_name
        msg = r.get("message") if isinstance(r.get("message"), dict) else None
        context = source_context(rel, sid)
        physical_agent = agent_id_from_name
        common = {
            "source_file": rel,
            "session_id": sid or "source:" + rel,
            "record_session_id": sid,
            "context_session_id": context,
            "agent_id": agent_id,
            "actor_id": actor_identity(context, physical_agent),
            "source_role": "child" if physical_agent else "main",
            "context_conflict": bool(
                (physical_agent and sid and sid != context)
                or (_str(r.get("agentId")) and r.get("agentId") != physical_agent)
            ),
        }

        if rtype == "assistant" and msg is not None:
            if msg.get("model") == "<synthetic>" and r.get("isApiErrorMessage") is not True:
                continue
            usage = (
                msg.get("usage")
                if isinstance(msg.get("usage"), dict) and r.get("isApiErrorMessage") is not True
                else {}
            )
            cc = (
                usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
            )
            otd = (
                usage.get("output_tokens_details")
                if isinstance(usage.get("output_tokens_details"), dict)
                else {}
            )
            blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
            btypes = [b.get("type") for b in blocks if isinstance(b, dict)]
            t["fragments"].append(
                {
                    **common,
                    "line_no": line_no,
                    "uuid": _str(r.get("uuid")) or None,
                    "message_id": _str(msg.get("id")) or None,
                    "request_id": _str(r.get("requestId")) or None,
                    "request_id_hash": _request_state(r),
                    "content_hash": _hash(msg.get("content")),
                    "payload_hash": _message_state(r, msg),
                    "immutable_hash": _message_state(r, msg, immutable=True),
                    "usage_hash": _hash(msg.get("usage")),
                    "terminal_hash": _terminal_state(r, msg),
                    "has_usage": isinstance(msg.get("usage"), dict)
                    and r.get("isApiErrorMessage") is not True,
                    "ts": ts,
                    "model": _str(msg.get("model")),
                    # Preserve the producer assertion for whole-terminal agreement;
                    # has_usage and is_api_error independently prohibit its vector.
                    "stop_reason": _str(msg.get("stop_reason")),
                    "input_tokens": _token(usage.get("input_tokens")),
                    "output_tokens": _token(usage.get("output_tokens")),
                    "cache_read": _token(usage.get("cache_read_input_tokens")),
                    "cache_create": _token(usage.get("cache_creation_input_tokens")),
                    "cache_create_5m": _token(cc.get("ephemeral_5m_input_tokens")),
                    "cache_create_1h": _token(cc.get("ephemeral_1h_input_tokens")),
                    "thinking_tokens": _token(otd.get("thinking_tokens")),
                    "is_sidechain": bool(r.get("isSidechain")),
                    "entrypoint": _str(r.get("entrypoint")),
                    "attribution_skill": _str(r.get("attributionSkill")),
                    "attribution_mcp_server": _str(r.get("attributionMcpServer")),
                    "attribution_mcp_tool": _str(r.get("attributionMcpTool")),
                    "effort": _str(r.get("effort"))
                    if not isinstance(r.get("effort"), dict)
                    else None,
                    "is_api_error": r.get("isApiErrorMessage") is True,
                    "api_error_status": _int(r.get("apiErrorStatus")),
                    "cwd": _str(r.get("cwd")),
                    "git_branch": _str(r.get("gitBranch")),
                    "version": _str(r.get("version")),
                    "n_tool_use": btypes.count("tool_use"),
                    "has_text": "text" in btypes,
                    "has_thinking": "thinking" in btypes,
                }
            )
            for b in blocks:
                if (
                    not isinstance(b, dict)
                    or b.get("type") != "tool_use"
                    or not isinstance(b.get("id"), str)
                    or not b.get("id")
                ):
                    continue
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                name = _str(b.get("name"))
                mcp_server = mcp_tool = None
                if name and name.startswith("mcp__") and name.count("__") >= 2:
                    _, mcp_server, mcp_tool = name.split("__", 2)
                cmd, cmd_failed = scrub_text(_str(inp.get("command")))
                desc, desc_failed = scrub_text(_str(inp.get("description")))
                pending.setdefault(b["id"], []).append(
                    {
                        **common,
                        "tool_use_id": b["id"],
                        "message_id": _str(msg.get("id")) or None,
                        "line_no_call": line_no,
                        "call_uuid": _str(r.get("uuid")) or None,
                        "call_hash": _hash(b),
                        "line_no_result": None,
                        "ts_call": ts,
                        "ts_result": None,
                        "latency_ms": None,
                        "tool": name,
                        "mcp_server": mcp_server,
                        "mcp_tool": mcp_tool,
                        "command": cmd,
                        "file_path": _str(inp.get("file_path")),
                        "subagent_type": _str(inp.get("subagent_type")),
                        "skill_arg": _str(inp.get("skill")),
                        "description": desc,
                        "input_len": len(json.dumps(inp, ensure_ascii=False)),
                        "is_sidechain": bool(r.get("isSidechain")),
                        "attribution_skill": _str(r.get("attributionSkill")),
                        "entrypoint": _str(r.get("entrypoint")),
                        "cwd": _str(r.get("cwd")),
                        "git_branch": _str(r.get("gitBranch")),
                        "has_result": False,
                        "is_error": None,
                        "error_source": None,
                        "error_class": None,
                        "error_text": None,
                        "tool_use_result_text": None,
                        "result_len": None,
                        "exit_code": None,
                        "interrupted": None,
                        "denial_kind": None,
                        "hook_event": None,
                        "hook_tool": None,
                        "hook_command": None,
                        "hook_script": None,
                        "scrub_failed": cmd_failed or desc_failed,
                    }
                )
            continue

        if rtype == "user" and msg is not None and isinstance(msg.get("content"), list):
            result_blocks = [
                b for b in msg["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            tur = r.get("toolUseResult") if len(result_blocks) == 1 else None
            for b in result_blocks:
                tid = _str(b.get("tool_use_id"))
                if not tid:
                    continue
                call_rows = pending.pop(tid, [])
                row = call_rows[0] if call_rows else None
                if row is None:  # orphan result: its call is not in this file
                    row = {
                        **common,
                        "tool_use_id": tid,
                        "message_id": None,
                        "line_no_call": None,
                        "ts_call": None,
                        "tool": None,
                        "mcp_server": None,
                        "mcp_tool": None,
                        "command": None,
                        "file_path": None,
                        "subagent_type": None,
                        "skill_arg": None,
                        "description": None,
                        "input_len": None,
                        "is_sidechain": bool(r.get("isSidechain")),
                        "attribution_skill": None,
                        "entrypoint": None,
                        "cwd": _str(r.get("cwd")),
                        "git_branch": _str(r.get("gitBranch")),
                        "scrub_failed": False,
                        "hook_event": None,
                        "hook_tool": None,
                        "hook_command": None,
                        "hook_script": None,
                    }
                content = _block_text(b.get("content"))
                is_error = b["is_error"] if isinstance(b.get("is_error"), bool) else None
                denial = _str(r.get("toolDenialKind"))
                cls, source = classify(content, is_error=is_error, denial_kind=denial)
                failed = cls is not None
                err_text, err_failed = scrub_text(content) if failed else (None, False)
                tur_text = None
                if failed and isinstance(tur, str) and tur not in (content, "Error: " + content):
                    tur_text, tur_failed = scrub_text(tur)
                    err_failed = err_failed or tur_failed
                exit_code = None
                if cls == "exit_nonzero":
                    head = content.strip().split("\n", 1)[0]
                    try:
                        exit_code = _int(int(head.removeprefix("Exit code ").strip()))
                    except ValueError:
                        exit_code = None
                hook = parse_hook_block(content) if cls == "hook_block" else None
                hook_cmd, hook_failed = scrub_text(hook["hook_command"]) if hook else (None, False)
                result_agent_ids = _result_agent_ids(b.get("content"), tur)
                if hook:  # name the script from the SCRUBBED command only (review SF-1)
                    hook["hook_script"] = _hook_script(hook_cmd) if hook_cmd else None
                row.update(
                    {
                        "line_no_result": line_no,
                        "result_hash": _hash(
                            {"block": b, "toolUseResult": tur, "denial_kind": denial}
                        ),
                        "result_agent_ids": result_agent_ids,
                        "result_agent_id": result_agent_ids[0] if len(result_agent_ids) == 1 else None,
                        "ts_result": ts,
                        "latency_ms": _ms_between(row.get("ts_call"), ts),
                        "has_result": True,
                        "is_error": is_error,
                        "error_source": source,
                        "error_class": cls,
                        "error_text": err_text,
                        "tool_use_result_text": tur_text,
                        "result_len": (
                            len(content)
                            if isinstance(b.get("content"), str)
                            or (
                                isinstance(b.get("content"), list)
                                and all(
                                    isinstance(part, dict) and part.get("type") == "text"
                                    for part in b["content"]
                                )
                            )
                            else len(
                                json.dumps(
                                    b.get("content"), ensure_ascii=True, separators=(",", ":")
                                )
                            )
                        ),
                        "exit_code": exit_code,
                        "interrupted": tur.get("interrupted")
                        if isinstance(tur, dict) and isinstance(tur.get("interrupted"), bool)
                        else None,
                        "denial_kind": denial,
                        "hook_event": hook["hook_event"] if hook else None,
                        "hook_tool": hook["hook_tool"] if hook else None,
                        "hook_command": hook_cmd,
                        "hook_script": hook["hook_script"] if hook else None,
                        "scrub_failed": bool(row.get("scrub_failed")) or err_failed or hook_failed,
                    }
                )
                t["tool_calls"].append(row)
                for additional in call_rows[1:]:
                    result_fields = {
                        k: v
                        for k, v in row.items()
                        if k
                        in {
                            "line_no_result",
                            "ts_result",
                            "has_result",
                            "result_hash",
                            "result_agent_id",
                            "result_agent_ids",
                            "is_error",
                            "error_source",
                            "error_class",
                            "error_text",
                            "tool_use_result_text",
                            "result_len",
                            "exit_code",
                            "interrupted",
                            "denial_kind",
                            "hook_event",
                            "hook_tool",
                            "hook_command",
                            "hook_script",
                        }
                    }
                    additional.update(result_fields)
                    additional["latency_ms"] = _ms_between(additional.get("ts_call"), ts)
                    additional["scrub_failed"] |= row["scrub_failed"]
                    t["tool_calls"].append(additional)
            continue

        att = r.get("attachment")
        if (
            rtype == "attachment"
            and isinstance(att, dict)
            and str(att.get("type", "")).startswith("hook")
        ):
            hcmd, hfailed = scrub_text(_str(att.get("command")))
            content = att.get("content")
            t["hooks"].append(
                {
                    **common,
                    "line_no": line_no,
                    "uuid": _str(r.get("uuid")) or None,
                    "ts": ts,
                    "kind": att.get("type"),
                    "record_hash": _hash(att),
                    "hook_event": _str(att.get("hookEvent")),
                    "hook_name": _str(att.get("hookName")),
                    "command": hcmd,
                    "exit_code": _int(att.get("exitCode")),
                    "duration_ms": _int(att.get("durationMs")),
                    "timed_out": att.get("timedOut")
                    if isinstance(att.get("timedOut"), bool)
                    else None,
                    "tool_use_id": _str(att.get("toolUseID")),
                    "content_len": len(content)
                    if isinstance(content, str)
                    else (len(json.dumps(content)) if content is not None else 0),
                    "stdout_len": len(att["stdout"])
                    if isinstance(att.get("stdout"), str)
                    else None,
                    "stderr_len": len(att["stderr"])
                    if isinstance(att.get("stderr"), str)
                    else None,
                    "scrub_failed": hfailed,
                }
            )
            continue

        if rtype == "system" and _str(r.get("subtype")) in _EVENT_SUBTYPES:
            cm = r.get("compactMetadata") if isinstance(r.get("compactMetadata"), dict) else {}
            detail = {
                k: r.get(k)
                for k in (
                    "hookInfos",
                    "hookErrors",
                    "preventedContinuation",
                    "compactMetadata",
                    "content",
                    "error",
                    "level",
                )
                if r.get(k) is not None
            }
            dtext, dfailed = scrub_json(detail) if detail else (None, False)
            t["events"].append(
                {
                    **common,
                    "line_no": line_no,
                    "uuid": _str(r.get("uuid")) or None,
                    "ts": ts,
                    "subtype": r.get("subtype"),
                    "record_hash": _hash(
                        {
                            k: v
                            for k, v in r.items()
                            if k
                            not in {"sessionId", "agentId", "isSidechain", "uuid", "parentUuid"}
                        }
                    ),
                    "level": _str(r.get("level")),
                    "duration_ms": _int(r.get("durationMs")),
                    "message_count": _int(r.get("messageCount")),
                    "status": _int(r.get("status")),
                    "pre_tokens": _int(cm.get("preTokens")),
                    "post_tokens": _int(cm.get("postTokens")),
                    "trigger": _str(cm.get("trigger")),
                    "detail": dtext,
                    "scrub_failed": dfailed,
                }
            )
            continue

        if rtype in _META_KINDS and sid:
            val = r.get(_META_KINDS[rtype])
            if isinstance(val, str):
                val, val_failed = scrub_text(val)
                t["session_meta"].append(
                    {
                        "source_file": rel,
                        "line_no": line_no,
                        "session_id": sid,
                        "kind": rtype,
                        "value": val,
                        "scrub_failed": val_failed,
                        "pr_repository": None,
                        "ts": ts,
                    }
                )
        elif rtype == "pr-link" and sid and _int(r.get("prNumber")) is not None:
            t["session_meta"].append(
                {
                    "source_file": rel,
                    "line_no": line_no,
                    "session_id": sid,
                    "kind": "pr-link",
                    "value": str(r["prNumber"]),
                    "pr_repository": _str(r.get("prRepository")),
                    "ts": ts,
                }
            )

    res.content_sha256 = counter["digest"].hexdigest()
    res.stats["bytes_read"] = counter["consumed"]
    if counter["consumed"] != (path.stat().st_size if stop_at is None else stop_at):
        res.chronology_known = False  # Torn/truncated captured prefix has unknown chronology.
    t["tool_calls"].extend(
        row for calls in pending.values() for row in calls
    )  # calls whose result never arrived (yet)

    if agent_id_from_name:
        meta_path = path.with_name(f"agent-{agent_id_from_name}.meta.json")
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            meta = None
        if isinstance(meta, dict):
            description, desc_failed = scrub_text(_str(meta.get("description")))
            t["agents"].append(
                {
                    "source_file": rel,
                    "agent_id": agent_id_from_name,
                    "session_id": first_sid,
                    "parent_agent_id": _str(meta.get("parentAgentId")) or None,
                    "context_session_id": source_context(rel, first_sid),
                    "actor_id": actor_identity(source_context(rel, first_sid), agent_id_from_name),
                    "agent_type": _str(meta.get("agentType")) or _str(meta.get("taskKind")),
                    # Scrubbed like tool_calls.description (review N-4).
                    "description": description,
                    "scrub_failed": desc_failed,
                    "tool_use_id": _str(meta.get("toolUseId")) or None,
                    "spawn_depth": _int(meta.get("spawnDepth")),
                    "request_shape": _str(meta.get("requestShape")),
                    "non_interactive": meta.get("requestNonInteractive")
                    if isinstance(meta.get("requestNonInteractive"), bool)
                    else None,
                }
            )
    for rows in t.values():
        for row in rows:
            row.setdefault("scrub_failed", False)
            for key in _TEXT_FIELDS & row.keys():
                if isinstance(row[key], str):
                    row[key], failed = scrub_text(row[key])
                    if "scrub_failed" in row:
                        row["scrub_failed"] |= failed
    normalize_rows(t)
    return res
