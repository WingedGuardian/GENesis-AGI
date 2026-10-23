"""Bounded, scrubbed evidence from live sources or local encrypted archives."""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from . import scrub
from .query import run_query

_MAX_RECORD_BYTES = 1024 * 1024


def _target_available(records, line, expected_id):
    target = next((record for record in records if record["line"] == line), None)
    if target is None or target.get("scrub_failed") or not target["text"]:
        return False
    if expected_id is None:
        return True
    token = json.dumps(expected_id, ensure_ascii=True)
    return any(f'"{key}": {token}' in target["text"] for key in ("id", "tool_use_id"))


def _result_envelope(record, blocks):
    results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
    denial = record.get("toolDenialKind")
    return {
        "toolUseResult": record.get("toolUseResult") if len(results) == 1 else None,
        "toolDenialKind": denial if isinstance(denial, str) else None,
    }


def _observation_hash(record, blocks, block):
    from .extract import _hash

    if block["type"] == "tool_use":
        return _hash(block)
    envelope = _result_envelope(record, blocks)
    return _hash(
        {
            "block": block,
            "toolUseResult": envelope["toolUseResult"],
            "denial_kind": envelope["toolDenialKind"],
        }
    )


def _matching_blocks(record, blocks, expected_id, expected_hash, expected_kind):
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in ("tool_use", "tool_result"):
            continue
        kind = "call" if block["type"] == "tool_use" else "result"
        key = "id" if kind == "call" else "tool_use_id"
        if block.get(key) != expected_id or (expected_kind and expected_kind != kind):
            continue
        digest = _observation_hash(record, blocks, block)
        if expected_hash is None or digest == expected_hash:
            yield digest, block


def _matching_record(raw, expected_id, expected_hash=None, expected_kind=None):
    try:
        record, duplicates = scrub.load_json(raw)
        if duplicates:
            return raw, False
        blocks = record.get("message", {}).get("content", [])
        if not isinstance(blocks, list):
            return raw, False
        matches = dict(_matching_blocks(record, blocks, expected_id, expected_hash, expected_kind))
        if len(matches) != 1:
            return raw, False
        block = next(iter(matches.values()))
        key = "id" if block["type"] == "tool_use" else "tool_use_id"
        block = {
            "type": block["type"],
            key: expected_id,
            **{k: v for k, v in block.items() if k not in ("type", key)},
        }
        selected = {"message": {"content": [block]}}
        if block["type"] == "tool_result":
            selected.update({k: v for k, v in _result_envelope(record, blocks).items() if v is not None})
        return (json.dumps(selected, ensure_ascii=True) + "\n").encode(), True
    except (ValueError, AttributeError, TypeError, UnicodeError, RecursionError):
        return raw, False


def _scrub_record(raw):
    """Retain original JSON only when its bytes are valid UTF-8 JSON."""
    try:
        decoded, duplicates = scrub.load_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        try:
            plain = raw.decode("utf-8")
        except UnicodeError:
            return None, True
        if "\x00" in plain or plain.removeprefix("\ufeff").lstrip().startswith(("{", "[")):
            return None, True
        return scrub.scrub_text(plain)
    if duplicates:
        return None, True
    text, failed = scrub.scrub_json(decoded, preserve_identity=True)
    try:
        unchanged = not failed and not duplicates and json.loads(text) == decoded
    except (ValueError, TypeError, RecursionError):
        return None, True
    if unchanged:
        try:
            original = raw.decode("utf-8")
            # BOM-less UTF-16/32 ASCII can decode as UTF-8 but contains NULs.
            json.loads(original.removeprefix("\ufeff"))
        except (ValueError, UnicodeError, RecursionError):
            pass  # The validated scrubbed JSON is already safe UTF-8 text.
        else:
            text = original
    return text, failed


