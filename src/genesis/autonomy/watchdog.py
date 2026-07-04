"""Watchdog — monitors Genesis infrastructure health via status.json staleness.

Design:
  - Reads ~/.genesis/status.json (written per awareness tick by StatusFileWriter)
  - If stale beyond threshold → bridge/runtime may be dead
  - Before restart: validates config (secrets.env, bridge code syntax)
  - NEVER modifies files, NEVER runs git, NEVER attempts code self-repair
  - On validation failure: refuse to restart, log loudly, exit non-zero

Mutual monitoring:
  - Watchdog monitors Genesis via status.json staleness
  - Genesis monitors watchdog via runtime.job_health (watchdog reports heartbeat)
  - systemd Restart=on-failure catches crashes of either
"""

from __future__ import annotations

import importlib.util
import json
import logging
import py_compile
import subprocess
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from genesis.autonomy.types import WatchdogAction
from genesis.env import secrets_path as default_secrets_path
from genesis.env import update_in_progress
from genesis.util.systemd import systemctl_env

if TYPE_CHECKING:
    from genesis.autonomy.remediation import RemediationRegistry

# outreach_fn signature: async (severity, title, body) -> None
OutreachFn = Callable[[str, str, str], Coroutine[Any, Any, None]]

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "config" / "autonomy.yaml"


