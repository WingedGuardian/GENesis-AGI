"""Guardian configuration — YAML loader with env var overrides."""

from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "config" / "guardian.yaml"
)


@dataclass
class ProbeConfig:
    """Timeouts and thresholds for individual health probes."""

    probe_timeout_s: int = 10
    ping_count: int = 1
    ping_timeout_s: int = 3
    http_timeout_s: int = 10
    journal_lines: int = 1


@dataclass
class SuspiciousChecksConfig:
    """Thresholds for 'healthy but suspicious' checks."""

    memory_warning_pct: float = 85.0
    tmp_warning_pct: float = 70.0
    tick_max_gap_s: int = 600
    tick_min_interval_s: int = 120
    tick_history_count: int = 10
    error_spike_window_min: int = 30
    error_spike_threshold: int = 50


@dataclass
class ConfirmationConfig:
    """State machine confirmation protocol settings."""

    recheck_delay_s: int = 30
    max_recheck_attempts: int = 3
    required_failed_signals: int = 2
    bootstrap_grace_s: int = 300
    pause_reminder_hours: int = 24


@dataclass
class AlertConfig:
    """Alert channel configuration."""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_thread_id: str = ""


@dataclass
class ApprovalConfig:
    """Approval HTTP handler settings."""

    port: int = 8888
    token_expiry_s: int = 86400
    bind_host: str = ""  # empty = auto-detect Tailscale IP


@dataclass
class CCConfig:
    """Claude Code diagnosis engine settings.

    The Guardian's CC invocation is the highest-stakes CC call in the system.
    When it fires, Genesis is down. Use the best model available (opus) with
    generous limits — these are runaway guards, not operational constraints.
    """

    enabled: bool = True
    model: str = "opus"
    timeout_s: int = 3600  # 60 min — must not clip downloads or deep investigation
    max_turns: int = 50    # Runaway guard — legitimate work is ~15-30 turns
    path: str = "claude"


@dataclass
class BriefingConfig:
    """Shared filesystem briefing settings.

    Genesis writes a curated briefing to the shared mount. Guardian reads
    it before CC diagnosis to give the investigator situational awareness.
    """

    enabled: bool = True
    # Relative to state_dir: {state_dir}/shared/briefing/guardian_briefing.md
    shared_subdir: str = "shared"
    briefing_filename: str = "guardian_briefing.md"
    max_age_s: int = 600  # 10 min — stale after this


@dataclass
class SnapshotConfig:
    """Incus snapshot management settings."""

    retention: int = 5
    prefix: str = "guardian-"


@dataclass
class RecoveryConfig:
    """Recovery engine settings."""

    verification_delay_s: int = 30
    max_escalations: int = 3


