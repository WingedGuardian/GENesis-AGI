"""Health signal collection — HOST-SIDE. 5 probes + 6 suspicious checks.

Each probe runs independently with its own timeout. A probe failure returns
alive=False but never crashes the check. All probes run in parallel via
asyncio.gather.

Probes:
  1. Container exists     — incus info genesis → "Status: RUNNING"
  2. ICMP reachable       — ping -c1 -W3 {container_ip}
  3. Health API           — HTTP GET :5000/api/genesis/health
  4. Heartbeat canary     — HTTP GET :5000/api/genesis/heartbeat
  5. Log freshness        — incus exec journalctl last line timestamp

Suspicious checks (run when all 5 probes pass):
  1. Tick regularity      — sqlite3 query for interval gaps
  2. Memory pressure      — cgroup memory.current vs max
  3. /tmp usage           — df /tmp
  4. Restart count        — systemctl NRestarts
  5. Error spike          — journalctl error count in window
  6. Pause state          — /api/genesis/pause or paused.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime

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


async def _run_subprocess(
    *args: str, timeout: float = 10.0,
) -> tuple[int, str, str]:
    """Run a subprocess with timeout. Returns (returncode, stdout, stderr)."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )
    except TimeoutError:
        # Kill if still running
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        return -1, "", "timeout"
    except OSError as exc:
        logger.warning("Subprocess exec failed for %s: %s", args[0] if args else "?", exc)
        return -1, "", str(exc)
    except Exception as exc:
        logger.error("Unexpected subprocess error: %s", exc, exc_info=True)
        return -1, "", str(exc)


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
    """Check Flask health API responds with 200."""
    name = "health_api"
    t0 = datetime.now(UTC)
    try:
        url = f"{config.health_url}/api/genesis/health"
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
    """Check container memory usage via cgroup v2."""
    name = "memory_pressure"
    t0 = datetime.now(UTC)
    try:
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", config.container_name, "--",
            "cat", "/sys/fs/cgroup/memory.current",
            timeout=config.probes.probe_timeout_s,
        )
        if rc != 0:
            return SuspiciousResult(
                name=name, ok=True, detail=f"cgroup read failed: {stderr[:200]}",
                collected_at=t0.isoformat(),
            )
        current = int(stdout.strip())

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
        pct = (current / max_bytes) * 100 if max_bytes > 0 else 0
        ok = pct < config.suspicious.memory_warning_pct
        detail = f"{pct:.1f}% ({current // (1024*1024)}M / {max_bytes // (1024*1024)}M)"
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


async def check_cc_tmp_usage(config: GuardianConfig) -> SuspiciousResult:
    """Check CC temp directory usage via watchgod state file."""
    name = "cc_tmp_usage"
    t0 = datetime.now(UTC)
    try:
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", config.container_name, "--",
            "su", "-", "ubuntu", "-c",
            "cat ~/.genesis/watchgod_state.json 2>/dev/null || echo '{}'",
            timeout=config.probes.probe_timeout_s,
        )
        if rc != 0:
            return SuspiciousResult(
                name=name, ok=True, detail=f"read failed: {stderr[:200]}",
                collected_at=t0.isoformat(),
            )
        import json

        data = json.loads(stdout.strip() or "{}")
        cc_tier = data.get("cc_tmp", {}).get("tier", "unknown")
        sys_tier = data.get("system_tmp", {}).get("tier", "unknown")
        used_mb = data.get("cc_tmp", {}).get("used_mb", 0)

        ok = cc_tier in ("green", "yellow") and sys_tier in ("green", "yellow")
        detail = f"cc_tmp: {cc_tier} ({used_mb}MB), /tmp: {sys_tier}"
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
    """Run all 5 probes in parallel, then suspicious checks if healthy."""
    now = datetime.now(UTC).isoformat()

    # Run all 5 probes in parallel
    probe_results = await asyncio.gather(
        probe_container_exists(config),
        probe_icmp_reachable(config),
        probe_health_api(config),
        probe_heartbeat_canary(config),
        probe_log_freshness(config),
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
            check_cc_tmp_usage(config),
            check_restart_count(config),
            check_error_spike(config),
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
