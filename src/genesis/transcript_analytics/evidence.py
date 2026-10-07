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


def _window(stream, line, surrounding, budget, expected_id=None):
    records, truncated = [], False
    identity_matches = expected_id is None
    for number, raw in enumerate(stream, 1):
        if number < max(1, line - surrounding):
            continue
        if number > line + surrounding:
            break
        if number == line and expected_id is not None:
            try:
                record = json.loads(raw)
                blocks = record.get("message", {}).get("content", [])
                identity_matches = isinstance(blocks, list) and any(
                    isinstance(b, dict)
                    and b.get("type") in ("tool_use", "tool_result")
                    and (b.get("id") == expected_id or b.get("tool_use_id") == expected_id)
                    for b in blocks
                )
            except (ValueError, AttributeError):
                identity_matches = False
        text, failed = scrub.scrub_text(raw.decode("utf-8", "replace"))
        if failed:
            text = "[scrub failed: evidence withheld]"
        encoded = (text or "").encode()
        if len(encoded) > budget:
            text = encoded[:budget].decode("utf-8", "ignore")
            truncated = True
        records.append({"line": number, "text": text})
        budget -= len((text or "").encode())
        if budget <= 0:
            truncated = True
            break
    if not identity_matches:
        return {
            "unavailable": "target line no longer matches the referenced tool use",
            "records": [],
            "target_available": False,
        }
    return {
        "records": records,
        "truncated": truncated,
        "target_available": any(r["line"] == line for r in records),
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
    path = PurePosixPath(relative)
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
        archive = archive_dir / f"v2-{hashlib.sha256(relative.encode()).hexdigest()}.tar.gpg"
        if not archive.is_file():
            return {"unavailable": "source and local encrypted archive absent"}
        try:
            return _archive_window(archive, relative, line, surrounding, budget, expected_id)
        except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
            return {"unavailable": "local archive unreadable or invalid"}
    except OSError:
        return {"unavailable": "source unreadable"}


def evidence(data, projects, tool_use_id, *, surrounding=5, budget=65536):
    if surrounding < 0 or budget < 1024:
        raise ValueError("evidence requires nonnegative context and at least 1024 bytes")
    _, rows = run_query(
        data,
        "SELECT source_file,line_no_call,result_source_file,line_no_result "
        "FROM tool_calls WHERE tool_use_id=?",
        [tool_use_id],
    )
    out = {"tool_use_id": tool_use_id, "references": [], "truncated": False}
    if not rows:
        out["unavailable"] = "tool use ID absent from query coverage"
        return out
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
                expected_id=tool_use_id,
            ),
        }
        out["references"].append(ref)
        out["truncated"] |= ref.get("truncated", False)
    # JSON escaping and metadata count against the output budget too.
    while len(json.dumps(out, ensure_ascii=False).encode()) > budget:
        candidates = [r for r in out["references"] if r.get("records")]
        if not candidates:
            return {"truncated": True, "unavailable": "references exceed evidence byte budget"}
        max(candidates, key=lambda r: len(json.dumps(r))).get("records").pop()
        out["truncated"] = True
    for ref in out["references"]:
        if "records" in ref:
            ref["target_available"] = any(r["line"] == ref["line"] for r in ref["records"])
    return out
