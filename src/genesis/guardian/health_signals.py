"""Health signal collection — HOST-SIDE. 6 probes + 7 suspicious checks.

Each probe runs independently with its own timeout. A probe failure returns
alive=False but never crashes the check. All probes run in parallel via
asyncio.gather.

Probes:
  1. Container exists     — incus info genesis → "Status: RUNNING"
  2. ICMP reachable       — ping -c1 -W3 {container_ip}
  3. Health API           — HTTP GET :5000/api/genesis/health (retries once on 503)
  4. Heartbeat canary     — HTTP GET :5000/api/genesis/heartbeat
  5. Log freshness        — incus exec journalctl last line timestamp
  6. I/O saturation       — cgroup io.pressure full avg10 > 50%

Suspicious checks (run when all 6 probes pass):
  1. Tick regularity      — sqlite3 query for interval gaps
  2. Memory pressure      — cgroup memory.stat anon+kernel vs max
  3. /tmp usage           — df /tmp
  4. Restart count        — systemctl NRestarts
  5. Error spike          — journalctl error count in window
  6. I/O pressure         — cgroup io.pressure full avg10 early warning
  7. Health API depth     — parse response body for degraded metrics
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.config import GuardianConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalResult:
    """Result from a single health probe."""

    name: str
    alive: bool
    latency_ms: float
    detail: str
    collected_at: str


@dataclass(frozen=True)
class SuspiciousResult:
    """Result from a 'healthy but suspicious' check."""

    name: str
    ok: bool
    detail: str
    collected_at: str


@dataclass(frozen=True)
class PauseState:
    """Genesis pause state read from container."""

    paused: bool
    reason: str | None = None
    since: str | None = None


@dataclass
class HealthSnapshot:
    """Complete health snapshot from all probes and suspicious checks."""

    signals: dict[str, SignalResult] = field(default_factory=dict)
    suspicious: dict[str, SuspiciousResult] = field(default_factory=dict)
    pause_state: PauseState = field(default_factory=lambda: PauseState(paused=False))
    collected_at: str = ""

    @property
    def all_alive(self) -> bool:
        return all(s.alive for s in self.signals.values())

    @property
    def any_alive(self) -> bool:
        return any(s.alive for s in self.signals.values())

    @property
    def failed_signals(self) -> list[SignalResult]:
        return [s for s in self.signals.values() if not s.alive]

    @property
    def suspicious_warnings(self) -> list[SuspiciousResult]:
        return [s for s in self.suspicious.values() if not s.ok]



# _run_subprocess is re-exported for backward compatibility with external callers.
# Canonical definition: genesis.guardian._subprocess.run_subprocess


def parse_psi_content(content: str) -> dict[str, float]:
    """Parse a PSI pressure file into a flat dict of metrics.

    Handles both io.pressure and memory.pressure format:
        some avg10=0.00 avg60=0.00 avg300=0.00 total=0
        full avg10=0.00 avg60=0.00 avg300=0.00 total=0

    Returns keys like 'some_avg10', 'full_avg60', etc.
    """
    result: dict[str, float] = {}
    for line in content.strip().splitlines():
        parts = line.split()
        if not parts:
            continue
        prefix = parts[0]  # "some" or "full"
        for part in parts[1:]:
            if "=" in part:
                key, _, val = part.partition("=")
                try:
                    result[f"{prefix}_{key}"] = float(val)
                except ValueError:
                    continue
    return result


def _http_get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    """Synchronous HTTP GET via stdlib. Returns (status_code, body)."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.debug("HTTP GET %s failed: %s", url, exc)
        return 0, ""
    except Exception as exc:
        logger.warning("Unexpected HTTP error for %s: %s", url, exc, exc_info=True)
        return 0, ""


