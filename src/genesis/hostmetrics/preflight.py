"""Admission verdict for a job: GO, WAIT, NO or ASK.

Per resource, with threshold t (default 80%), total D, live use L and the
job's estimate J, the budget line is t·D:

- NO   — J > t·D on its own (memory and disk; CPU is compressible and a scope
         quota enforces it, so CPU is never worse than WAIT);
- ASK  — L is already over the line and it is not transient: memory pressure
         (PSI ``avg300``) above the ASK lever, or disk (full stays full);
- WAIT — L + J > t·D: it would fit once current load drops;
- GO   — otherwise.

The overall verdict is the worst across resources (NO > ASK > WAIT > GO).
Memory is judged on the container and, when reachable, on the host too.
Inputs are a ``Snapshot`` of readings, so the rules are testable without a box.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from genesis.hostmetrics.host import HostMemory
from genesis.hostmetrics.readings import Memory

GO, WAIT, NO, ASK = "GO", "WAIT", "NO", "ASK"
EXIT_CODES = {GO: 0, NO: 2, WAIT: 3, ASK: 4}
_RANK = {GO: 0, WAIT: 1, ASK: 2, NO: 3}
GIB = 1024**3

ENV_FILE = Path.home() / ".genesis" / "resource-budget.env"
_LEVER_DEFAULTS = {
    "GENESIS_RB_THRESHOLD_PCT": 80.0,  # the budget line, % of each total
    "GENESIS_RB_ASK_PSI": 10.0,  # memory PSI some-avg300 % that makes over-the-line ASK
    "GENESIS_RB_DEFAULT_RAM_PCT": 25.0,  # --assume-default RAM, % of memory total
    "GENESIS_RB_DEFAULT_CPU_PCT": 25.0,  # --assume-default CPU, % of CPU capacity
}


@dataclass(frozen=True)
class Levers:
    threshold_pct: float = 80.0
    ask_psi: float = 10.0
    default_ram_pct: float = 25.0
    default_cpu_pct: float = 25.0
    notes: tuple[str, ...] = ()


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return values
    for line in text.splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and key.strip().startswith("GENESIS_RB_"):
            values[key.strip()] = value.strip().strip("'\"")
    return values


def load_levers(env: dict[str, str] | None = None, env_file: Path = ENV_FILE) -> Levers:
    """Levers from the install env file, overridden by the process environment.

    An invalid value falls back to its default and says so in ``notes``.
    """
    merged = {**_read_env_file(env_file), **(os.environ if env is None else env)}
    values: dict[str, float] = {}
    notes = [
        f"{key} is not a known lever; ignored"
        for key in merged
        if key.startswith("GENESIS_RB_") and key not in _LEVER_DEFAULTS
    ]
    for key, default in _LEVER_DEFAULTS.items():
        raw = merged.get(key)
        values[key] = default
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except ValueError:
            value = -1.0
        if 0 < value <= 100 or (key == "GENESIS_RB_ASK_PSI" and value == 0):
            values[key] = value
        else:
            notes.append(f"{key}={raw!r} is not a percent in (0, 100]; using {default:g}")
    return Levers(
        threshold_pct=values["GENESIS_RB_THRESHOLD_PCT"],
        ask_psi=values["GENESIS_RB_ASK_PSI"],
        default_ram_pct=values["GENESIS_RB_DEFAULT_RAM_PCT"],
        default_cpu_pct=values["GENESIS_RB_DEFAULT_CPU_PCT"],
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class Snapshot:
    memory: Memory | None
    host: HostMemory
    cpu_capacity: float  # cores
    cpu_used: float | None  # cores
    psi: dict[str, float | None]  # "cpu"/"memory"/"io" → some avg300 %
    disks: dict[str, tuple[int, int] | None]  # path → (total, free) bytes
    # Live genesis-job scopes: (live) memory they may still grow into, Σ max(0, R_i − actual_i).
    # None when the user manager could not be asked.
    reserved_beyond_use: int | None = 0


@dataclass(frozen=True)
class Request:
    name: str
    ram: int | None = None  # bytes
    cpu: float | None = None  # core-percent (100 = one core)
    disks: dict[str, int] = field(default_factory=dict)  # path → bytes
    assume_default: bool = False
    approved_over_line: frozenset[str] = frozenset()  # "memory" / "cpu" / "disk"


@dataclass(frozen=True)
class Check:
    resource: str
    verdict: str
    total: float
    live: float
    estimate: float
    line: float
    reason: str


@dataclass(frozen=True)
class Result:
    verdict: str
    checks: tuple[Check, ...]
    notes: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]


def judge(
    resource: str,
    *,
    total: float,
    live: float,
    estimate: float,
    threshold: float,
    compressible: bool = False,
    persistent: bool = False,
    pressure_high: bool = False,
    approved: bool = False,
) -> Check:
    """One resource's verdict. ``approved``: the owner accepted running while the
    resource is over the line; the job's budget becomes t × what is free now."""
    line = threshold * total
    over = NO if not compressible else WAIT
    if approved:
        budget = threshold * max(0.0, total - live)
        if estimate > budget:
            return Check(
                resource,
                over,
                total,
                live,
                estimate,
                budget,
                "over the approved share of what is free",
            )
        return Check(
            resource, GO, total, live, estimate, budget, "within the approved share of what is free"
        )
    if estimate > line and not compressible:
        return Check(
            resource, NO, total, live, estimate, line, "estimate alone exceeds the budget line"
        )
    if live > line and (persistent or pressure_high):
        why = "already over the line" + (" with sustained pressure" if pressure_high else "")
        return Check(resource, ASK, total, live, estimate, line, why)
    if live + estimate > line:
        return Check(resource, WAIT, total, live, estimate, line, "fits once current load drops")
    return Check(resource, GO, total, live, estimate, line, "fits under the line")


