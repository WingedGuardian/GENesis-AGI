"""Bounded, scrubbed evidence from live sources or local encrypted archives."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from . import scrub
from .query import run_query


def _target_available(records, line, expected_id):
    target = next((record for record in records if record["line"] == line), None)
    if target is None or not target["text"]:
        return False
    if expected_id is None:
        return True
    token = json.dumps(expected_id, ensure_ascii=True)
    return any(f'"{key}": {token}' in target["text"] for key in ("id", "tool_use_id"))


def _matching_record(raw, expected_id):
    try:
        record = json.loads(raw)
        blocks = record.get("message", {}).get("content", [])
        block = (
            next(
                (
                    b
                    for b in blocks
                    if isinstance(b, dict)
                    and b.get("type") in ("tool_use", "tool_result")
                    and b.get("id" if b["type"] == "tool_use" else "tool_use_id") == expected_id
                ),
                None,
            )
            if isinstance(blocks, list)
            else None
        )
        if block is None:
            return raw, False
        key = "id" if block["type"] == "tool_use" else "tool_use_id"
        block = {
            "type": block["type"],
            key: expected_id,
            **{k: v for k, v in block.items() if k not in ("type", key)},
        }
        return (
            json.dumps({"message": {"content": [block]}}, ensure_ascii=True) + "\n"
        ).encode(), True
    except (ValueError, AttributeError):
        return raw, False


def _scrub_record(raw):
    """Retain original JSON only when its bytes are valid UTF-8 JSON."""
    try:
        decoded, duplicates = scrub.load_json(raw)
    except (ValueError, UnicodeError):
        try:
            plain = raw.decode("utf-8")
        except UnicodeError:
            return None, True
        if "\x00" in plain or plain.removeprefix("\ufeff").lstrip().startswith(("{", "[")):
            return None, True
        return scrub.scrub_text(plain)
    text, failed = scrub.scrub_json(decoded, preserve_identity=True)
    if not failed and not duplicates and json.loads(text) == decoded:
        try:
            original = raw.decode("utf-8")
            # BOM-less UTF-16/32 ASCII can decode as UTF-8 but contains NULs.
            json.loads(original.removeprefix("\ufeff"))
        except (ValueError, UnicodeError):
            pass  # The validated scrubbed JSON is already safe UTF-8 text.
        else:
            text = original
    return text, failed


def _window(stream, line, surrounding, budget, expected_id=None):
    records = []
    identity_matches = expected_id is None
    for number, raw in enumerate(stream, 1):
        if number < max(1, line - surrounding):
            continue
        if number > line + surrounding:
            break
        if number == line and expected_id is not None:
            raw, identity_matches = _matching_record(raw, expected_id)
        text, failed = _scrub_record(raw)
        if failed:
            text = "[scrub failed: evidence withheld]"
        encoded = (text or "").encode()
        records.append(
            {
                "line": number,
                "text": encoded[:budget].decode("utf-8", "ignore"),
                "truncated": len(encoded) > budget,
            }
        )
    if not identity_matches:
        return {
            "unavailable": "target line no longer matches the referenced tool use",
            "records": [],
            "target_available": False,
        }
    selected = []
    truncated = False
    for record in sorted(records, key=lambda r: (abs(r["line"] - line), r["line"])):
        encoded = record["text"].encode()
        if budget <= 0:
            truncated = True
            continue
        text = encoded[:budget].decode("utf-8", "ignore")
        clipped = record["truncated"] or len(encoded) > budget
        selected.append({"line": record["line"], "text": text, "truncated": clipped})
        truncated |= clipped
        budget -= len(text.encode())
    selected.sort(key=lambda r: r["line"])
    return {
        "records": selected,
        "truncated": truncated,
        "target_available": _target_available(selected, line, expected_id),
    }


def _archive_window(archive, relative, line, surrounding, budget, expected_id=None):
    password = os.environ.get("GENESIS_BACKUP_PASSPHRASE")
    if not password:
        return {"unavailable": "local encrypted archive requires GENESIS_BACKUP_PASSPHRASE"}
    proc = subprocess.Popen(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase-fd",
            "0",
            "--decrypt",
            str(archive),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        proc.stdin.write(password.encode())
        proc.stdin.close()
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            member = next(iter(tar), None)
            if member is None or not member.isfile() or member.name != relative:
                raise ValueError("archive member does not match source")
            with tar.extractfile(member) as stream:
                result = _window(stream, line, surrounding, budget, expected_id)
            if tar.next() is not None:
                raise ValueError("archive has multiple members")
        # Drain to verify the authenticated encryption trailer even for early windows.
        while proc.stdout.read(65536):
            pass
        if proc.wait(timeout=30) != 0:
            raise ValueError("archive decryption failed")
        return {**result, "origin": "local encrypted archive"}
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def read_reference(
    projects, relative, line, *, surrounding=5, budget=65536, archive_dir=None, expected_id=None
):
    from .store import source_path

    try:
        relative = source_path(relative)
        path = PurePosixPath(relative)
    except (TypeError, ValueError):
        return {"unavailable": "invalid source reference"}
    if path.is_absolute() or ".." in path.parts or not path.parts or line < 1:
        return {"unavailable": "invalid source reference"}
    target = projects / relative
    try:
        if not target.resolve().is_relative_to(projects.resolve()) or target.is_symlink():
            return {"unavailable": "source resolves outside projects or is a symlink"}
        with target.open("rb") as stream:
            result = _window(stream, line, surrounding, budget, expected_id)
            if "unavailable" not in result:
                return {**result, "origin": "live"}
            raise FileNotFoundError("live evidence identity changed")
    except FileNotFoundError:
        archive_dir = archive_dir or Path.home() / "backups/genesis-backups/transcripts"
        archive = archive_dir / f"v2-{hashlib.sha256(os.fsencode(relative)).hexdigest()}.tar.gpg"
        if not archive.is_file():
            return {"unavailable": "source and local encrypted archive absent"}
        try:
            return _archive_window(archive, relative, line, surrounding, budget, expected_id)
        except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
            return {"unavailable": "local archive unreadable or invalid"}
    except OSError:
        return {"unavailable": "source unreadable"}


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


def _fit_output(out, budget):
    """Discard distant context first, then share target text space fairly."""

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
        return {"truncated": True, "unavailable": "references exceed evidence byte budget"}
    # Short targets release unused space to longer targets; JSON escaping counts.
    targets.sort(key=lambda item: len(json.dumps(item[2], ensure_ascii=False).encode()))
    for index, (_, record, text) in enumerate(targets):
        clipped = _clip_json_text(text, available // (len(targets) - index))
        record["text"] = clipped
        record["truncated"] |= clipped != text
        available -= len(json.dumps(clipped, ensure_ascii=False).encode()) - 2
    return out


def evidence(data, projects, tool_use_id, *, surrounding=5, budget=65536):
    if surrounding < 0 or budget < 1024:
        raise ValueError("evidence requires nonnegative context and at least 1024 bytes")
    from .identity import decode_identity

    raw_id = decode_identity(tool_use_id)
    _, rows = run_query(
        data,
        "SELECT source_file,line_no_call,result_source_file,line_no_result "
        "FROM tool_calls WHERE tool_use_id=?",
        [tool_use_id],
    )
    out = {"tool_use_id": tool_use_id, "references": [], "truncated": False}
    if not rows:
        out["unavailable"] = "tool use ID absent from query coverage"
        return _fit_output(out, budget)
    for relative, line in ((rows[0][0], rows[0][1]), (rows[0][2], rows[0][3])):
        if not relative or line is None:
            out["references"].append({"unavailable": "call or result reference absent"})
            continue
        ref = {
            "source": relative,
            "line": line,
            **read_reference(
                projects,
                relative,
                line,
                surrounding=surrounding,
                budget=budget // 3,
                expected_id=raw_id,
            ),
        }
        out["references"].append(ref)
        out["truncated"] |= ref.get("truncated", False)
    out = _fit_output(out, budget)
    for ref in out.get("references", []):
        if "records" in ref:
            ref["target_available"] = _target_available(ref["records"], ref["line"], raw_id)
    return out
