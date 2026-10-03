"""Pinned stock CBM worker adapter, called only inside the admitted batch scope.

Ordinary v0.11 CLI calls delegate to the account daemon, so their worker inherits
that daemon's limits. This opt-in adapter runs stock internal worker mode in the
queue's existing scope, preserving native cohort, mutation locks and publication.
No provider patch or separate IPC namespace is used. Internal ABI is exact-build
pinned. Crashes fail the queued attempt; provider skip-and-retry is not reproduced.
The entrypoint's cgroup watchdog owns cancellation and descendant cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

# v0.11.0 Linux x86_64 portable release, independently matched to release digest.
BUILD = "ce11c141431aeadd788506c3a7e6942db8fd438dec369d0707a39ec9fd8c6510"
MAX_RESPONSE = 1024 * 1024


def verify_scope(env: dict[str, str]) -> int:
    """Reuse destination-scope admission; never accept a caller-only cap."""
    admission = runpy.run_path(str(Path(__file__).with_name("code_intel_cbm_admission.py")))
    number = admission["number"]
    try:
        cap = number(env.get("CODE_INTEL_CHILD_CAP_BYTES", ""), "worker cap")
        admission["assess"](
            cap,
            number(env.get("CODE_INTEL_CHILD_RESERVE_BYTES", ""), "sibling reserve"),
            env.get("CODE_INTEL_CHILD_SCOPE_UNIT", ""),
            cache_reserve=number(
                env.get("CODE_INTEL_FILE_CACHE_RESERVE_BYTES", "2147483648"), "cache reserve"
            ),
        )
    except admission["AdmissionRefused"]:
        marker = env.get("CODE_INTEL_CHILD_REFUSAL_MARKER")
        if marker:
            Path(marker).write_text("refused\n")
        raise
    if Path("/proc/self/oom_score_adj").read_text().strip() != "1000":
        raise ValueError("worker requires OOM priority 1000")
    return cap


def read_result(path: Path) -> dict:
    """A clean worker exit also carries MCP errors; validate its response."""
    with path.open("rb") as stream:
        payload = stream.read(MAX_RESPONSE + 1)
    if not payload or len(payload) > MAX_RESPONSE:
        raise ValueError("missing or oversized worker response")
    result = json.loads(payload)
    if not isinstance(result, dict) or result.get("isError") is not False:
        raise ValueError("worker returned an error or invalid MCP response")
    if not isinstance(result.get("content"), list):
        raise ValueError("worker response has no MCP content")
    return result


def _execute_stock_worker(binary: Path, args: argparse.Namespace, cap: int) -> int:
    if not binary.is_absolute() or not os.access(binary, os.X_OK):
        raise ValueError("worker binary must be an absolute executable path")
    # Keep the verified inode open through exec. Never resolve the incident shim
    # on PATH or fall back to an ordinary CLI when this adapter refuses.
    with binary.open("rb") as executable:
        if hashlib.file_digest(executable, "sha256").hexdigest() != BUILD:
            raise ValueError("unsupported worker build; rerun acceptance before upgrading")
        cache = Path(os.environ.get("CBM_CACHE_DIR", ""))
        if not cache.is_absolute() or not cache.is_dir():
            raise ValueError("worker requires an explicit existing absolute cache directory")
        # Response sizes are bounded and small; place them in the selected cache,
        # never in the shared CC temporary volume.
        with tempfile.TemporaryDirectory(prefix="genesis-worker-", dir=cache) as directory:
            response = Path(directory) / "response.json"
            command = [
                f"/proc/self/fd/{executable.fileno()}",
                "cli",
                "--index-worker",
                "--index-worker-build",
                BUILD,
                "index_repository",
                json.dumps(
                    {
                        "repo_path": args.repo_path,
                        "mode": args.mode,
                        "persistence": args.persistence == "true",
                    }
                ),
                "--response-out",
                str(response),
                "--index-worker-memory-budget-bytes",
                str(cap * 3 // 4),
            ]
            rc = subprocess.call(command, pass_fds=(executable.fileno(),))
            if rc != 0:
                return 111  # failure, never queue deferral/partial-success codes
            print(json.dumps(read_result(response)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--mode", choices=("fast", "moderate", "full"), required=True)
    parser.add_argument("--persistence", choices=("true", "false"), required=True)
    args = parser.parse_args(argv)
    try:
        if not Path(args.repo_path).is_absolute():
            raise ValueError("repository path must be absolute")
        cap = verify_scope(dict(os.environ))
        return _execute_stock_worker(Path(os.environ.get("CODE_INTEL_CBM_WORKER_BINARY", "")), args, cap)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"code-intel: stock worker refused/failed: {exc}", file=sys.stderr)
        return 111


if __name__ == "__main__":
    raise SystemExit(main())