def _bounded_lines(stream, last_line, source_bytes):
    """Yield physical lines without allocating an oversized record or following appends."""
    remaining = source_bytes
    for number in range(1, last_line + 1):
        if remaining is not None and remaining <= 0:
            break
        limit = _MAX_RECORD_BYTES + 1
        raw = stream.readline(limit if remaining is None else min(limit, remaining))
        if not raw:
            break
        if remaining is not None:
            remaining -= len(raw)
        oversized = len(raw) > _MAX_RECORD_BYTES
        if oversized:
            while not raw.endswith(b"\n") and (remaining is None or remaining > 0):
                raw = stream.readline(_MAX_RECORD_BYTES if remaining is None else min(_MAX_RECORD_BYTES, remaining))
                if remaining is not None:
                    remaining -= len(raw)
                if not raw:
                    break
        yield number, None if oversized else raw


def _window(
    stream, line, surrounding, budget, expected_id=None, expected_hash=None, expected_kind=None,
    *, source_bytes=None,
):
    records, retained = [], 0
    dropped = False
    identity_matches = expected_id is None
    for number, raw in _bounded_lines(stream, line + surrounding, source_bytes):
        if number < max(1, line - surrounding):
            continue
        if raw is None:
            if number == line:
                return {"unavailable": "source record exceeds the 1 MiB evidence read limit",
                        "records": [], "target_available": False}
            text, failed = "[oversized context record: evidence withheld]", False
        else:
            if number == line and expected_id is not None:
                raw, identity_matches = _matching_record(raw, expected_id, expected_hash, expected_kind)
            text, failed = _scrub_record(raw)
            if failed:
                text = "[scrub failed: evidence withheld]"
        encoded = (text or "").encode()
        record = {"line": number, "text": encoded[:budget].decode("utf-8", "ignore"),
                  "truncated": raw is None or len(encoded) > budget, "scrub_failed": failed}
        cost = len(json.dumps(record, ensure_ascii=False).encode())
        heapq.heappush(records, (-abs(number - line), -number, cost, record))
        retained += cost
        while len(records) > 1 and retained > max(budget, _MAX_RECORD_BYTES):
            retained -= heapq.heappop(records)[2]
            dropped = True
    if not identity_matches:
        return {
            "unavailable": "target line no longer matches the referenced tool use",
            "records": [],
            "target_available": False,
        }
    return _fit_window([entry[3] for entry in records], line, budget, dropped, expected_id)


def _fit_window(records, line, budget, truncated, expected_id):
    selected = []
    for record in sorted(records, key=lambda r: (abs(r["line"] - line), r["line"])):
        encoded = record["text"].encode()
        if budget <= 0:
            truncated = True
            continue
        text = encoded[:budget].decode("utf-8", "ignore")
        clipped = record["truncated"] or len(encoded) > budget
        selected.append(
            {
                "line": record["line"],
                "text": text,
                "truncated": clipped,
                "scrub_failed": record["scrub_failed"],
            }
        )
        truncated |= clipped
        budget -= len(text.encode())
    selected.sort(key=lambda r: r["line"])
    return {
        "records": selected,
        "truncated": truncated,
        "target_available": _target_available(selected, line, expected_id),
    }


def _archive_window(
    archive,
    relative,
    line,
    surrounding,
    budget,
    expected_id=None,
    expected_hash=None,
    expected_kind=None,
):
    password = os.environ.get("GENESIS_BACKUP_PASSPHRASE")
    if not password:
        return {"unavailable": "local encrypted archive requires GENESIS_BACKUP_PASSPHRASE"}
    if "\n" in password or "\0" in password:
        return {"unavailable": "backup passphrase must be a single line without a terminator"}
    from genesis.guardian.cred_integrity import decrypt_backup_stream

    with decrypt_backup_stream(archive, password) as plaintext, tarfile.open(
        fileobj=plaintext, mode="r|"
    ) as tar:
        member = next(iter(tar), None)
        if member is None or not member.isfile() or member.name != relative:
            raise ValueError("archive member does not match source")
        with tar.extractfile(member) as stream:
            result = _window(
                stream, line, surrounding, budget, expected_id, expected_hash, expected_kind,
                source_bytes=member.size,
            )
        if tar.next() is not None:
            raise ValueError("archive has multiple members")
    # Context exit drains the trailer and verifies authentication before any
    # tentative evidence can be returned to the caller.
    return {**result, "origin": "local encrypted archive"}