def group_disks(
    pairs: list[tuple[str, int]], device: Callable[[str], int | None]
) -> tuple[dict[str, int], list[str]]:
    """Sum disk needs per filesystem, keyed by the first path named on it.

    Two paths on one filesystem share its free space, so judging them apart
    would admit a job that needs their sum. A repeated path adds up too.
    """
    by_dev: dict[object, str] = {}
    disks: dict[str, int] = {}
    notes: list[str] = []
    for path, need in pairs:
        dev = device(path)
        key = by_dev.setdefault(path if dev is None else dev, path)
        if key in disks and key != path:
            notes.append(f"disk {path} counted with {key} (same filesystem)")
        disks[key] = disks.get(key, 0) + need
    return disks, notes


def missing_estimate(req: Request, levers: Levers) -> Result | None:
    """NO before any reading is taken, when an estimate is missing and not defaulted."""
    if req.assume_default or (req.ram is not None and req.cpu is not None):
        return None
    missing = " and ".join(n for n, v in (("--ram", req.ram), ("--cpu", req.cpu)) if v is None)
    return Result(
        NO, (), (*levers.notes, f"estimate required: pass {missing}, or --assume-default")
    )


def evaluate(snap: Snapshot, req: Request, levers: Levers) -> Result:
    early = missing_estimate(req, levers)
    if early is not None:
        return early
    t = levers.threshold_pct / 100
    notes = list(levers.notes)
    checks: list[Check] = []
    # committed = live − Σ actual_i + Σ R_i: admitted jobs keep their headroom.
    reserved = snap.reserved_beyond_use or 0
    if snap.reserved_beyond_use is None:
        notes.append("live jobs unknown (user manager unreachable); not counted")
    elif reserved:
        notes.append(f"live genesis jobs reserve {reserved / GIB:.1f} GiB beyond their use")

    ram, cpu = req.ram, req.cpu
    if ram is None or cpu is None:
        if ram is None and snap.memory is not None:
            ram = int(snap.memory.total * levers.default_ram_pct / 100)
            notes.append(
                f"ASSUMED --ram {ram / GIB:.1f} GiB ({levers.default_ram_pct:g}% of memory)"
            )
        if cpu is None:
            cpu = snap.cpu_capacity * levers.default_cpu_pct
            notes.append(f"ASSUMED --cpu {cpu:.0f} ({levers.default_cpu_pct:g}% of CPU)")

    if snap.memory is None or ram is None:
        notes.append("memory reading unavailable; cannot judge memory")
        checks.append(Check("memory", ASK, 0, 0, float(ram or 0), 0, "memory unreadable"))
    else:
        mem = snap.memory
        psi = snap.psi.get("memory")
        checks.append(
            judge(
                "memory",
                total=mem.total,
                live=mem.total - mem.available + reserved,
                estimate=ram,
                threshold=t,
                pressure_high=psi is not None and psi > levers.ask_psi,
                approved="memory" in req.approved_over_line,
            )
        )
    host = snap.host  # judged even when the container reading failed
    if ram is not None and (host.unavailable or host.total is None or host.used is None):
        notes.append(f"host leg unavailable: {host.unavailable}; judged on the container only")
    elif ram is not None:
        checks.append(
            judge(
                "memory (host)",
                total=host.total,
                live=host.used + reserved,
                estimate=ram,
                threshold=t,
                approved="memory" in req.approved_over_line,
            )
        )

    cpu_live = snap.cpu_used
    if cpu_live is None:
        notes.append("CPU usage unreadable; CPU judged on the estimate alone")
        cpu_live = 0.0
    checks.append(
        judge(
            "cpu",
            total=snap.cpu_capacity * 100,
            live=cpu_live * 100,
            estimate=cpu,
            threshold=t,
            compressible=True,
            approved="cpu" in req.approved_over_line,
        )
    )

    for path, need in req.disks.items():
        disk = snap.disks.get(path)
        if disk is None:
            checks.append(Check(f"disk {path}", ASK, 0, 0, need, 0, "disk unreadable"))
            continue
        total, free = disk
        checks.append(
            judge(
                f"disk {path}",
                total=total,
                live=total - free,
                estimate=need,
                threshold=t,
                persistent=True,
                approved="disk" in req.approved_over_line,
            )
        )

    verdict = max((c.verdict for c in checks), key=_RANK.__getitem__, default=GO)
    return Result(verdict, tuple(checks), tuple(notes))
