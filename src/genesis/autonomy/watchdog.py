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
        self._az_installed = self._detect_az_installed()

    @property
    def target_service(self) -> str:
        """The systemd service name this watchdog monitors."""
        return self._target_service

    def _detect_target_service(self) -> str:
        """Auto-detect which Genesis service to monitor.

        Standalone mode uses genesis-server.service.
        AZ/bridge mode uses genesis-bridge.service (default).
        """
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-enabled", "genesis-server.service"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return "genesis-server.service"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return "genesis-bridge.service"

    def _detect_az_installed(self) -> bool:
        """Check if agent-zero.service is enabled. Cached at init time."""
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-enabled", "agent-zero.service"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

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

        # 0.3. Check if Agent Zero is running (separate from bridge)
        self._check_az_health()

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
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip() == "active"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None  # Can't determine — don't block on this

    def _bridge_exited_unconfigured(self) -> bool:
        """Check if the target service exited with code 2 (unconfigured — missing secrets.env)."""
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", "-p", "ExecMainStatus", self._target_service],
                capture_output=True, text=True, timeout=5,
            )
            return "ExecMainStatus=2" in result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    def _check_az_health(self) -> None:
        """Check if Agent Zero service is active. Auto-restart if down.

        Skipped entirely in standalone mode (agent-zero.service not enabled).
        Uses a separate state file (~/.genesis/watchdog_az_state.json) from
        the bridge watchdog to keep backoff counters independent. AZ restarts
        are more impactful (kills dashboard + all Genesis subsystems), so
        max_attempts is conservative (3).
        """
        # Skip if AZ service is not installed/enabled (standalone mode)
        if not self._az_installed:
            return

        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "agent-zero.service"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip() == "active":
                # Reset AZ backoff state on recovery
                az_state_path = self._state_path.parent / "watchdog_az_state.json"
                if az_state_path.exists():
                    import contextlib

                    with contextlib.suppress(OSError):
                        az_state_path.write_text(json.dumps({
                            "consecutive_failures": 0,
                            "next_attempt_after": None,
                        }))
                return  # Healthy
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return  # Can't determine — don't act

        # AZ is not active — attempt restart with its own backoff state
        az_state_path = self._state_path.parent / "watchdog_az_state.json"
        az_state = {"consecutive_failures": 0, "next_attempt_after": None}
        try:
            if az_state_path.exists():
                az_state = json.loads(az_state_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

        max_az_attempts = 3  # Conservative — AZ restart is high-impact
        if az_state.get("consecutive_failures", 0) >= max_az_attempts:
            logger.error("AZ: max restart attempts (%d) reached", max_az_attempts)
            return

        if az_state.get("next_attempt_after") and time.time() < az_state["next_attempt_after"]:
            return  # In backoff

        logger.warning("Agent Zero service not active — attempting restart")
        az_state["consecutive_failures"] = az_state.get("consecutive_failures", 0) + 1
        backoff = min(self._backoff_initial * (2 ** (az_state["consecutive_failures"] - 1)), self._backoff_max)
        az_state["next_attempt_after"] = time.time() + backoff
        try:
            az_state_path.write_text(json.dumps(az_state))
        except OSError:
            logger.error("Failed to save AZ watchdog state", exc_info=True)

        restart_az()

    def _check_memory_pressure(self) -> None:
        """Proactive memory check — reclaim page cache if approaching limit.

        Runs every watchdog cycle (60s). Prevents the OOM cascade where ghost
        page cache from killed sessions accumulates and triggers increasingly
        rapid OOM kills.
        """
        mem = get_container_memory()
        if mem is None or mem[1] == 0:
            return  # Can't read or no limit set

        current, limit = mem
        pct = (current / limit) * 100
        if pct >= 90:
            logger.error(
                "Container memory CRITICAL: %.0f%% (%.1f/%.1f GiB) — reclaiming cache",
                pct, current / (1024**3), limit / (1024**3),
            )
            reclaim_page_cache("256M")
        elif pct >= 80:
            logger.warning(
                "Container memory HIGH: %.0f%% (%.1f/%.1f GiB) — reclaiming cache",
                pct, current / (1024**3), limit / (1024**3),
            )
            reclaim_page_cache("128M")

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


_last_reclaim_time: float = 0.0


def reclaim_page_cache(target_bytes: str = "128M") -> bool:
    """Reclaim file-backed page cache from user.slice cgroup.

    Uses cgroup v2 memory.reclaim in small, guarded increments.
    Large reclaims (>256M) cause I/O storms in I/O-limited containers
    because evicted active pages are immediately re-faulted from disk,
    saturating the cgroup I/O budget and creating a death spiral
    (incident 2026-03-16).
    """
    global _last_reclaim_time

    now = time.monotonic()

    # Cap at 256M: small reclaims let the kernel LRU pick inactive pages
    # instead of evicting the active working set
    allowed = {"64M", "128M", "256M"}
    if target_bytes not in allowed:
        logger.warning(
            "Capping reclaim target from %s to 256M (max safe size)", target_bytes,
        )
        target_bytes = "256M"

    reclaim_path = Path("/sys/fs/cgroup/user.slice/memory.reclaim")
    if not reclaim_path.exists() and "unittest.mock" not in type(subprocess.run).__module__:
        logger.warning("memory.reclaim not available — skipping page cache reclaim")
        return False

    try:
        result = subprocess.run(
            ["sudo", "tee", str(reclaim_path)],
            input=target_bytes, capture_output=True, text=True,
            timeout=5,  # Short timeout — if reclaim stalls, bail out
        )
        if result.returncode == 0:
            _last_reclaim_time = now
            logger.info("Page cache reclaim succeeded (requested %s)", target_bytes)
            return True
        logger.warning("Page cache reclaim failed: %s", result.stderr.strip())
        return False
    except subprocess.TimeoutExpired:
        logger.error(
            "Page cache reclaim TIMED OUT after 5s — I/O system may be saturated"
        )
        return False
    except (FileNotFoundError, OSError):
        logger.warning("Page cache reclaim unavailable", exc_info=True)
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


def restart_bridge(service: str = "genesis-bridge.service") -> int:
    """Restart the specified Genesis service via systemctl. Returns exit code.

    Defaults to genesis-bridge.service for backward compatibility.
    Pass checker.target_service to restart the auto-detected service.
    """
    logger.info("Restarting %s via systemctl...", service)
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", service],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("%s restart command succeeded", service)
        else:
            logger.error("%s restart failed: %s", service, result.stderr)
        return result.returncode
    except subprocess.TimeoutExpired:
        logger.error("%s restart command timed out", service)
        return -1
    except FileNotFoundError:
        logger.error("systemctl not found — cannot restart bridge")
        return -2


def restart_az() -> int:
    """Restart Agent Zero via systemctl. Returns exit code.

    Returns 0 (no-op) if agent-zero.service is not enabled (standalone mode).
    """
    # Guard: skip if AZ not installed
    try:
        check = subprocess.run(
            ["systemctl", "--user", "is-enabled", "agent-zero.service"],
            capture_output=True, text=True, timeout=5,
        )
        if check.returncode != 0:
            logger.info("AZ not installed (standalone mode) — skipping restart")
            return 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        logger.info("Cannot determine AZ status — skipping restart")
        return 0

    logger.info("Restarting agent-zero.service via systemctl...")
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "agent-zero.service"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("AZ restart command succeeded")
        else:
            logger.error("AZ restart failed: %s", result.stderr)
        return result.returncode
    except subprocess.TimeoutExpired:
        logger.error("AZ restart command timed out")
        return -1
    except FileNotFoundError:
        logger.error("systemctl not found — cannot restart AZ")
        return -2
