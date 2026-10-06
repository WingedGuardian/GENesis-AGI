"""CLI: ``python -m genesis.hostmetrics status | preflight``.

Exit codes for ``preflight``: GO 0, NO 2, WAIT 3, ASK 4; 64 for a usage error
(argparse's own 2 would read as NO). Full guide: docs/reference/resource-budget.md.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path

from genesis.hostmetrics import readings
from genesis.hostmetrics.host import HostMemory, host_memory_once
from genesis.hostmetrics.preflight import (
    GIB,
    Request,
    Snapshot,
    evaluate,
    group_disks,
    load_levers,
    missing_estimate,
)

EXIT_USAGE = 64
# Far beyond any real host (GiB or core-percent); a larger number is a typo, and
# 1e300 overflows int(value * GIB) into a traceback instead of a usage error.
_MAX_NUMBER = 1e6
_MAX_WINDOW = 3600.0  # seconds: a CPU sample longer than an hour is a mistake


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # noqa: D102 — keep exit 2 meaning NO only
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def take_snapshot(disk_paths: list[str], cpu_window: float, host: bool = True) -> Snapshot:
    return Snapshot(
        memory=readings.read_memory(),
        host=host_memory_once() if host else HostMemory(unavailable="skipped (--no-host)"),
        cpu_capacity=readings.cpu_capacity(),
        cpu_used=readings.read_cpu_used(cpu_window),
        psi={r: readings.read_psi(r) for r in ("cpu", "memory", "io")},
        disks={p: readings.read_disk(p) for p in disk_paths},
    )


def _disk_arg(text: str) -> tuple[str, int]:
    path, sep, gib = text.rpartition("=")
    try:
        if not sep or not path or not 0 <= float(gib) <= _MAX_NUMBER:  # nan fails too
            raise ValueError
        return str(Path(path).expanduser()), int(float(gib) * GIB)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected PATH=GB, got {text!r}") from None


def _nonneg(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not math.isfinite(value) or not 0 <= value <= _MAX_NUMBER:
        raise argparse.ArgumentTypeError(
            f"expected a number from 0 to {_MAX_NUMBER:g}, got {text!r}"
        )
    return value


def _window(text: str) -> float:
    value = _nonneg(text)
    if not 0.5 <= value <= _MAX_WINDOW:  # shorter is noise; zero reads as unreadable
        raise argparse.ArgumentTypeError(
            f"expected 0.5 to {_MAX_WINDOW:g} seconds, got {text!r}"
        )
    return value


def _gib(n: float) -> str:
    return f"{n / GIB:.1f} GiB"


def _status(args) -> int:
    levers = load_levers()
    paths: dict[int, str] = {}  # one line per filesystem
    for p in (str(Path.home()), "/"):
        try:
            paths.setdefault(os.stat(p).st_dev, p)
        except OSError:
            continue
    snap = take_snapshot(list(paths.values()), args.cpu_window, host=not args.no_host)
    if args.json:
        print(json.dumps({"threshold_pct": levers.threshold_pct, **asdict(snap)}, indent=1))
        return 0
    print(f"budget line: {levers.threshold_pct:g}% of each total")
    for note in levers.notes:
        print(f"note: {note}")
    if snap.memory:
        m = snap.memory
        print(f"memory ({m.source}): {_gib(m.total - m.available)} used of {_gib(m.total)}")
    else:
        print("memory: unreadable")
    if snap.host.unavailable:
        print(f"host memory: unavailable ({snap.host.unavailable})")
    else:
        print(f"host memory: {_gib(snap.host.used)} used of {_gib(snap.host.total)}")
    used = "unreadable" if snap.cpu_used is None else f"{snap.cpu_used:.2f}"
    print(f"cpu: {used} of {snap.cpu_capacity:g} cores busy (over {args.cpu_window:g}s)")
    print(
        "pressure avg300: "
        + ", ".join(f"{k} {'n/a' if v is None else f'{v:.2f}%'}" for k, v in snap.psi.items())
    )
    for path, disk in snap.disks.items():
        if disk:
            print(f"disk {path}: {_gib(disk[0] - disk[1])} used of {_gib(disk[0])}")
    return 0


def _preflight(args) -> int:
    req = Request(
        name=args.name,
        ram=None if args.ram is None else int(args.ram * GIB),
        cpu=args.cpu,
        assume_default=args.assume_default,
        approved_over_line=frozenset(args.approved_over_line or []),
    )
    levers = load_levers()
    # Before any reading: the CPU sample, the host call, even a stat of a disk path
    # (a stalled network mount would block a command that is about to say NO).
    disk_notes: list[str] = []
    result = missing_estimate(req, levers)
    if result is None:
        disks, disk_notes = group_disks(args.disk or [], readings.disk_device)
        req = replace(req, disks=disks)
        snap = take_snapshot(list(req.disks), args.cpu_window, host=not args.no_host)
        result = evaluate(snap, req, levers)
    result = replace(result, notes=(*disk_notes, *result.notes))
    if args.json:
        print(
            json.dumps(
                {
                    "name": req.name,
                    "verdict": result.verdict,
                    "exit_code": result.exit_code,
                    "checks": [asdict(c) for c in result.checks],
                    "notes": list(result.notes),
                },
                indent=1,
            )
        )
        return result.exit_code
    print(f"{result.verdict} {req.name}")
    for c in result.checks:
        fmt = (lambda v: f"{v:.0f}%") if c.resource == "cpu" else _gib
        print(
            f"  {c.resource}: {c.verdict} — {c.reason} (live {fmt(c.live)}, "
            f"estimate {fmt(c.estimate)}, line {fmt(c.line)}, total {fmt(c.total)})"
        )
    for note in result.notes:
        print(f"  note: {note}")
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="python -m genesis.hostmetrics")
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    common = _Parser(add_help=False)
    common.add_argument("--json", action="store_true")
    common.add_argument(
        "--cpu-window",
        type=_window,
        default=3.0,
        metavar="SECS",
        help="seconds to sample CPU use over (default 3)",
    )
    common.add_argument(
        "--no-host",
        action="store_true",
        help="skip the host leg (memory judged on the container only)",
    )
    sub.add_parser("status", parents=[common], help="print current readings")
    pf = sub.add_parser("preflight", parents=[common], help="admission verdict for a job")
    pf.add_argument("--name", required=True)
    pf.add_argument("--ram", type=_nonneg, metavar="GB", help="peak memory, GiB")
    pf.add_argument("--cpu", type=_nonneg, metavar="PCT", help="CPU, core-percent (100 = 1 core)")
    pf.add_argument(
        "--disk",
        type=_disk_arg,
        action="append",
        metavar="PATH=GB",
        help="disk space the job writes under PATH, GiB (repeatable)",
    )
    pf.add_argument(
        "--assume-default",
        action="store_true",
        help="use the default estimate for a missing --ram/--cpu",
    )
    pf.add_argument(
        "--approved-over-line",
        action="append",
        metavar="RESOURCE",
        choices=("memory", "cpu", "disk"),
        help="owner approved running over the line: budget = threshold x free",
    )
    args = parser.parse_args(argv)
    return _status(args) if args.cmd == "status" else _preflight(args)


if __name__ == "__main__":
    sys.exit(main())