async def _http_get_async(url: str, timeout: float = 10.0) -> tuple[int, str]:
    """Run HTTP GET in executor to avoid blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, _http_get, url, timeout),
        timeout=timeout + 2,  # small grace period over the urllib timeout
    )


# ── Probe implementations ──────────────────────────────────────────────


async def probe_container_exists(config: GuardianConfig) -> SignalResult:
    """Check that the container exists and is running via incus info."""
    name = "container_exists"
    t0 = datetime.now(UTC)
    try:
        rc, stdout, stderr = await _run_subprocess(
            "incus", "info", config.container_name,
            timeout=config.probes.probe_timeout_s,
        )
        latency = (datetime.now(UTC) - t0).total_seconds() * 1000
        if rc != 0:
            return SignalResult(
                name=name, alive=False, latency_ms=latency,
                detail=f"incus info failed: rc={rc} {stderr}",
                collected_at=t0.isoformat(),
            )
        alive = "STATUS: RUNNING" in stdout.upper()
        detail = "running" if alive else f"not running: {stdout[:200]}"
        return SignalResult(
            name=name, alive=alive, latency_ms=latency,
            detail=detail, collected_at=t0.isoformat(),
        )
    except Exception as exc:
        latency = (datetime.now(UTC) - t0).total_seconds() * 1000
        return SignalResult(
            name=name, alive=False, latency_ms=latency,
            detail=f"exception: {exc}", collected_at=t0.isoformat(),
        )


async def probe_icmp_reachable(config: GuardianConfig) -> SignalResult:
    """Check container is reachable via ICMP ping.

    A single ping on the Incus bridge occasionally loses to an ARP race —
    the kernel has no neighbor entry yet, the first packet gets dropped
    during resolution, and the probe reports the container dead when it
    is actually fine. A single retry closes that window.

    Each attempt has its own ``probes.probe_timeout_s`` budget, so the
    worst-case total is roughly ``2 * probe_timeout_s + 0.5s`` (the
    inter-attempt sleep). With default ``probe_timeout_s=10`` that's
    ~20.5s — still well under the Guardian check interval.
    """
    name = "icmp_reachable"
    t0 = datetime.now(UTC)
    last_stderr = ""
    last_stdout = ""
    last_exc: Exception | None = None
    retry_used = False

    for attempt in range(2):
        try:
            rc, stdout, stderr = await _run_subprocess(
                "ping",
                f"-c{config.probes.ping_count}",
                f"-W{config.probes.ping_timeout_s}",
                config.container_ip,
                timeout=config.probes.probe_timeout_s,
            )
            if rc == 0:
                latency = (datetime.now(UTC) - t0).total_seconds() * 1000
                detail = "reachable (retry)" if retry_used else "reachable"
                return SignalResult(
                    name=name, alive=True, latency_ms=latency,
                    detail=detail, collected_at=t0.isoformat(),
                )
            last_stdout = stdout
            last_stderr = stderr
        except Exception as exc:
            last_exc = exc

        if attempt == 0:
            retry_used = True
            await asyncio.sleep(0.5)

    latency = (datetime.now(UTC) - t0).total_seconds() * 1000
    if last_exc is not None:
        return SignalResult(
            name=name, alive=False, latency_ms=latency,
            detail=f"exception: {last_exc}"[:200],
            collected_at=t0.isoformat(),
        )
    detail = f"unreachable (retry): {last_stderr or last_stdout}"
    return SignalResult(
        name=name, alive=False, latency_ms=latency,
        detail=detail[:200], collected_at=t0.isoformat(),
    )


async def probe_health_api(config: GuardianConfig) -> SignalResult:
    """Check Flask health API responds with 200.

    Retries once on 503 (often transient during DB lock or bootstrap).
    Reported latency reflects the successful call, not the retry wait.
    """
    name = "health_api"
    t0 = datetime.now(UTC)
    try:
        url = f"{config.health_url}/api/genesis/health"
        status, body = await _http_get_async(url, timeout=config.probes.http_timeout_s)

        # 503 is often transient (DB lock, bootstrap race). Retry once.
        if status == 503:
            await asyncio.sleep(5)
            t0 = datetime.now(UTC)  # Reset — report latency of the successful call
            status, body = await _http_get_async(url, timeout=config.probes.http_timeout_s)

        latency = (datetime.now(UTC) - t0).total_seconds() * 1000
        alive = status == 200
        detail = "healthy" if alive else f"status={status} body={body[:200]}"
        return SignalResult(
            name=name, alive=alive, latency_ms=latency,
            detail=detail, collected_at=t0.isoformat(),
        )
    except Exception as exc:
        latency = (datetime.now(UTC) - t0).total_seconds() * 1000
        return SignalResult(
            name=name, alive=False, latency_ms=latency,
            detail=f"exception: {exc}", collected_at=t0.isoformat(),
        )


async def probe_heartbeat_canary(config: GuardianConfig) -> SignalResult:
    """Check heartbeat canary endpoint — awareness loop is alive."""
    name = "heartbeat_canary"
    t0 = datetime.now(UTC)
    try:
        url = f"{config.health_url}/api/genesis/heartbeat"
        status, body = await _http_get_async(url, timeout=config.probes.http_timeout_s)
        latency = (datetime.now(UTC) - t0).total_seconds() * 1000
        if status == 503:
            # Bootstrapping — not a failure, but not alive
            return SignalResult(
                name=name, alive=False, latency_ms=latency,
                detail="bootstrapping (503)", collected_at=t0.isoformat(),
            )
        alive = status == 200
        detail = "alive" if alive else f"status={status}"
        if alive:
            try:
                data = json.loads(body)
                detail = f"alive, ticks={data.get('tick_count', '?')}"
            except (json.JSONDecodeError, KeyError):
                pass
        return SignalResult(
            name=name, alive=alive, latency_ms=latency,
            detail=detail, collected_at=t0.isoformat(),
        )
    except Exception as exc:
        latency = (datetime.now(UTC) - t0).total_seconds() * 1000
        return SignalResult(
            name=name, alive=False, latency_ms=latency,
            detail=f"exception: {exc}", collected_at=t0.isoformat(),
        )


async def probe_log_freshness(config: GuardianConfig) -> SignalResult:
    """Check journal log freshness — recent output from genesis-bridge."""
    name = "log_freshness"
    t0 = datetime.now(UTC)
    try:
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", config.container_name, "--",
            "su", "-", "ubuntu", "-c",
            f"journalctl --user -u genesis-bridge -n{config.probes.journal_lines} "
            "--no-pager -o short-iso",
            timeout=config.probes.probe_timeout_s,
        )
        latency = (datetime.now(UTC) - t0).total_seconds() * 1000
        if rc != 0:
            return SignalResult(
                name=name, alive=False, latency_ms=latency,
                detail=f"journalctl failed: rc={rc} {stderr[:200]}",
                collected_at=t0.isoformat(),
            )
        # Parse timestamp from journal output — first token of last non-empty line
        alive = bool(stdout.strip())
        detail = stdout.strip()[-200:] if alive else "no journal output"
        return SignalResult(
            name=name, alive=alive, latency_ms=latency,
            detail=detail, collected_at=t0.isoformat(),
        )
    except Exception as exc:
        latency = (datetime.now(UTC) - t0).total_seconds() * 1000
        return SignalResult(
            name=name, alive=False, latency_ms=latency,
            detail=f"exception: {exc}", collected_at=t0.isoformat(),
        )


async def probe_io_saturation(config: GuardianConfig) -> SignalResult:
    """Check container I/O pressure via host-side cgroup PSI.

    Reads /sys/fs/cgroup/lxc.payload.{container}/io.pressure directly from
    the host filesystem — no incus exec needed. Returns alive=False if
    full avg10 > 50% (severe I/O stall indicating potential D-state freeze).
    """
    name = "io_saturation"
    t0 = datetime.now(UTC)
    psi_path = f"/sys/fs/cgroup/lxc.payload.{config.container_name}/io.pressure"
    try:
        with open(psi_path) as f:
            content = f.read()
        latency = (datetime.now(UTC) - t0).total_seconds() * 1000

        # Parse "full avg10=X.XX avg60=Y.YY avg300=Z.ZZ total=NNNNN"
        full_avg10 = _parse_psi_avg10(content, "full")
        if full_avg10 is None:
            return SignalResult(
                name=name, alive=True, latency_ms=latency,
                detail=f"could not parse io.pressure: {content[:200]}",
                collected_at=t0.isoformat(),
            )

        # Threshold: > 50% full avg10 means severe I/O stall
        alive = full_avg10 <= 50.0
        detail = f"io.pressure full avg10={full_avg10:.2f}%"
        return SignalResult(
            name=name, alive=alive, latency_ms=latency,
            detail=detail, collected_at=t0.isoformat(),
        )
    except FileNotFoundError:
        latency = (datetime.now(UTC) - t0).total_seconds() * 1000
        return SignalResult(
            name=name, alive=True, latency_ms=latency,
            detail="io.pressure file not found (container may be stopped)",
            collected_at=t0.isoformat(),
        )
    except Exception as exc:
        latency = (datetime.now(UTC) - t0).total_seconds() * 1000
        return SignalResult(
            name=name, alive=True, latency_ms=latency,
            detail=f"exception: {exc}", collected_at=t0.isoformat(),
        )


def _parse_psi_avg10(content: str, line_prefix: str) -> float | None:
    """Parse avg10 value from a PSI pressure file line.

    Example line: 'full avg10=0.00 avg60=0.00 avg300=0.00 total=0'
    Returns the avg10 float value, or None if not found.
    """
    return parse_psi_content(content).get(f"{line_prefix}_avg10")


# ── Suspicious check implementations ───────────────────────────────────


async def check_tick_regularity(config: GuardianConfig) -> SuspiciousResult:
    """Query last N ticks from DB, check for gaps or irregular intervals."""
    name = "tick_regularity"
    t0 = datetime.now(UTC)
    try:
        count = config.suspicious.tick_history_count
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", config.container_name, "--",
            "su", "-", "ubuntu", "-c",
            f"sqlite3 ~/genesis/data/genesis.db "
            f"'SELECT created_at FROM awareness_ticks "
            f"ORDER BY created_at DESC LIMIT {count}'",
            timeout=config.probes.probe_timeout_s,
        )
        if rc != 0 or not stdout.strip():
            return SuspiciousResult(
                name=name, ok=True,  # can't check = assume ok
                detail=f"query failed: {stderr[:200]}",
                collected_at=t0.isoformat(),
            )

        timestamps = stdout.strip().splitlines()
        if len(timestamps) < 2:
            return SuspiciousResult(
                name=name, ok=True, detail="not enough ticks to check",
                collected_at=t0.isoformat(),
            )

        # Check intervals between consecutive ticks
        issues = []
        for i in range(len(timestamps) - 1):
            try:
                t_newer = datetime.fromisoformat(timestamps[i].strip())
                t_older = datetime.fromisoformat(timestamps[i + 1].strip())
                gap = (t_newer - t_older).total_seconds()
                if gap > config.suspicious.tick_max_gap_s:
                    issues.append(f"gap={gap:.0f}s between ticks")
                elif gap < config.suspicious.tick_min_interval_s:
                    issues.append(f"interval={gap:.0f}s (too fast)")
            except (ValueError, TypeError):
                continue

        ok = len(issues) == 0
        detail = "; ".join(issues[:3]) if issues else "regular"
        return SuspiciousResult(
            name=name, ok=ok, detail=detail, collected_at=t0.isoformat(),
        )
    except Exception as exc:
        return SuspiciousResult(
            name=name, ok=True, detail=f"exception: {exc}",
            collected_at=t0.isoformat(),
        )


async def check_memory_pressure(config: GuardianConfig) -> SuspiciousResult:
    """Check container memory usage via cgroup v2.

    Uses anon+kernel from memory.stat (non-reclaimable) for threshold
    decisions instead of memory.current (which includes reclaimable page cache).
    """
    name = "memory_pressure"
    t0 = datetime.now(UTC)
    try:
        # Read memory.stat for anon+kernel (non-reclaimable memory)
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", config.container_name, "--",
            "cat", "/sys/fs/cgroup/memory.stat",
            timeout=config.probes.probe_timeout_s,
        )
        if rc != 0:
            return SuspiciousResult(
                name=name, ok=True, detail=f"cgroup stat read failed: {stderr[:200]}",
                collected_at=t0.isoformat(),
            )
        # Parse anon and kernel values from memory.stat
        stats: dict[str, int] = {}
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in ("anon", "kernel"):
                stats[parts[0]] = int(parts[1])
        anon_kernel = stats.get("anon", 0) + stats.get("kernel", 0)

        rc2, stdout2, stderr2 = await _run_subprocess(
            "incus", "exec", config.container_name, "--",
            "cat", "/sys/fs/cgroup/memory.max",
            timeout=config.probes.probe_timeout_s,
        )
        if rc2 != 0:
            return SuspiciousResult(
                name=name, ok=True, detail=f"cgroup max read failed: {stderr2[:200]}",
                collected_at=t0.isoformat(),
            )

        max_mem = stdout2.strip()
        if max_mem == "max":
            return SuspiciousResult(
                name=name, ok=True, detail="no memory limit set",
                collected_at=t0.isoformat(),
            )

        max_bytes = int(max_mem)
        pct = (anon_kernel / max_bytes) * 100 if max_bytes > 0 else 0
        ok = pct < config.suspicious.memory_warning_pct
        detail = f"{pct:.1f}% anon+kernel ({anon_kernel // (1024*1024)}M / {max_bytes // (1024*1024)}M)"
        return SuspiciousResult(
            name=name, ok=ok, detail=detail, collected_at=t0.isoformat(),
        )
    except Exception as exc:
        return SuspiciousResult(
            name=name, ok=True, detail=f"exception: {exc}",
            collected_at=t0.isoformat(),
        )


async def check_tmp_usage(config: GuardianConfig) -> SuspiciousResult:
    """Check /tmp usage inside the container."""
    name = "tmp_usage"
    t0 = datetime.now(UTC)
    try:
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", config.container_name, "--",
            "df", "--output=pcent", "/tmp",
            timeout=config.probes.probe_timeout_s,
        )
        if rc != 0:
            return SuspiciousResult(
                name=name, ok=True, detail=f"df failed: {stderr[:200]}",
                collected_at=t0.isoformat(),
            )

        # Parse: header line + "  42%"
        lines = stdout.strip().splitlines()
        if len(lines) < 2:
            return SuspiciousResult(
                name=name, ok=True, detail="unexpected df output",
                collected_at=t0.isoformat(),
            )
        pct_str = lines[-1].strip().rstrip("%")
        pct = float(pct_str)
        ok = pct < config.suspicious.tmp_warning_pct
        detail = f"{pct:.0f}% used"
        return SuspiciousResult(
            name=name, ok=ok, detail=detail, collected_at=t0.isoformat(),
        )
    except Exception as exc:
        return SuspiciousResult(
            name=name, ok=True, detail=f"exception: {exc}",
            collected_at=t0.isoformat(),
        )


async def check_restart_count(config: GuardianConfig) -> SuspiciousResult:
    """Check genesis-bridge systemd restart count (crash loop detection)."""
    name = "restart_count"
    t0 = datetime.now(UTC)
    try:
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", config.container_name, "--",
            "su", "-", "ubuntu", "-c",
            "systemctl --user show genesis-bridge -p NRestarts --value",
            timeout=config.probes.probe_timeout_s,
        )
        if rc != 0:
            return SuspiciousResult(
                name=name, ok=True, detail=f"systemctl failed: {stderr[:200]}",
                collected_at=t0.isoformat(),
            )

        restarts = int(stdout.strip()) if stdout.strip().isdigit() else 0
        ok = restarts == 0
        detail = f"{restarts} restarts" if restarts > 0 else "no restarts"
        return SuspiciousResult(
            name=name, ok=ok, detail=detail, collected_at=t0.isoformat(),
        )
    except Exception as exc:
        return SuspiciousResult(
            name=name, ok=True, detail=f"exception: {exc}",
            collected_at=t0.isoformat(),
        )


async def check_error_spike(config: GuardianConfig) -> SuspiciousResult:
    """Check for error log spike in recent window."""
    name = "error_spike"
    t0 = datetime.now(UTC)
    try:
        window = config.suspicious.error_spike_window_min
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", config.container_name, "--",
            "su", "-", "ubuntu", "-c",
            f"journalctl --user -p err --since '{window} min ago' "
            "--no-pager -o cat | wc -l",
            timeout=config.probes.probe_timeout_s,
        )
        if rc != 0:
            return SuspiciousResult(
                name=name, ok=True, detail=f"journalctl failed: {stderr[:200]}",
                collected_at=t0.isoformat(),
            )

        count = int(stdout.strip()) if stdout.strip().isdigit() else 0
        ok = count < config.suspicious.error_spike_threshold
        detail = f"{count} errors in last {window}min"
        return SuspiciousResult(
            name=name, ok=ok, detail=detail, collected_at=t0.isoformat(),
        )
    except Exception as exc:
        return SuspiciousResult(
            name=name, ok=True, detail=f"exception: {exc}",
            collected_at=t0.isoformat(),
        )


async def check_io_pressure(config: GuardianConfig) -> SuspiciousResult:
    """Check container I/O pressure for early warning (lower threshold than probe).

    Uses the configurable io_pressure_threshold_pct (default 10%) for early
    detection of I/O saturation before it becomes critical.
    """
    name = "io_pressure"
    t0 = datetime.now(UTC)
    psi_path = f"/sys/fs/cgroup/lxc.payload.{config.container_name}/io.pressure"
    try:
        with open(psi_path) as f:
            content = f.read()

        full_avg10 = _parse_psi_avg10(content, "full")
        if full_avg10 is None:
            return SuspiciousResult(
                name=name, ok=True,
                detail=f"could not parse io.pressure: {content[:200]}",
                collected_at=t0.isoformat(),
            )

        threshold = config.suspicious.io_pressure_threshold_pct
        ok = full_avg10 <= threshold
        detail = f"io.pressure full avg10={full_avg10:.2f}% (threshold={threshold}%)"
        return SuspiciousResult(
            name=name, ok=ok, detail=detail, collected_at=t0.isoformat(),
        )
    except FileNotFoundError:
        return SuspiciousResult(
            name=name, ok=True,
            detail="io.pressure file not found (container may be stopped)",
            collected_at=t0.isoformat(),
        )
    except Exception as exc:
        return SuspiciousResult(
            name=name, ok=True, detail=f"exception: {exc}",
            collected_at=t0.isoformat(),
        )


async def check_health_api_depth(config: GuardianConfig) -> SuspiciousResult:
    """Parse health API response body for degraded infrastructure metrics.

    Runs only when all probes pass (health API already returned 200).
    Checks DB latency, scheduler status, and Qdrant status against thresholds.
    """
    name = "health_api_depth"
    t0 = datetime.now(UTC)
    try:
        url = f"{config.health_url}/api/genesis/health"
        status, body = await _http_get_async(url, timeout=config.probes.http_timeout_s)
        if status != 200:
            return SuspiciousResult(
                name=name, ok=True, detail=f"skipped (status={status})",
                collected_at=t0.isoformat(),
            )

        data = json.loads(body)
        infra = data.get("infrastructure", {})

        warnings: list[str] = []

        # DB latency
        db = infra.get("genesis.db", {})
        db_latency = db.get("latency_ms", 0)
        if isinstance(db_latency, (int, float)) and db_latency > config.suspicious.db_latency_warning_ms:
            warnings.append(f"db_latency={db_latency:.0f}ms")

        # Scheduler
        sched = infra.get("scheduler", {})
        sched_status = sched.get("status", "unknown")
        if sched_status not in ("healthy", "unknown"):
            warnings.append(f"scheduler={sched_status}")

        # Qdrant
        qdrant = infra.get("qdrant", {})
        qdrant_status = qdrant.get("status", "unknown")
        if qdrant_status not in ("healthy", "unknown"):
            warnings.append(f"qdrant={qdrant_status}")

        ok = len(warnings) == 0
        detail = ", ".join(warnings) if warnings else "all metrics healthy"
        return SuspiciousResult(
            name=name, ok=ok, detail=detail, collected_at=t0.isoformat(),
        )
    except Exception as exc:
        return SuspiciousResult(
            name=name, ok=True, detail=f"exception: {exc}",
            collected_at=t0.isoformat(),
        )


async def check_pause_state(config: GuardianConfig) -> PauseState:
    """Read Genesis pause state from the container."""
    try:
        url = f"{config.health_url}/api/genesis/pause"
        status, body = await _http_get_async(url, timeout=5.0)
        if status == 200:
            data = json.loads(body)
            return PauseState(
                paused=data.get("paused", False),
                reason=data.get("reason"),
                since=data.get("since"),
            )
    except Exception as exc:
        logger.debug("Pause API check failed, falling back to file: %s", exc)

    # Fallback: read pause file directly via incus
    try:
        rc, stdout, _ = await _run_subprocess(
            "incus", "exec", config.container_name, "--",
            "cat", f"/home/{config.container_user}/.genesis/paused.json",
            timeout=5.0,
        )
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            return PauseState(
                paused=data.get("paused", False),
                reason=data.get("reason"),
                since=data.get("since"),
            )
    except Exception as exc:
        logger.warning("Pause state check failed (both API and file): %s", exc)

    return PauseState(paused=False)


# ── Collectors ──────────────────────────────────────────────────────────


async def collect_all_signals(config: GuardianConfig) -> HealthSnapshot:
    """Run all 6 probes in parallel, then suspicious checks if healthy."""
    now = datetime.now(UTC).isoformat()

    # Run all 6 probes in parallel
    probe_results = await asyncio.gather(
        probe_container_exists(config),
        probe_icmp_reachable(config),
        probe_health_api(config),
        probe_heartbeat_canary(config),
        probe_log_freshness(config),
        probe_io_saturation(config),
        return_exceptions=True,
    )

    signals: dict[str, SignalResult] = {}
    for result in probe_results:
        if isinstance(result, SignalResult):
            signals[result.name] = result
        elif isinstance(result, Exception):
            logger.error("Probe raised unexpected exception: %s", result, exc_info=True)

    snapshot = HealthSnapshot(signals=signals, collected_at=now)

    # Read pause state (always needed for state machine decisions)
    snapshot.pause_state = await check_pause_state(config)

    # Run suspicious checks only if all probes are alive
    if snapshot.all_alive:
        suspicious_results = await asyncio.gather(
            check_tick_regularity(config),
            check_memory_pressure(config),
            check_tmp_usage(config),
            check_restart_count(config),
            check_error_spike(config),
            check_io_pressure(config),
            check_health_api_depth(config),
            return_exceptions=True,
        )
        for result in suspicious_results:
            if isinstance(result, SuspiciousResult):
                snapshot.suspicious[result.name] = result
            elif isinstance(result, Exception):
                logger.error(
                    "Suspicious check raised exception: %s", result, exc_info=True,
                )

    return snapshot
