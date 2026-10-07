"""Pinned stock CBM worker adapter, called only inside the admitted batch scope.

Ordinary v0.11 CLI calls delegate to the account daemon, so their worker inherits
that daemon's limits. This managed adapter runs stock internal worker mode in the
queue's existing scope, preserving native cohort, mutation locks and publication.
No provider patch or separate IPC namespace is used. Internal ABI is exact-build
pinned. Crashes fail the queued attempt; provider skip-and-retry is not reproduced.
The entrypoint's cgroup watchdog owns cancellation and descendant cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

# v0.11.0 Linux x86_64 portable release, independently matched to release digest.
BUILD = "ce11c141431aeadd788506c3a7e6942db8fd438dec369d0707a39ec9fd8c6510"  # pragma: allowlist secret (public executable digest, not a secret)
MAX_RESPONSE = 1024 * 1024
JOB_CAP = 8 * 1024**3


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
        adjustment = int(Path("/proc/self/oom_score_adj").read_text().strip())
        if not -1000 <= adjustment < 1000:
            raise ValueError("worker supervisor requires a nonmaximum OOM adjustment")
    except (OSError, ValueError):
        marker = env.get("CODE_INTEL_CHILD_REFUSAL_MARKER")
        if marker:
            Path(marker).write_text("refused\n")
        raise
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


def load_managed() -> dict:
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "codebase_managed.py"))


def _execute_stock_worker(path: Path, args: argparse.Namespace, cap: int) -> int:
    with ExitStack() as resources:
        spawn_started = False
        try:
            if cap != JOB_CAP:
                raise ValueError("managed worker requires the full 8 GiB job cap")
            managed = load_managed()
            resources.enter_context(managed["lifecycle_lock"](shared=True))
            config = managed["runtime_config"](managed["config_path"](str(path)))
            if not Path(args.repo_path).is_absolute() or Path(args.repo_path).resolve(
                strict=True
            ) != Path(config["main"]):
                raise ValueError("worker requires the configured physical main checkout")
            managed["verify_cache"](config)
            managed["ready"](config)
            executable = managed["verified_binary"](Path(config["binary"]))
            # Keep the accepted inode through child creation, but close the
            # lifecycle admission lock immediately after Popen, before wait.
            with (
                executable as binary,
                tempfile.TemporaryDirectory(
                    prefix="genesis-worker-", dir=config["cache"]
                ) as directory,
            ):
                managed["require_enabled"](config)
                managed["check_backend"](config)
                spawn_started = True
                return _spawn_worker(managed, config, binary, args, cap, Path(directory), resources)
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError):
            if not spawn_started:
                marker = os.environ.get("CODE_INTEL_CHILD_REFUSAL_MARKER")
                if marker:
                    Path(marker).write_text("refused\n")
            raise


def _spawn_worker(
    managed: dict, config: dict, executable, args, cap: int, directory: Path, admission: ExitStack
) -> int:
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
    process = subprocess.Popen(
        ["/bin/bash", str(Path(__file__).with_name("code_intel_index.sh")),
         "--exec-indexer-with-oom-adj", *command],
        pass_fds=(executable.fileno(),),
        env=managed["native_env"](config),
        cwd=config["main"],
    )
    admission.close()
    rc = process.wait()
    if rc != 0:
        return 111  # failure, never queue deferral/partial-success codes
    print(json.dumps(read_result(response)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--mode", choices=("fast", "moderate", "full"), required=True)
    parser.add_argument("--persistence", choices=("true", "false"), required=True)
    parser.add_argument("--managed-config", required=True)
    args = parser.parse_args(argv)
    try:
        if not Path(args.repo_path).is_absolute():
            raise ValueError("repository path must be absolute")
        cap = verify_scope(dict(os.environ))
        return _execute_stock_worker(Path(args.managed_config), args, cap)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"code-intel: stock worker refused/failed: {exc}", file=sys.stderr)
        return 111


if __name__ == "__main__":
    raise SystemExit(main())
