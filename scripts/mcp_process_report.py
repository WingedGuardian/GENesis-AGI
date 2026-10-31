#!/usr/bin/env python3
"""Read-only Linux MCP process observations; never infer abandoned ownership."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def proc_identity(path: Path) -> tuple[int, int]:
    """Return PPID/start ticks; comm may contain spaces and closing parentheses."""
    tail = (path / "stat").read_text().rsplit(")", 1)[1].split()
    return int(tail[1]), int(tail[19])


def family(argv: list[str]) -> str | None:
    """Classify known launchers without retaining arbitrary arguments."""
    if not argv:
        return None
    executable = Path(argv[0]).name
    if executable == "codex" and argv[1:2] == ["app-server"]:
        return "codex-app-server"
    # Match executable/script positions, not arbitrary flag values or shell text.
    scripts = argv[1:2] if executable.startswith(("python", "node")) else argv[:1]
    names = {Path(arg).name for arg in scripts}
    if "genesis_mcp_server.py" in names:
        return "genesis-mcp"
    if "codex_external_mcp.py" in names:
        return "genesis-launcher"
    if executable in {"gitnexus", "ripwire", "terse", "serena", "codebase-memory-mcp"}:
        return executable
    if "gitnexus" in names or ("cli.js" in names and any("/gitnexus/" in a for a in scripts)):
        return "gitnexus"
    return None


def memory(path: Path) -> tuple[dict, list[str]]:
    values = {"rss_kib": None, "pss_kib": None, "swap_pss_kib": None}
    errors = []
    for filename, fields in (
        ("status", {"VmRSS": "rss_kib"}),
        ("smaps_rollup", {"Pss": "pss_kib", "SwapPss": "swap_pss_kib"}),
    ):
        try:
            for line in (path / filename).read_text().splitlines():
                name, _, rest = line.partition(":")
                if name in fields:
                    parts = rest.split()
                    if len(parts) != 2 or parts[1] != "kB" or int(parts[0]) < 0:
                        raise ValueError("invalid memory field")
                    values[fields[name]] = int(parts[0])
        except (OSError, ValueError) as error:
            errors.append(f"{filename}:{type(error).__name__}")
    return values, errors


def ancestor(pid: int, rows: dict[int, dict]) -> int | None:
    """Find a Codex daemon through stable observed parent edges, not timing."""
    visited = set()
    while pid in rows and pid not in visited:
        visited.add(pid)
        row = rows[pid]
        if not row["stable"]:
            return None
        if row["family"] == "codex-app-server":
            return pid
        parent = rows.get(row["ppid"])
        if parent and parent["start_ticks"] > row["start_ticks"]:
            return None
        pid = row["ppid"]
    return None


def snapshot(proc_root: Path = Path("/proc"), cgroup: Path | None = None) -> dict:
    started = datetime.now(UTC).isoformat()
    errors = []
    try:
        boot_id = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
    except OSError as error:
        boot_id = None
        errors.append({"source": "boot_id", "error": type(error).__name__})
    rows = {}
    # Failure to enumerate proc itself aborts; never report an empty healthy scan.
    for path in sorted(proc_root.iterdir()):
        if not path.name.isdecimal():
            continue
        pid = int(path.name)
        try:
            ppid, ticks = proc_identity(path)
            argv = (path / "cmdline").read_bytes().decode(errors="replace").split("\0")
            rows[pid] = {"pid": pid, "ppid": ppid, "start_ticks": ticks,
                         "family": family(argv), "stable": True}
        except (OSError, ValueError, IndexError) as error:
            errors.append({"pid": pid, "source": "discovery", "error": type(error).__name__})
    # Read candidate metrics before verifying identities for all ancestry nodes.
    for pid, row in rows.items():
        if row["family"] or ancestor(pid, rows) is not None:
            row["memory"], row["memory_errors"] = memory(proc_root / str(pid))
    for pid, row in rows.items():
        try:
            row["stable"] = proc_identity(proc_root / str(pid)) == (row["ppid"], row["start_ticks"])
        except (OSError, ValueError, IndexError):
            row["stable"] = False
        if not row["stable"]:
            errors.append({"pid": pid, "source": "identity", "error": "changed_or_unavailable"})
    processes = []
    for pid, row in rows.items():
        if "memory" not in row:
            continue
        owner = ancestor(pid, rows) if boot_id else None
        if not row["stable"]:
            row["memory"] = dict.fromkeys(row["memory"])
        processes.append({
            **row, "boot_id": boot_id,
            "daemon_pid": owner,
            "process_ownership": "observed_ancestry" if owner is not None else "unknown",
            "thread_ownership": "unknown",
        })
    metrics = {}
    for name in ("rss_kib", "pss_kib", "swap_pss_kib"):
        measured = [row["memory"][name] for row in processes if row["memory"][name] is not None]
        metrics[name] = {"sum_available": sum(measured) if measured else None,
                         "measured_count": len(measured), "selected_count": len(processes)}
    result = {
        "schema_version": 1, "started_at": started,
        "scope": "known MCP launchers and observed Codex app-server descendants",
        "limitations": ["Not an atomic snapshot; new or hidden processes may be absent.",
                        "Ancestry establishes a process relationship, not conversation ownership.",
                        "No liveness, orphan, leak, or cleanup verdict is inferred."],
        "thread_inventory": {"status": "unavailable", "reason": "not queried by this proc-only report"},
        "processes": processes, "memory_totals": metrics, "discovery_errors": errors,
        "discovery_complete": not errors,
    }
    if cgroup is not None:
        result["cgroup"] = {"scope": str(cgroup), "values": {}, "errors": {}}
        for name in ("memory.current", "memory.max", "memory.swap.current", "memory.pressure"):
            try:
                result["cgroup"]["values"][name] = (cgroup / name).read_text().strip()
            except OSError as error:
                result["cgroup"]["errors"][name] = type(error).__name__
    result["finished_at"] = datetime.now(UTC).isoformat()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    parser.add_argument("--cgroup", type=Path, help="Optional explicit cgroup scope; not inferred from ancestry")
    args = parser.parse_args()
    print(json.dumps(snapshot(args.proc_root, args.cgroup), indent=2))


if __name__ == "__main__":
    main()