class WatchdogChecker:
    """Stateless health checker — called by systemd timer or standalone script.

    Each invocation checks status.json, decides action, and returns it.
    State (backoff counters) lives in a small JSON sidecar file.
    """

    def __init__(
        self,
        *,
        status_file: str | Path = "~/.genesis/status.json",
        staleness_threshold_s: int = 300,
        max_restart_attempts: int = 5,
        backoff_initial_s: int = 10,
        backoff_max_s: int = 300,
        config_validation: bool = True,
        bridge_module: str = "genesis.channels.bridge",
        secrets_path: str | Path | None = None,
        state_file: str | Path = "~/.genesis/watchdog_state.json",
        stabilization_s: int = 120,
        remediation_registry: RemediationRegistry | None = None,
        outreach_fn: OutreachFn | None = None,
    ) -> None:
        self._status_path = Path(status_file).expanduser()
        self._staleness_threshold = staleness_threshold_s
        self._max_restarts = max_restart_attempts
        self._backoff_initial = backoff_initial_s
        self._backoff_max = backoff_max_s
        self._validate_config = config_validation
        self._bridge_module = bridge_module
        resolved_secrets = secrets_path or default_secrets_path()
        self._secrets_path = Path(resolved_secrets).expanduser()
        self._state_path = Path(state_file).expanduser()
        self._stabilization_s = stabilization_s
        self._remediation_registry = remediation_registry
        self._outreach_fn = outreach_fn
        self._target_service = self._detect_target_service()

    @property
    def target_service(self) -> str:
        """The systemd service name this watchdog monitors."""
        return self._target_service

    def _detect_target_service(self) -> str:
        """Auto-detect which Genesis service to monitor.

        Prefers genesis-server.service, falls back to genesis-bridge.service.
        """
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-enabled", "genesis-server.service"],
                capture_output=True, text=True, timeout=5, env=systemctl_env(),
            )
            if result.returncode == 0:
                return "genesis-server.service"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return "genesis-bridge.service"

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> WatchdogChecker:
        config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
        try:
            data = yaml.safe_load(config_path.read_text())
            wd = data.get("watchdog", {})
            return cls(
                status_file=wd.get("status_file", "~/.genesis/status.json"),
                staleness_threshold_s=wd.get("staleness_threshold_seconds", 300),
                max_restart_attempts=wd.get("max_restart_attempts", 5),
                backoff_initial_s=wd.get("backoff_initial_seconds", 10),
                backoff_max_s=wd.get("backoff_max_seconds", 300),
                config_validation=wd.get("config_validation", True),
                stabilization_s=wd.get("stabilization_seconds", 120),
            )
        except (yaml.YAMLError, OSError, AttributeError):
            logger.warning("Failed to load watchdog config — using defaults", exc_info=True)
            return cls()

    def check(self) -> WatchdogAction:
        """Run a single health check. Returns the recommended action."""
        # 0. Always record that we ran (enables watchdog staleness detection)
        self._record_check()

        # 0.25. Proactive memory pressure check — reclaim before OOM fires
        self._check_memory_pressure()

        # 0.3. I/O pressure check — detect thrashing before it freezes the container
        self._check_io_pressure()

        # 0.5. Quick check: is the bridge process even running?
        bridge_active = self._is_bridge_active()
        if bridge_active is False:
            if self._bridge_exited_unconfigured():
                logger.info("Bridge exited unconfigured (exit 2) — skipping restart")
                return WatchdogAction.SKIP
            logger.warning("Bridge service is not active — skipping staleness check, going to restart logic")
            # Fall through to backoff/validation/restart below
            state = self._load_state()
            return self._restart_if_allowed(state, reason="bridge_inactive")

        # 1. Read status.json
        status = self._read_status()
        if status is None:
            logger.error("Status file missing at %s — cannot determine health", self._status_path)
            return WatchdogAction.NOTIFY

        # 2. Check staleness
        staleness_s = self._compute_staleness(status)
        if staleness_s is None:
            logger.error("Status file has no valid timestamp")
            return WatchdogAction.NOTIFY

        if staleness_s <= self._staleness_threshold:
            logger.debug("Status file fresh (%.0fs old, threshold %ds)", staleness_s, self._staleness_threshold)
            # Check for zombie schedulers: process alive but scheduler dead.
            zombie = self._check_scheduler_heartbeats(status)
            if zombie:
                # Don't restart during heavy workload — staleness is expected
                # (dream cycle runs 15-35 min, during which surplus doesn't dispatch).
                heavy = status.get("heavy_workload")
                if heavy:
                    logger.info(
                        "Zombie detected (%s) but heavy workload active (%s) — skipping",
                        ", ".join(zombie), heavy,
                    )
                    return WatchdogAction.SKIP

                # Don't restart if server just started — timestamps are stale
                # from the previous process and will be refreshed shortly.
                uptime = status.get("uptime_s")
                if uptime is not None and uptime < self._stabilization_s:
                    logger.info(
                        "Zombie detected but server just started (%.0fs < %ds) — skipping",
                        uptime, self._stabilization_s,
                    )
                    return WatchdogAction.SKIP

                logger.warning(
                    "Zombie scheduler detected: %s — triggering restart",
                    ", ".join(zombie),
                )
                state = self._load_state()
                return self._restart_if_allowed(state, reason="zombie_scheduler")
            self._reset_state()
            return WatchdogAction.SKIP  # Healthy — no action needed

        logger.warning(
            "Status file stale: %.0fs old (threshold %ds) — bridge may be down",
            staleness_s, self._staleness_threshold,
        )

        # 3. Stale — attempt restart via shared logic
        state = self._load_state()
        return self._restart_if_allowed(state, reason="stale_status_restart")

    def _restart_if_allowed(self, state: dict, *, reason: str) -> WatchdogAction:
        """Shared restart logic: backoff → validation → restart or skip."""
        # Defer restarts while a deploy is running. update.sh intentionally stops
        # genesis-server for its merge/bootstrap/migrate window; a watchdog revival
        # there takes the DB write lock and deadlocks bootstrap's seed (incident
        # IR-2). Return SKIP (not NOTIFY/BACKOFF) BEFORE _record_failure so the
        # deploy window never trips backoff or the max-restart counter. Note: this
        # trusts every deploy phase to either self-bound or not deadlock (the seed,
        # the one proven hang, is separately timeout-bounded in bootstrap.sh).
        if update_in_progress():
            logger.info(
                "Deploy in progress — deferring %s restart until it completes", reason,
            )
            return WatchdogAction.SKIP

        if state["consecutive_failures"] >= self._max_restarts:
            logger.error(
                "Max restart attempts (%d) reached — refusing to restart. Manual intervention needed.",
                self._max_restarts,
            )
            return WatchdogAction.NOTIFY

        if state["next_attempt_after"] and time.time() < state["next_attempt_after"]:
            wait_remaining = state["next_attempt_after"] - time.time()
            logger.info("In backoff period — %.0fs remaining", wait_remaining)
            return WatchdogAction.BACKOFF

        if self._validate_config:
            issues = self.validate_config()
            if issues:
                for issue in issues:
                    logger.error("Config validation failed: %s", issue)
                self._record_failure(state, reason="config_validation_failed")
                return WatchdogAction.SKIP

        self._record_failure(state, reason=reason)
        return WatchdogAction.RESTART

    def validate_config(self) -> list[str]:
        """Validate critical config files. Returns list of issues (empty = OK).

        NEVER modifies files. Read-only checks only.
        """
        issues = []

        # Check secrets.env exists and has TELEGRAM_BOT_TOKEN
        if not self._secrets_path.exists():
            issues.append(f"Secrets file missing: {self._secrets_path}")
        else:
            try:
                content = self._secrets_path.read_text()
                if "TELEGRAM_BOT_TOKEN" not in content:
                    issues.append("TELEGRAM_BOT_TOKEN not found in secrets.env")
                if "placeholder" in content.lower():
                    token_lines = [
                        line for line in content.splitlines()
                        if "TELEGRAM_BOT_TOKEN" in line and "placeholder" in line.lower()
                    ]
                    if token_lines:
                        issues.append("TELEGRAM_BOT_TOKEN appears to be a placeholder")
            except OSError:
                issues.append(f"Cannot read secrets file: {self._secrets_path}")

        # Check bridge module compiles (no syntax errors)
        try:
            bridge_path = self._find_module_path(self._bridge_module)
            if bridge_path and bridge_path.exists():
                py_compile.compile(str(bridge_path), doraise=True)
            elif bridge_path:
                issues.append(f"Bridge module not found: {bridge_path}")
        except py_compile.PyCompileError as e:
            issues.append(f"Bridge module has syntax error: {e}")

        return issues

    def _is_bridge_active(self) -> bool | None:
        """Check if the target Genesis service is active. Returns None if unknown."""
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", self._target_service],
                capture_output=True, text=True, timeout=5, env=systemctl_env(),
            )
            return result.stdout.strip() == "active"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None  # Can't determine — don't block on this

    def _bridge_exited_unconfigured(self) -> bool:
        """Check if the target service exited with code 2 (unconfigured — missing secrets.env)."""
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", "-p", "ExecMainStatus", self._target_service],
                capture_output=True, text=True, timeout=5, env=systemctl_env(),
            )
            return "ExecMainStatus=2" in result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    def _check_memory_pressure(self) -> None:
        """Proactive memory check — reclaim page cache if approaching limit.

        Runs every watchdog cycle (300s). Uses anon+kernel memory (non-reclaimable)
        for threshold decisions. Total cgroup usage (memory.current) includes
        reclaimable page cache, which inflates the metric and causes false alarms.
        """
        mem = get_container_anon_memory()
        if mem is None or mem[1] == 0:
            return  # Can't read or no limit set

        anon_kernel, limit = mem
        pct = (anon_kernel / limit) * 100
        if pct >= 90:
            logger.error(
                "Container memory CRITICAL: %.0f%% anon+kernel (%.1f/%.1f GiB) — reclaiming cache",
                pct, anon_kernel / (1024**3), limit / (1024**3),
            )
            reclaim_page_cache("256M")
        elif pct >= 80:
            logger.warning(
                "Container memory HIGH: %.0f%% anon+kernel (%.1f/%.1f GiB) — reclaiming cache",
                pct, anon_kernel / (1024**3), limit / (1024**3),
            )
            reclaim_page_cache("128M")

    def _check_io_pressure(self) -> None:
        """Check container I/O pressure via PSI.

        Reads /sys/fs/cgroup/io.pressure (container-scoped). When full
        avg10 exceeds 25%, the container is experiencing significant I/O
        stalls — log a warning. This is the leading indicator for the
        page cache thrashing cascade that caused the 2026-05-25 incident.
        """
        psi_path = Path("/sys/fs/cgroup/io.pressure")
        try:
            content = psi_path.read_text()
        except OSError:
            return  # PSI not available — skip silently

        for line in content.splitlines():
            if not line.startswith("full"):
                continue
            for part in line.split():
                if part.startswith("avg10="):
                    try:
                        avg10 = float(part.split("=")[1])
                    except (ValueError, IndexError):
                        break
                    if avg10 > 50:
                        logger.error(
                            "I/O pressure CRITICAL: full avg10=%.1f%% — "
                            "container approaching freeze",
                            avg10,
                        )
                    elif avg10 > 25:
                        logger.warning(
                            "I/O pressure elevated: full avg10=%.1f%%",
                            avg10,
                        )
                    break

    def _record_check(self) -> None:
        """Record that the watchdog ran (even on SKIP). Enables staleness detection."""
        state = self._load_state()
        state["last_check_at"] = datetime.now(UTC).isoformat()
        self._save_state(state)

    def _read_status(self) -> dict | None:
        if not self._status_path.exists():
            return None
        try:
            return json.loads(self._status_path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.error("Failed to read status file at %s", self._status_path, exc_info=True)
            return None

    @staticmethod
    def _check_scheduler_heartbeats(
        status: dict,
        *,
        threshold_s: int = 900,  # 15 min — surplus dispatches every 5m, awareness ticks every 5m
    ) -> list[str]:
        """Check scheduler_heartbeats in status.json for zombie schedulers.

        Returns list of stale scheduler names (empty = healthy).
        """
        heartbeats = status.get("scheduler_heartbeats")
        if not heartbeats:
            return []  # No heartbeat data yet — don't alarm

        now = datetime.now(UTC)
        stale: list[str] = []
        for name, ts_str in heartbeats.items():
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                age_s = (now - ts).total_seconds()
                if age_s > threshold_s:
                    stale.append(f"{name} ({int(age_s)}s stale)")
            except (ValueError, TypeError):
                pass
        return stale

    @staticmethod
    def _compute_staleness(status: dict) -> float | None:
        ts = status.get("timestamp")
        if not ts:
            return None
        try:
            written = datetime.fromisoformat(ts)
            if written.tzinfo is None:
                written = written.replace(tzinfo=UTC)
            return (datetime.now(UTC) - written).total_seconds()
        except (ValueError, TypeError):
            return None

    def _load_state(self) -> dict:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"consecutive_failures": 0, "next_attempt_after": None, "last_reason": None, "last_restart_at": None, "last_check_at": None}

    def _save_state(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._state_path.write_text(json.dumps(state))
        except OSError:
            logger.error("Failed to save watchdog state to %s", self._state_path, exc_info=True)

    def _record_failure(self, state: dict, *, reason: str) -> None:
        state["consecutive_failures"] += 1
        state["last_reason"] = reason
        state["last_restart_at"] = time.time()
        # Exponential backoff: initial * 2^(failures-1), capped
        backoff = min(
            self._backoff_initial * (2 ** (state["consecutive_failures"] - 1)),
            self._backoff_max,
        )
        state["next_attempt_after"] = time.time() + backoff
        self._save_state(state)

    def _reset_state(self) -> None:
        if not self._state_path.exists():
            return

        # Stabilization cooldown: don't reset failure counter if a restart
        # was attempted recently.  This prevents the counter from bouncing
        # back to 0 when a service briefly appears healthy right after
        # restart but crashes again within the cooldown window.
        try:
            state = json.loads(self._state_path.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}

        last_restart_at = state.get("last_restart_at")
        if last_restart_at is not None:
            elapsed = time.time() - last_restart_at
            if elapsed < self._stabilization_s:
                logger.debug(
                    "Skipping failure counter reset — last restart %.0fs ago (cooldown %ds)",
                    elapsed,
                    self._stabilization_s,
                )
                # Still update last_check_at
                state["last_check_at"] = datetime.now(UTC).isoformat()
                try:
                    self._state_path.write_text(json.dumps(state))
                except OSError:
                    logger.error("Failed to save watchdog state", exc_info=True)
                return

        try:
            self._state_path.write_text(
                json.dumps({
                    "consecutive_failures": 0,
                    "next_attempt_after": None,
                    "last_reason": None,
                    "last_restart_at": None,
                    "last_check_at": datetime.now(UTC).isoformat(),
                })
            )
        except OSError:
            logger.error("Failed to save watchdog state", exc_info=True)

    @staticmethod
    def _find_module_path(module_name: str) -> Path | None:
        """Convert dotted module name to file path."""
        spec = importlib.util.find_spec(module_name)
        if spec and spec.origin and spec.origin not in {"built-in", "frozen"}:
            return Path(spec.origin)

        parts = module_name.split(".")
        # Try src/ layout first
        candidate = Path("src") / Path(*parts)
        for suffix in (".py", "/__init__.py"):
            p = candidate.parent / (candidate.name + suffix) if suffix == ".py" else candidate / "__init__.py"
            if p.exists():
                return p
        # Direct layout
        candidate2 = Path(*parts)
        for suffix in (".py", "/__init__.py"):
            p = candidate2.parent / (candidate2.name + suffix) if suffix == ".py" else candidate2 / "__init__.py"
            if p.exists():
                return p
        return Path("src") / Path(*parts).with_suffix(".py")


# Reclaim-cooldown state is persisted to this sidecar rather than an in-process
# global: the watchdog runs as a systemd *oneshot* (a fresh process every timer
# fire), so a module global would reset to 0.0 each run and the cooldown would
# never actually gate — letting reclaims fire back-to-back during memory pressure,
# the exact I/O storm the cooldown exists to prevent (incident 2026-03-16).
_RECLAIM_STATE_PATH = Path("~/.genesis/watchdog_reclaim.json").expanduser()


def _load_last_reclaim() -> float:
    """Epoch seconds of the last successful page-cache reclaim, persisted across
    the watchdog's oneshot runs. Returns 0.0 when absent/unreadable (fail-open:
    "never reclaimed" ⇒ a reclaim is allowed)."""
    try:
        return float(json.loads(_RECLAIM_STATE_PATH.read_text())["last_reclaim_at"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 0.0


def _save_last_reclaim(ts: float) -> None:
    """Persist the last-reclaim time so the cooldown survives the next oneshot run."""
    try:
        _RECLAIM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _RECLAIM_STATE_PATH.write_text(json.dumps({"last_reclaim_at": ts}))
    except OSError:
        logger.warning("Failed to persist reclaim cooldown to %s", _RECLAIM_STATE_PATH)


def reclaim_page_cache(target_bytes: str = "128M") -> bool:
    """Reclaim file-backed page cache via cgroup v2 memory.reclaim.

    Uses the user-owned cgroup path (no sudo needed). Small, guarded
    increments only — large reclaims (>256M) cause I/O storms in
    I/O-limited containers because evicted active pages are immediately
    re-faulted from disk, saturating the cgroup I/O budget and creating
    a death spiral (incident 2026-03-16).

    The 5-min cooldown is persisted (``_RECLAIM_STATE_PATH``) so it holds
    across the watchdog's oneshot process boundary, not just within one run.
    """
    # Wall-clock time.time() (not monotonic): the value is persisted and compared
    # across the oneshot's separate processes, where a monotonic reference resets.
    now = time.time()
    last_reclaim = _load_last_reclaim()

    # Cooldown: don't reclaim more than once per 5 minutes. Repeated
    # reclaims cause I/O storms in I/O-limited containers (incident 2026-03-16).
    _RECLAIM_COOLDOWN_S = 300.0
    if now - last_reclaim < _RECLAIM_COOLDOWN_S:
        remaining = _RECLAIM_COOLDOWN_S - (now - last_reclaim)
        logger.debug("Skipping reclaim — cooldown %.0fs remaining", remaining)
        return False

    # Cap at 256M: small reclaims let the kernel LRU pick inactive pages
    # instead of evicting the active working set
    allowed = {"64M", "128M", "256M"}
    if target_bytes not in allowed:
        logger.warning(
            "Capping reclaim target from %s to 256M (max safe size)", target_bytes,
        )
        target_bytes = "256M"

    # Use the user-owned cgroup path — writable without sudo.
    # The user.slice/memory.reclaim path requires root (--w------- root),
    # but the user@1000.service path is owned by ubuntu (incident 2026-04-08).
    reclaim_path = Path(
        "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/memory.reclaim"
    )
    if not reclaim_path.exists():
        logger.warning("memory.reclaim not available — skipping page cache reclaim")
        return False

    try:
        reclaim_path.write_text(target_bytes)
        _save_last_reclaim(now)
        logger.info("Page cache reclaim succeeded (requested %s)", target_bytes)
        return True
    except OSError as exc:
        logger.warning("Page cache reclaim failed: %s", exc)
        return False


def get_container_memory() -> tuple[int, int] | None:
    """Read container cgroup memory usage and limit.

    Returns (current_bytes, max_bytes) or None if unavailable.
    """
    try:
        current = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
        max_raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        max_bytes = int(max_raw) if max_raw != "max" else 0
        return current, max_bytes
    except (OSError, ValueError):
        return None


def get_container_anon_memory() -> tuple[int, int] | None:
    """Read non-reclaimable container memory (anon + kernel) and limit.

    Unlike ``get_container_memory()`` which reads ``memory.current`` (includes
    reclaimable page cache), this reads ``memory.stat`` for anon + kernel
    bytes — the memory that actually matters for OOM risk.

    Returns (anon_plus_kernel_bytes, max_bytes) or None if unavailable.
    """
    try:
        stats: dict[str, int] = {}
        with open("/sys/fs/cgroup/memory.stat") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] in ("anon", "kernel"):
                    stats[parts[0]] = int(parts[1])
        if not stats:
            return None
        anon_kernel = stats.get("anon", 0) + stats.get("kernel", 0)
        max_raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        max_bytes = int(max_raw) if max_raw != "max" else 0
        return anon_kernel, max_bytes
    except (OSError, ValueError):
        return None


def _wait_until_active(
    service: str, *, timeout_s: int = 60, poll_interval_s: float = 2.0,
) -> bool:
    """Poll ``systemctl --user is-active`` until *service* is active or timeout.

    Mirrors WatchdogChecker._is_bridge_active's probe. Used to confirm the
    true post-restart state instead of trusting the restart client's exit.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", service],
                capture_output=True, text=True, timeout=5, env=systemctl_env(),
            )
            if result.stdout.strip() == "active":
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        time.sleep(poll_interval_s)
    return False


def restart_bridge(service: str = "genesis-bridge.service", *, timeout_s: int = 120) -> int:
    """Restart the specified Genesis service via systemctl. Returns exit code.

    Returns 0 if the service is confirmed active after the restart, 1 if not,
    -2 if systemctl is missing.

    Verifies the *actual* service state rather than trusting the systemctl
    client's exit. A graceful genesis-server restart's stop phase is bounded
    by systemd's ~90s TimeoutStopSec; under load it can exceed a short client
    timeout, but killing the client does NOT abort the systemd restart job —
    systemd completes it independently. So on a slow/timed-out client we poll
    is-active before declaring failure, instead of returning a spurious error
    (which previously left the watchdog unit `failed` after a successful
    restart). ``timeout_s`` (default 120s) clears systemd's stop bound with
    margin while still bounding a genuinely hung systemctl — important because
    the watchdog timer cannot overlap oneshot runs.

    Defaults to genesis-bridge.service for backward compatibility.
    Pass checker.target_service to restart the auto-detected service.
    """
    logger.info("Restarting %s via systemctl...", service)
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", service],
            capture_output=True, text=True, timeout=timeout_s, env=systemctl_env(),
        )
        if result.returncode == 0:
            logger.info("%s restart command succeeded", service)
            return 0
        logger.error("%s restart returned %d: %s — verifying actual state",
                     service, result.returncode, result.stderr)
    except subprocess.TimeoutExpired:
        # Client exceeded the budget; systemd finishes the restart job
        # independently. Verify rather than assume failure.
        logger.warning(
            "%s restart client exceeded %ds — verifying actual service state "
            "(systemd completes the restart independently of the client)",
            service, timeout_s,
        )
    except FileNotFoundError:
        logger.error("systemctl not found — cannot restart %s", service)
        return -2

    if _wait_until_active(service):
        logger.info("%s confirmed active after restart", service)
        return 0
    logger.error("%s not active after restart attempt", service)
    return 1


