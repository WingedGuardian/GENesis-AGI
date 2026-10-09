#!/usr/bin/env python3
"""Closed validator operations; no generic commands, SQL, plugins or lifecycle.

Serving observations delegate to the existing deployment status tripwire.
GROUNDWORK(validator-request-operations): verification CLI and synthetic hook
probes need their separately reviewed operations. Requests are retained; admitted
workspace patches own creation and retirement after evidence capture.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME_ROOT / "src"))
sys.path.insert(0, str(RUNTIME_ROOT / "scripts"))
sys.path.insert(0, str(RUNTIME_ROOT / "scripts" / "hooks"))

from codex_validator_shell import MAX_REQUEST_BYTES, check_request  # noqa: E402

from genesis.eval.qualification.evidence import load_json, private_open  # noqa: E402


class RequestParser(argparse.ArgumentParser):
    """Keep malformed CLI arguments out of diagnostics, including their values."""

    def error(self, message: str) -> None:
        raise ValueError("Unsupported validator invocation")


def read_request(workspace: Path, supplied: str) -> object:
    """Bound actual opened bytes; refuse replacement observed during opening."""
    target = check_request(workspace, supplied)
    before = target.lstat()
    fd = private_open(target, os.O_RDONLY)
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Validator request changed while opening")
        raw = handle.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("Validator request exceeds its size limit")
    return load_json(raw)


def execute(request: object) -> dict:
    """Exact per-operation schemas, checked before entering any operation."""
    if (not isinstance(request, dict)
            or type(request.get("version")) is not int or request["version"] != 1
            or not isinstance(request.get("operation"), str)):
        raise ValueError("Unsupported validator request")
    operation = request["operation"]
    keys = {"version", "operation", "token"} if operation == "serving_verify" else {"version", "operation"}
    if set(request) != keys or operation not in {"protocol_probe", "serving_status", "serving_verify"}:
        raise ValueError("Unsupported validator request")
    if operation == "protocol_probe":
        return {"version": 1, "operation": operation, "ok": True}
    from codex_validator_serving import TOKEN_RE, observe

    if operation == "serving_verify" and (
            not isinstance(request["token"], str) or not TOKEN_RE.fullmatch(request["token"])):
        raise ValueError("Unsupported validation token")

    result = observe(RUNTIME_ROOT, token=request["token"] if operation == "serving_verify" else None)
    return {"version": 1, "operation": operation, **result}


def main() -> int:
    parser = RequestParser()
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--request", required=True)
    try:
        args = parser.parse_args()
        workspace = Path(args.workspace_root)
        if (not workspace.is_absolute() or not workspace.is_dir()
                or str(workspace.resolve()) != args.workspace_root
                or workspace.is_relative_to(RUNTIME_ROOT)
                or RUNTIME_ROOT.is_relative_to(workspace)):
            raise ValueError("Unsupported validator workspace")
        result = execute(read_request(workspace, args.request))
    except Exception:
        print("Validator request refused", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
