"""Diagnostic data collector — HOST-SIDE. Gathers system metrics for CC diagnosis.

Implements the design doc's diagnostic checklist. Collects ALL metrics into a
DiagnosticSnapshot before any LLM reasoning. Each collector runs independently
with its own timeout and returns a sensible default on failure.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from genesis.guardian.config import GuardianConfig

logger = logging.getLogger(__name__)

_INCUS_TIMEOUT = 15.0  # seconds per incus exec call


@dataclass
class MemoryInfo:
    current_bytes: int = 0
    max_bytes: int = 0
    usage_pct: float = 0.0
    pressure_full_10s: float = 0.0
    pressure_full_60s: float = 0.0


@dataclass
class IOInfo:
    pressure_full_10s: float = 0.0
    pressure_full_60s: float = 0.0


@dataclass
class CPUInfo:
    usage_usec: int = 0
    pressure_some_10s: float = 0.0  # CPU only has "some", not "full"


@dataclass
class DiskInfo:
    mount: str = ""
    total_mb: int = 0
    used_mb: int = 0
    avail_mb: int = 0
    usage_pct: float = 0.0


@dataclass
class ServiceInfo:
    name: str = ""
    active: bool = False
    sub_state: str = ""
    n_restarts: int = 0


@dataclass
class DiagnosticSnapshot:
    """Complete system diagnostic data for CC analysis."""

    collected_at: str = ""

    # Container info
    container_status: str = ""
    uptime: str = ""

    # Processes
    top_processes: str = ""
    zombie_count: int = 0
    dstate_count: int = 0

    # Resources
    memory: MemoryInfo = field(default_factory=MemoryInfo)
    io: IOInfo = field(default_factory=IOInfo)
    cpu: CPUInfo = field(default_factory=CPUInfo)
    disks: list[DiskInfo] = field(default_factory=list)

    # Systemd services
    services: list[ServiceInfo] = field(default_factory=list)

    # Journal logs
    journal_recent: str = ""  # last 100 lines
    error_count_1h: int = 0
    error_count_6h: int = 0

    # Git state
    git_last_commit: str = ""
    git_uncommitted: str = ""

    # Status files
    status_json: str = ""
    watchdog_state: str = ""

    def to_prompt_text(self) -> str:
        """Format as structured text for the CC diagnosis prompt."""
        lines = [
            f"=== DIAGNOSTIC SNAPSHOT ({self.collected_at}) ===",
            "",
            f"Container: {self.container_status}",
            f"Uptime: {self.uptime}",
            "",
            "--- MEMORY ---",
            f"Usage: {self.memory.usage_pct:.1f}% "
            f"({self.memory.current_bytes // (1024*1024)}M / "
            f"{self.memory.max_bytes // (1024*1024)}M)",
            f"Pressure (10s/60s): {self.memory.pressure_full_10s:.2f}% / "
            f"{self.memory.pressure_full_60s:.2f}%",
            "",
            "--- CPU ---",
            f"Usage: {self.cpu.usage_usec}us total",
            f"Pressure (some 10s): {self.cpu.pressure_some_10s:.2f}%",
            "",
            "--- I/O ---",
            f"Pressure (10s/60s): {self.io.pressure_full_10s:.2f}% / "
            f"{self.io.pressure_full_60s:.2f}%",
            "",
            "--- DISK ---",
        ]
        for d in self.disks:
            lines.append(
                f"  {d.mount}: {d.usage_pct:.0f}% "
                f"({d.used_mb}M / {d.total_mb}M, {d.avail_mb}M free)"
            )
        lines.extend([
            "",
            "--- SERVICES ---",
        ])
        for s in self.services:
            status = "ACTIVE" if s.active else "INACTIVE"
            lines.append(f"  {s.name}: {status} ({s.sub_state}) restarts={s.n_restarts}")
        lines.extend([
            "",
            "--- PROCESSES (top by memory) ---",
            self.top_processes or "(unavailable)",
            "",
            f"Zombies: {self.zombie_count}, D-state: {self.dstate_count}",
            "",
            f"--- ERRORS (1h: {self.error_count_1h}, 6h: {self.error_count_6h}) ---",
            "",
            "--- GIT STATE ---",
            f"Last commit: {self.git_last_commit}",
            f"Uncommitted: {self.git_uncommitted or 'clean'}",
            "",
            "--- RECENT JOURNAL (last 100 lines) ---",
            self.journal_recent or "(unavailable)",
            "",
            "--- STATUS.JSON ---",
            self.status_json or "(unavailable)",
            "",
            "--- WATCHDOG STATE ---",
            self.watchdog_state or "(unavailable)",
        ])
        return "\n".join(lines)


async def _incus_exec(
    container: str, *cmd: str, timeout: float = _INCUS_TIMEOUT,
) -> tuple[int, str]:
    """Run a command inside the container. Returns (rc, stdout)."""
    from genesis.guardian.health_signals import _run_subprocess

    rc, stdout, _stderr = await _run_subprocess(
        "incus", "exec", container, "--", *cmd,
        timeout=timeout,
    )
    return rc, stdout


async def _incus_exec_user(
    container: str, cmd_str: str, timeout: float = _INCUS_TIMEOUT,
) -> tuple[int, str]:
    """Run a command as the ubuntu user inside the container."""
    from genesis.guardian.health_signals import _run_subprocess

    rc, stdout, _stderr = await _run_subprocess(
        "incus", "exec", container, "--",
        "su", "-", "ubuntu", "-c", cmd_str,
        timeout=timeout,
    )
    return rc, stdout


# ── Individual collectors ───────────────────────────────────────────────


async def _collect_container_info(config: GuardianConfig) -> tuple[str, str]:
    """Collect container status and uptime."""
    try:
        from genesis.guardian.health_signals import _run_subprocess
        rc, stdout, _ = await _run_subprocess(
            "incus", "info", config.container_name,
            timeout=_INCUS_TIMEOUT,
        )
        if rc != 0:
            return "unknown", "unknown"
        status = "unknown"
        uptime = "unknown"
        for line in stdout.splitlines():
            if line.strip().lower().startswith("status:"):
                status = line.split(":", 1)[1].strip()
            elif "created" in line.lower() or "started" in line.lower():
                uptime = line.strip()
        return status, uptime
    except Exception as exc:
        logger.warning("Failed to collect container info: %s", exc, exc_info=True)
        return "error", str(exc)


async def _collect_processes(config: GuardianConfig) -> tuple[str, int, int]:
    """Collect top processes and zombie/D-state counts."""
    try:
        rc, top_out = await _incus_exec(
            config.container_name,
            "ps", "aux", "--sort=-%mem",
        )
        top_processes = top_out[:3000] if rc == 0 else "(unavailable)"

        rc2, ps_out = await _incus_exec(
            config.container_name,
            "bash", "-c",
            "ps axo stat | grep -c '^Z' 2>/dev/null; "
            "ps axo stat | grep -c '^D' 2>/dev/null",
        )
        zombie = 0
        dstate = 0
        if rc2 == 0:
            lines = ps_out.strip().splitlines()
            if len(lines) >= 1 and lines[0].strip().isdigit():
                zombie = int(lines[0].strip())
            if len(lines) >= 2 and lines[1].strip().isdigit():
                dstate = int(lines[1].strip())

        return top_processes, zombie, dstate
    except Exception as exc:
        logger.warning("Failed to collect process info: %s", exc, exc_info=True)
        return "(error)", 0, 0


async def _collect_memory(config: GuardianConfig) -> MemoryInfo:
    """Collect memory metrics from cgroup v2."""
    info = MemoryInfo()
    try:
        rc, out = await _incus_exec(
            config.container_name,
            "cat", "/sys/fs/cgroup/memory.current",
        )
        if rc == 0:
            info.current_bytes = int(out.strip())

        rc, out = await _incus_exec(
            config.container_name,
            "cat", "/sys/fs/cgroup/memory.max",
        )
        if rc == 0 and out.strip() != "max":
            info.max_bytes = int(out.strip())
            if info.max_bytes > 0:
                info.usage_pct = (info.current_bytes / info.max_bytes) * 100

        rc, out = await _incus_exec(
            config.container_name,
            "cat", "/sys/fs/cgroup/memory.pressure",
        )
        if rc == 0:
            info.pressure_full_10s, info.pressure_full_60s = _parse_pressure(out, "full")
    except Exception as exc:
        logger.warning("Failed to collect memory info: %s", exc, exc_info=True)
    return info


async def _collect_io(config: GuardianConfig) -> IOInfo:
    """Collect I/O pressure from cgroup v2."""
    info = IOInfo()
    try:
        rc, out = await _incus_exec(
            config.container_name,
            "cat", "/sys/fs/cgroup/io.pressure",
        )
        if rc == 0:
            info.pressure_full_10s, info.pressure_full_60s = _parse_pressure(out, "full")
    except Exception as exc:
        logger.warning("Failed to collect I/O info: %s", exc, exc_info=True)
    return info


async def _collect_cpu(config: GuardianConfig) -> CPUInfo:
    """Collect CPU metrics from cgroup v2."""
    info = CPUInfo()
    try:
        rc, out = await _incus_exec(
            config.container_name,
            "cat", "/sys/fs/cgroup/cpu.stat",
        )
        if rc == 0:
            for line in out.splitlines():
                if line.startswith("usage_usec"):
                    info.usage_usec = int(line.split()[1])
                    break

        rc, out = await _incus_exec(
            config.container_name,
            "cat", "/sys/fs/cgroup/cpu.pressure",
        )
        if rc == 0:
            info.pressure_some_10s, _ = _parse_pressure(out, "some")
    except Exception as exc:
        logger.warning("Failed to collect CPU info: %s", exc, exc_info=True)
    return info


async def _collect_disk(config: GuardianConfig) -> list[DiskInfo]:
    """Collect disk usage for key mount points."""
    disks = []
    for mount in ["/", "/tmp", "/home"]:
        try:
            rc, out = await _incus_exec(
                config.container_name,
                "df", "-m", "--output=target,size,used,avail,pcent", mount,
            )
            if rc != 0:
                continue
            lines = out.strip().splitlines()
            if len(lines) < 2:
                continue
            parts = lines[-1].split()
            if len(parts) >= 5:
                disks.append(DiskInfo(
                    mount=parts[0],
                    total_mb=int(parts[1]),
                    used_mb=int(parts[2]),
                    avail_mb=int(parts[3]),
                    usage_pct=float(parts[4].rstrip("%")),
                ))
        except Exception as exc:
            logger.warning("Failed to collect disk info for %s: %s", mount, exc, exc_info=True)
    return disks


async def _collect_services(config: GuardianConfig) -> list[ServiceInfo]:
    """Collect systemd service status."""
    services = []
    for svc in ["genesis-bridge", "genesis-watchdog.timer", "qdrant"]:
        try:
            is_user = svc != "qdrant"
            if is_user:
                rc, out = await _incus_exec_user(
                    config.container_name,
                    f"systemctl --user show {svc} "
                    "-p ActiveState,SubState,NRestarts --value",
                )
            else:
                rc, out = await _incus_exec(
                    config.container_name,
                    "systemctl", "show", svc,
                    "-p", "ActiveState,SubState,NRestarts", "--value",
                )
            if rc == 0:
                lines = out.strip().splitlines()
                active_state = lines[0] if len(lines) > 0 else "unknown"
                sub_state = lines[1] if len(lines) > 1 else "unknown"
                n_restarts = int(lines[2]) if len(lines) > 2 and lines[2].isdigit() else 0
                services.append(ServiceInfo(
                    name=svc,
                    active=active_state == "active",
                    sub_state=sub_state,
                    n_restarts=n_restarts,
                ))
        except Exception as exc:
            logger.warning("Failed to collect service info for %s: %s", svc, exc, exc_info=True)
    return services


async def _collect_journal(config: GuardianConfig) -> tuple[str, int, int]:
    """Collect recent journal entries and error counts."""
    journal_recent = ""
    error_1h = 0
    error_6h = 0
    try:
        rc, out = await _incus_exec_user(
            config.container_name,
            "journalctl --user -n 100 --no-pager -o short-iso",
            timeout=20.0,
        )
        if rc == 0:
            journal_recent = out

        rc, out = await _incus_exec_user(
            config.container_name,
            "journalctl --user -p err --since '1 hour ago' --no-pager -o cat | wc -l",
        )
        if rc == 0 and out.strip().isdigit():
            error_1h = int(out.strip())

        rc, out = await _incus_exec_user(
            config.container_name,
            "journalctl --user -p err --since '6 hours ago' --no-pager -o cat | wc -l",
        )
        if rc == 0 and out.strip().isdigit():
            error_6h = int(out.strip())
    except Exception as exc:
        logger.warning("Failed to collect journal info: %s", exc, exc_info=True)
    return journal_recent, error_1h, error_6h


async def _collect_git(config: GuardianConfig) -> tuple[str, str]:
    """Collect git state from the Genesis repo."""
    last_commit = ""
    uncommitted = ""
    try:
        rc, out = await _incus_exec_user(
            config.container_name,
            "cd ~/genesis && git log --oneline -1",
        )
        if rc == 0:
            last_commit = out.strip()

        rc, out = await _incus_exec_user(
            config.container_name,
            "cd ~/genesis && git status --short",
        )
        if rc == 0:
            uncommitted = out.strip()
    except Exception as exc:
        logger.warning("Failed to collect git info: %s", exc, exc_info=True)
    return last_commit, uncommitted


async def _collect_status_files(config: GuardianConfig) -> tuple[str, str]:
    """Read status.json and watchdog_state.json from the container."""
    status = ""
    watchdog = ""
    try:
        rc, out = await _incus_exec(
            config.container_name,
            "cat", f"/home/{config.container_user}/.genesis/status.json",
        )
        if rc == 0:
            status = out.strip()
    except Exception as exc:
        logger.debug("Failed to read status.json: %s", exc)
    try:
        rc, out = await _incus_exec(
            config.container_name,
            "cat", f"/home/{config.container_user}/.genesis/watchdog_state.json",
        )
        if rc == 0:
            watchdog = out.strip()
    except Exception as exc:
        logger.debug("Failed to read watchdog_state.json: %s", exc)
    return status, watchdog


def _parse_pressure(text: str, prefix: str) -> tuple[float, float]:
    """Parse cgroup pressure file for avg10 and avg60 values.

    Format: some avg10=0.00 avg60=0.00 avg300=0.00 total=0
            full avg10=0.00 avg60=0.00 avg300=0.00 total=0
    """
    for line in text.splitlines():
        if line.strip().startswith(prefix):
            parts = line.split()
            avg10 = avg60 = 0.0
            for part in parts:
                if part.startswith("avg10="):
                    avg10 = float(part.split("=")[1])
                elif part.startswith("avg60="):
                    avg60 = float(part.split("=")[1])
            return avg10, avg60
    return 0.0, 0.0


# ── Main collector ──────────────────────────────────────────────────────


async def collect_diagnostics(config: GuardianConfig) -> DiagnosticSnapshot:
    """Run all diagnostic collectors in parallel."""
    now = datetime.now(UTC).isoformat()

    # Run all collectors concurrently
    results = await asyncio.gather(
        _collect_container_info(config),
        _collect_processes(config),
        _collect_memory(config),
        _collect_io(config),
        _collect_cpu(config),
        _collect_disk(config),
        _collect_services(config),
        _collect_journal(config),
        _collect_git(config),
        _collect_status_files(config),
        return_exceptions=True,
    )

    snap = DiagnosticSnapshot(collected_at=now)

    # Unpack results safely
    if isinstance(results[0], tuple):
        snap.container_status, snap.uptime = results[0]
    if isinstance(results[1], tuple):
        snap.top_processes, snap.zombie_count, snap.dstate_count = results[1]
    if isinstance(results[2], MemoryInfo):
        snap.memory = results[2]
    if isinstance(results[3], IOInfo):
        snap.io = results[3]
    if isinstance(results[4], CPUInfo):
        snap.cpu = results[4]
    if isinstance(results[5], list):
        snap.disks = results[5]
    if isinstance(results[6], list):
        snap.services = results[6]
    if isinstance(results[7], tuple):
        snap.journal_recent, snap.error_count_1h, snap.error_count_6h = results[7]
    if isinstance(results[8], tuple):
        snap.git_last_commit, snap.git_uncommitted = results[8]
    if isinstance(results[9], tuple):
        snap.status_json, snap.watchdog_state = results[9]

    # Log any collector exceptions
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error("Diagnostic collector %d failed: %s", i, result, exc_info=result)

    return snap
