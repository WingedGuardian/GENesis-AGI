#!/usr/bin/env python3
"""Closed validator operations; no generic commands, SQL, plugins or lifecycle.

GROUNDWORK(validator-request-operations): serving identity, verification CLI and
synthetic hook probes will register their separately reviewed operations here.
Only the stateless protocol probe exists today. Requests are retained; admitted
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
    """Closed schema and operation set; probe safety proves no future mutation."""
    if (not isinstance(request, dict) or set(request) != {"version", "operation"}
            or type(request["version"]) is not int or request["version"] != 1
            or request["operation"] != "protocol_probe"):
        raise ValueError("Unsupported validator request")
    return {"version": 1, "operation": "protocol_probe", "ok": True}


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