def read_reference(
    projects,
    relative,
    line,
    *,
    surrounding=5,
    budget=65536,
    archive_dir=None,
    expected_id=None,
    expected_hash=None,
    expected_kind=None,
):
    from .store import _open_regular, source_path

    try:
        relative = source_path(relative)
        path = PurePosixPath(relative)
    except (TypeError, ValueError):
        return {"unavailable": "invalid source reference"}
    if path.is_absolute() or ".." in path.parts or not path.parts or line < 1:
        return {"unavailable": "invalid source reference"}
    target = projects / relative
    live_unavailable = None
    try:
        if not target.resolve().is_relative_to(projects.resolve()) or target.is_symlink():
            return {"unavailable": "source resolves outside projects or is a symlink"}
        with _open_regular(target) as stream:
            result = _window(
                stream, line, surrounding, budget, expected_id, expected_hash, expected_kind,
                source_bytes=os.fstat(stream.fileno()).st_size,
            )
            if "unavailable" not in result:
                return {**result, "origin": "live"}
            # Other refusals retain the existing archive-fallback diagnostics.
            if result["unavailable"] == "source record exceeds the 1 MiB evidence read limit":
                live_unavailable = result
            raise FileNotFoundError("live evidence identity changed")
    except FileNotFoundError:
        archive_dir = archive_dir or Path.home() / "backups/genesis-backups/transcripts"
        archive = archive_dir / f"v2-{hashlib.sha256(os.fsencode(relative)).hexdigest()}.tar.gpg"
        if not archive.is_file():
            return live_unavailable or {"unavailable": "original target and local encrypted archive unavailable"}
        try:
            return _archive_window(
                archive,
                relative,
                line,
                surrounding,
                budget,
                expected_id,
                expected_hash,
                expected_kind,
            )
        except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
            return live_unavailable or {"unavailable": "local archive unreadable or invalid"}
    except (OSError, ValueError, RuntimeError):
        return {"unavailable": "source unreadable or not a regular file"}


def _clip_json_text(text, budget):
    """Largest character prefix whose JSON string payload fits the byte budget."""
    lower, upper = 0, len(text)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        cost = len(json.dumps(text[:middle], ensure_ascii=False).encode()) - 2
        if cost <= budget:
            lower = middle
        else:
            upper = middle - 1
    return text[:lower]


def _compact_failure_markers(out):
    """Preserve withholding markers while omitting redundant healthy flags."""
    for ref in out.get("references", []):
        for record in ref.get("records", []):
            if record.get("scrub_failed") is False:
                record.pop("scrub_failed")