@dataclass
class GuardianConfig:
    """Top-level Guardian configuration."""

    container_name: str = "genesis"
    container_ip: str = ""  # Auto-detected at runtime if empty
    container_user: str = "ubuntu"
    health_api_port: int = 5000
    check_interval_s: int = 30
    state_dir: str = "~/.local/state/genesis-guardian"

    # Host VM details — used by container for bidirectional monitoring (SSH → gateway)
    host_ip: str = ""      # Auto-detected by installer; empty = not installed
    host_user: str = ""    # Host VM username

    probes: ProbeConfig = field(default_factory=ProbeConfig)
    suspicious: SuspiciousChecksConfig = field(default_factory=SuspiciousChecksConfig)
    confirmation: ConfirmationConfig = field(default_factory=ConfirmationConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    cc: CCConfig = field(default_factory=CCConfig)
    briefing: BriefingConfig = field(default_factory=BriefingConfig)
    snapshots: SnapshotConfig = field(default_factory=SnapshotConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)

    @property
    def health_url(self) -> str:
        ip = self.container_ip or self._detect_container_ip()
        return f"http://{ip}:{self.health_api_port}"

    def _detect_container_ip(self) -> str:
        """Auto-detect container IP via incus list. Cache result.

        Prefers eth0 over tailscale interfaces — Tailscale IPs may not
        be routable from the host depending on network configuration.
        """
        import subprocess
        try:
            result = subprocess.run(
                ["incus", "list", self.container_name, "-f", "csv", "-c", "4"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                import re
                ip = None
                # Prefer eth0, then any non-tailscale interface, then first IP
                eth0_match = re.search(
                    r"(\d+\.\d+\.\d+\.\d+)\s*\(eth0\)", result.stdout,
                )
                if eth0_match:
                    ip = eth0_match.group(1)
                else:
                    for m in re.finditer(
                        r"(\d+\.\d+\.\d+\.\d+)\s*\((\w+)\)", result.stdout,
                    ):
                        if "tailscale" not in m.group(2):
                            ip = m.group(1)
                            break
                    else:
                        # Last resort: first IP found
                        fallback = re.search(
                            r"(\d+\.\d+\.\d+\.\d+)", result.stdout,
                        )
                        if fallback:
                            ip = fallback.group(1)
                if ip:
                    self.container_ip = ip  # Cache for future calls
                    return ip
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        logger.warning("Cannot auto-detect container IP for '%s'", self.container_name)
        return "127.0.0.1"  # Safe fallback — will fail health probes, not crash

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser()

    @property
    def briefing_path(self) -> Path:
        """Full path to the Guardian briefing file on the host."""
        return (
            self.state_path
            / self.briefing.shared_subdir
            / "briefing"
            / self.briefing.briefing_filename
        )

    @property
    def findings_path(self) -> Path:
        """Directory for Guardian diagnosis result files on the host.

        Maps to ~/.genesis/shared/findings/ inside the container via
        the Incus shared mount.
        """
        return self.state_path / self.briefing.shared_subdir / "findings"


def _env_override(config: GuardianConfig) -> GuardianConfig:
    """Apply environment variable overrides to config values."""
    env_map = {
        "GUARDIAN_CONTAINER_NAME": ("container_name", str),
        "GUARDIAN_CONTAINER_IP": ("container_ip", str),
        "GUARDIAN_CONTAINER_USER": ("container_user", str),
        "GUARDIAN_HEALTH_PORT": ("health_api_port", int),
        "GUARDIAN_CHECK_INTERVAL": ("check_interval_s", int),
        "GUARDIAN_STATE_DIR": ("state_dir", str),
        "GUARDIAN_TELEGRAM_BOT_TOKEN": None,  # handled separately
        "GUARDIAN_TELEGRAM_CHAT_ID": None,
        "GUARDIAN_TELEGRAM_THREAD_ID": None,
        "GUARDIAN_CC_ENABLED": None,
        "GUARDIAN_CC_MODEL": None,
        "GUARDIAN_CC_PATH": None,
    }

    for env_var, mapping in env_map.items():
        val = os.environ.get(env_var)
        if val is None:
            continue
        if mapping is not None:
            attr, typ = mapping
            setattr(config, attr, typ(val))

    # Alert config overrides
    for env_var, attr in [
        ("GUARDIAN_TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
        ("GUARDIAN_TELEGRAM_CHAT_ID", "telegram_chat_id"),
        ("GUARDIAN_TELEGRAM_THREAD_ID", "telegram_thread_id"),
    ]:
        val = os.environ.get(env_var)
        if val is not None:
            setattr(config.alert, attr, val)

    # CC config overrides
    for env_var, attr, typ in [
        ("GUARDIAN_CC_ENABLED", "enabled", lambda v: v.lower() in ("1", "true", "yes")),
        ("GUARDIAN_CC_MODEL", "model", str),
        ("GUARDIAN_CC_TIMEOUT", "timeout_s", int),
        ("GUARDIAN_CC_MAX_TURNS", "max_turns", int),
        ("GUARDIAN_CC_PATH", "path", str),
    ]:
        val = os.environ.get(env_var)
        if val is not None:
            setattr(config.cc, attr, typ(val))

    return config


def _build_sub(cls: type, raw: dict, key: str) -> object:
    """Build a sub-config dataclass from a YAML dict section."""
    section = raw.get(key, {})
    if not isinstance(section, dict):
        return cls()
    valid_fields = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in section.items() if k in valid_fields})


def load_config(path: Path | None = None) -> GuardianConfig:
    """Load Guardian config from YAML with env var overrides.

    Returns sensible defaults if the config file is missing.
    """
    config_path = path or Path(os.environ.get("GUARDIAN_CONFIG", str(_DEFAULT_CONFIG_PATH)))

    if not config_path.exists():
        logger.info("Guardian config not found at %s, using defaults", config_path)
        return _env_override(GuardianConfig())

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    # Top-level scalar fields
    top_fields = {
        "container_name", "container_ip", "container_user",
        "health_api_port", "check_interval_s", "state_dir",
        "host_ip", "host_user",
    }
    top_kwargs = {k: v for k, v in raw.items() if k in top_fields}

    config = GuardianConfig(
        **top_kwargs,
        probes=_build_sub(ProbeConfig, raw, "probes"),
        suspicious=_build_sub(SuspiciousChecksConfig, raw, "suspicious"),
        confirmation=_build_sub(ConfirmationConfig, raw, "confirmation"),
        alert=_build_sub(AlertConfig, raw, "alert"),
        approval=_build_sub(ApprovalConfig, raw, "approval"),
        cc=_build_sub(CCConfig, raw, "cc"),
        briefing=_build_sub(BriefingConfig, raw, "briefing"),
        snapshots=_build_sub(SnapshotConfig, raw, "snapshots"),
        recovery=_build_sub(RecoveryConfig, raw, "recovery"),
    )

    return _env_override(config)


def load_secrets(path: Path | None = None) -> dict[str, str]:
    """Load secrets from a dotenv-style file (key=value lines).

    Used for Telegram bot token and chat ID on the host VM.
    """
    secrets_path = path or Path(
        os.environ.get("GUARDIAN_SECRETS", "~/.local/share/genesis-guardian/secrets.env")
    ).expanduser()

    if not secrets_path.exists():
        logger.warning("Guardian secrets file not found at %s", secrets_path)
        return {}

    secrets: dict[str, str] = {}
    for line in secrets_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            # Handle 'export KEY=value' syntax
            if key.startswith("export "):
                key = key[7:].strip()
            # Strip optional quotes
            value = value.strip().strip("'\"")
            secrets[key] = value

    return secrets
