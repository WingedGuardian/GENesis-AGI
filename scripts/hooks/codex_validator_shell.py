"""One sealed request invocation, not a general shell-effects classifier.

Full-access programs, disabled hooks and later filesystem races remain outside
this accident-prevention boundary. Launch-owned roots establish authority.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from uuid import UUID

from codex_validator_patch import check_patch
from shell_parse import analyze_checked

MAX_REQUEST_BYTES = 64 * 1024


def request_path(workspace: Path, supplied: str) -> Path:
    """Reconstruct an exact absolute request path; cwd contributes nothing."""
    name = Path(supplied).name
    token = name.removesuffix(".json")
    if name != token + ".json" or str(UUID(token)) != token:
        raise ValueError("Validator request requires a canonical UUID filename")
    expected = workspace / "requests" / name
    if supplied != str(expected) or not expected.is_absolute():
        raise ValueError("Validator request must use its exact workspace path")
    return expected


def check_request(workspace: Path, supplied: str) -> Path:
    """Validate private data-file ownership; never read or change its contents."""
    from genesis.eval.qualification.evidence import private_open

    target = request_path(workspace, supplied)
    check_patch(f"*** Begin Patch\n*** Update File: {target}\n*** End Patch", workspace)
    for directory in (workspace, workspace / "requests"):
        info = directory.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Validator request directories must be private and owned")
    fd = private_open(target, os.O_RDONLY)
    try:
        if os.fstat(fd).st_size > MAX_REQUEST_BYTES:
            raise ValueError("Validator request exceeds its size limit")
    finally:
        os.close(fd)
    return target


def check_command(command: str, workspace: Path, runtime: Path) -> None:
    """Match the shared parser and raw bytes to one known seven-argv tuple."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in str(workspace) + str(runtime)):
        raise ValueError("Validator roots contain unsupported characters")
    segments, blind = analyze_checked(command)
    if blind or len(segments) != 1:
        raise ValueError("Use the sealed validator request invocation")
    segment = segments[0]
    if segment.depth or segment.redirects or segment.verb_unresolved or len(segment.argv) != 7:
        raise ValueError("Use the sealed validator request invocation")
    target = request_path(workspace, segment.argv[-1])
    interpreter = runtime / ".venv" / "bin" / "python"
    runner = runtime / "scripts" / "codex_validator_request.py"
    expected = [str(interpreter), "-I", str(runner), "--workspace-root",
                str(workspace), "--request", str(target)]
    # All variable tokens are reconstructed from trusted roots and a UUID.
    # Canonical shell quoting protects literal characters inside those paths.
    if segment.argv != expected or command != shlex.join(expected):
        raise ValueError("Use the sealed validator request invocation")
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK) or not runner.is_file():
        raise ValueError("Validator request runtime is unavailable")
    check_request(workspace, str(target))