def _fit_output(out, budget):
    """Discard distant context first, then share target text space fairly."""

    _compact_failure_markers(out)

    def size():
        return len(json.dumps(out, ensure_ascii=False).encode())

    while size() > budget:
        context = [
            (ref, record)
            for ref in out.get("references", [])
            for record in ref.get("records", [])
            if record["line"] != ref["line"]
        ]
        if not context:
            break
        ref, record = max(context, key=lambda pair: abs(pair[1]["line"] - pair[0]["line"]))
        ref["records"].remove(record)
        ref["truncated"] = True
        out["truncated"] = True
    if size() <= budget:
        return out
    targets = [
        (ref, record, record["text"])
        for ref in out.get("references", [])
        for record in ref.get("records", [])
    ]
    out["truncated"] = True
    for ref, record, _ in targets:
        record["text"] = ""
        ref["truncated"] = True
    available = budget - size()
    if available < 0 or not targets:
        withheld = {"truncated": True, "unavailable": out.get(
            "unavailable", "references exceed evidence byte budget"
        )}
        for key in ("reference_count", "candidate_count"):
            if key in out:
                withheld[key] = out[key]
        return withheld
    # Short targets release unused space to longer targets; JSON escaping counts.
    targets.sort(key=lambda item: len(json.dumps(item[2], ensure_ascii=False).encode()))
    for index, (_, record, text) in enumerate(targets):
        clipped = _clip_json_text(text, available // (len(targets) - index))
        record["text"] = clipped
        record["truncated"] |= clipped != text
        available -= len(json.dumps(clipped, ensure_ascii=False).encode()) - 2
    return out


_ATTRIBUTION_FIELDS = (
    "executor_id",
    "attribution_status",
    "attribution_reason",
    "delegator_id",
    "lineage_status",
    "context_sessions",
    "content_conflict",
    "result_conflict",
    "call_valid",
    "result_valid",
)


def _coverage(row, count):
    return {
        "attribution": dict(zip(_ATTRIBUTION_FIELDS, row[5:], strict=True)),
        "parent_links": {
            "view": "actor_lineage",
            "actor_id": row[5],
            "raw_context_verified": False,
        },
        "reference_count": count,
    }


def _observations(rows):
    observations = set()
    for relative, call_line, result_line, call_hash, result_hash, *_ in rows:
        for kind, line, digest in (
            ("call", call_line, call_hash),
            ("result", result_line, result_hash),
        ):
            if relative and line is not None:
                observations.add((kind, relative, line, digest))
    return sorted(observations, key=lambda v: (v[0], v[1], v[2], v[3] or ""))


def _read_observation(projects, observation, raw_id, surrounding, budget):
    kind, relative, line, digest = observation
    ref = {"kind": kind, "source": relative, "line": line}
    if digest is None:
        return {**ref, "unavailable": "observation content hash absent"}
    return {
        **ref,
        **read_reference(
            projects,
            relative,
            line,
            surrounding=surrounding,
            budget=budget,
            expected_id=raw_id,
            expected_hash=digest,
            expected_kind=kind,
        ),
    }


def evidence(data, projects, tool_use_id, *, surrounding=5, budget=65536):
    if surrounding < 0 or budget < 1024:
        raise ValueError("evidence requires nonnegative context and at least 1024 bytes")
    from .identity import decode_identity

    raw_id = decode_identity(tool_use_id)
    _, rows = run_query(
        data,
        "WITH selected AS (SELECT *,count(*) OVER () AS candidate_count "
        "FROM tool_calls WHERE tool_use_id=?) "
        "SELECT r.source_file,r.line_no_call,r.line_no_result,r.call_hash,r.result_hash,"
        "c.executor_id,c.attribution_status,c.attribution_reason,c.delegator_id,"
        "c.lineage_status,c.context_sessions,c.content_conflict,c.result_conflict,"
        "c.call_valid,c.result_valid,c.candidate_count FROM selected c LEFT JOIN raw_tool_calls r "
        "ON c.tool_use_id=r.tool_use_id AND list_contains(c.source_references,"
        "struct_pack(source_file:=r.source_file,line_no_call:=r.line_no_call,"
        "line_no_result:=r.line_no_result)) ORDER BY r.source_file,"
        "r.line_no_call,r.line_no_result,r.call_hash,r.result_hash",
        [tool_use_id],
        live=True,
    )
    out = {"tool_use_id": tool_use_id, "references": [], "truncated": False}
    if not rows:
        out["unavailable"] = "tool use ID absent from query coverage"
        return _fit_output(out, budget)
    if rows[0][-1] > 1:
        out.update(
            candidate_count=rows[0][-1],
            unavailable="tool use ID ambiguous; inspect source_references with live SQL",
        )
        return _fit_output(out, budget)
    observations = _observations(rows)
    out.update(_coverage(rows[0][:-1], len(observations)))
    kinds = {observation[0] for observation in observations}
    text_budget = budget // max(3, len(observations))
    for observation in observations:
        ref = _read_observation(projects, observation, raw_id, surrounding, text_budget)
        if "records" in ref:
            ref["target_available"] = False  # Reserve the longer boolean before fitting.
        out["references"].append(ref)
        out["truncated"] |= ref.get("truncated", False)
    for kind in ("call", "result"):
        if kind not in kinds:
            out["references"].append({"kind": kind, "unavailable": "reference absent"})
    out = _fit_output(out, budget)
    for ref in out.get("references", []):
        if "records" in ref:
            ref["target_available"] = _target_available(ref["records"], ref["line"], raw_id)
    return out
