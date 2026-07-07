"""Guardian configuration — BOTH SIDES. YAML loader with env var overrides."""

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
    db_latency_warning_ms: float = 5000.0  # Normal is <100ms; 5s = serious degradation
    io_pressure_threshold_pct: float = 10.0  # PSI full avg10 above this → warning


@dataclass
class ConfirmationConfig:
    """State machine confirmation protocol settings."""

    recheck_delay_s: int = 30
    max_recheck_attempts: int = 3
    required_failed_signals: int = 2
    bootstrap_grace_s: int = 300
    pause_reminder_hours: int = 24
    confirmed_dead_timeout_s: int = 600  # Auto-reset after 10min stuck
    max_auto_resets: int = 3  # Prevent infinite reset oscillation


@dataclass
class AlertConfig:
    """Alert channel configuration."""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_thread_id: str = ""


@dataclass
class CCConfig:
    """Claude Code diagnosis engine settings.

    The Guardian's CC invocation is the highest-stakes CC call in the system.
    When it fires, Genesis is down. Use the best model available (opus) with
    generous limits — these are runaway guards, not operational constraints.
    """

    enabled: bool = True
    model: str = "opus"
    # Thinking effort for diagnosis: low/medium/high/xhigh/max. Omitted at
    # dispatch for models that don't use an effort setting (Haiku).
    effort: str = "high"
    timeout_s: int = 3600  # 60 min — must not clip downloads or deep investigation
    max_turns: int = 50    # Runaway guard — legitimate work is ~15-30 turns
    path: str = "claude"
    work_dir: str = "/var/lib/guardian-snapshots/cc-sessions"


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

    # Non-healthy snapshots kept by take()'s delete-before-create (the latest
    # healthy snapshot is exempt there and rotated by mark_healthy instead).
    # 2 so the healthy lifeline and the newest pre-recovery snapshot coexist.
    retention: int = 2
    prefix: str = "guardian-"
    take_pre_recovery: bool = True  # Take snapshot before recovery action
    # Daily 'healthy' snapshot while the guardian state is HEALTHY — produces
    # the offline SNAPSHOT_ROLLBACK lifeline (without it, rollback has no
    # target and always fails). Rotated on each take: exactly one healthy
    # snapshot, ≤1 maintenance interval old, so CoW divergence never
    # accumulates. Set false to stop taking (existing healthy snapshots then
    # age out via max_age_days / expiry).
    healthy_enabled: bool = True
    max_pool_usage_pct: float = 80.0  # Fallback threshold if headroom check unavailable
    min_headroom_gb: float = 5.0  # Minimum free space floor for headroom check
    # Age-based prune: delete guardian-* snapshots older than this many days,
    # regardless of retention count — EXCEPT the newest and the latest healthy
    # (the offline snapshot-rollback lifeline). Backstops the incident where
    # stale guardian-pre-recovery snapshots accumulated CoW divergence for months.
    max_age_days: int = 14
    # incus `snapshots.expiry` — daemon-side auto-deletion of SCHEDULED snapshots
    # after this interval (units: s/m/h/d/w/M/y). A guardian-independent kill
    # switch that fires even if the guardian process is dead. Deliberately does
    # NOT set `snapshots.expiry.manual` (instance-wide; would expire snapshots
    # the user creates by hand). Empty string disables enforcement.
    expiry: str = "2w"


@dataclass
class RecoveryConfig:
    """Recovery engine settings."""

    verification_delay_s: int = 30
    max_escalations: int = 3
    max_io_triage_attempts: int = 5  # Separate budget for IO_TRIAGE (low-risk)


@dataclass
class StoragePoolConfig:
    """Host storage-pool monitoring thresholds (LVM-thin data + metadata).

    The incident that motivated this: the LVM thin pool filled to 100% with no
    warning. Metadata exhaustion is worse than data exhaustion (it forces an
    offline thin_check/repair), so metadata alerts at LOWER percentages.
    """

    enabled: bool = True
    # Data-allocation tiers (percent of thin-pool data space used).
    data_warn_pct: float = 75.0
    data_high_pct: float = 85.0
    data_crit_pct: float = 92.0
    # Metadata tiers — alert earlier; metadata-full is nastier to recover.
    metadata_warn_pct: float = 60.0
    metadata_high_pct: float = 70.0
    metadata_crit_pct: float = 80.0
    # Backend-agnostic pool-used% tiers (incus space.used/total) — the FALLBACK
    # signal for non-LVM backends (btrfs), where data%/metadata% don't exist.
    # Only consulted when both LVM percents are absent (see pool.worst_tier).
    pool_used_warn_pct: float = 75.0
    pool_used_high_pct: float = 85.0
    pool_used_crit_pct: float = 92.0
    # Re-alert cadence while a tier is sustained (avoids per-tick spam but keeps
    # a live problem visible). Tier *increases* always alert immediately.
    realert_hours: float = 6.0


@dataclass
class ProvisioningConfig:
    """Hypervisor provisioning (rung 5 of the escalation ladder).

    Lets the Guardian grow the VM's virtual disk / RAM from the hypervisor
    (Proxmox) so a thin-pool with zero VG free extents can finally be extended —
    the structural fix the 2026-07 outage proved was missing. Disabled by
    default; every mutation passes a fresh per-action Telegram APPROVE/DENY gate.
    Machine specifics (host/node/vmid/disk/storage) live here in config, never
    hardcoded; only the two API token strings cross the credential bridge.

    Safety invariants encoded as config, not code constants:
    - grows are bounded per action (max_*_step_*) AND per week (max_actions_per_week)
    - a fresh capacity+due-diligence re-check runs AFTER approval, before mutating
    - node_memory_margin_mib / storage_margin_gib are headroom the grow must leave
    """

    enabled: bool = False
    provider: str = "proxmox"  # only "proxmox" implemented; ABC allows others
    api_host: str = ""         # PVE host, e.g. 192.168.1.10 — empty = unconfigured
    api_port: int = 8006
    verify_tls: bool = True     # self-signed PVE → set false per-install (documented)
    node: str = ""             # PVE node name, e.g. "pve"
    vmid: int = 0              # this container's host VM id, e.g. 100; 0 = unconfigured
    target_disk: str = "scsi1"  # the disk backing the pool's PV (prefer whole-disk PV)
    storage: str = "local-lvm"  # PVE storage the disk lives on
    # Per-action grow caps (a single approval can never exceed these).
    max_disk_step_gib: int = 32
    max_memory_step_mib: int = 4096
    # Headroom the grow must leave on the hypervisor (refuse if it would dip below).
    storage_margin_gib: int = 64
    node_memory_margin_mib: int = 8192
    # Rate cap: executed mutations allowed per rolling 7-day window.
    max_actions_per_week: int = 2
    # A recent successful backup is a precondition for an irreversible grow.
    require_recent_backup: bool = True
    backup_max_age_days: int = 14
    # Bounded wait for the Telegram APPROVE/DENY reply (guardian is oneshot).
    approval_timeout_s: int = 1800
    # Damper: don't re-propose the same autonomous pool-grow more than this often.
    min_repropose_hours: int = 24
    # Autonomous path: on pool TIER_CRIT, PROPOSE a disk grow (never execute).
    propose_on_pool_crit: bool = True


@dataclass
class GuardianConfig:
    """Top-level Guardian configuration."""

    container_name: str = "genesis"
    container_ip: str = ""  # Auto-detected at runtime if empty
    container_user: str = "ubuntu"
    health_api_port: int = 5000
    check_interval_s: int = 30
    state_dir: str = "~/.local/state/genesis-guardian"
    maintenance_file: str = "/var/lib/guardian-snapshots/.guardian-maintenance"

    # Host VM details — used by container for bidirectional monitoring (SSH → gateway)
    host_ip: str = ""      # Auto-detected by installer; empty = not installed
    host_user: str = ""    # Host VM username

    probes: ProbeConfig = field(default_factory=ProbeConfig)
    suspicious: SuspiciousChecksConfig = field(default_factory=SuspiciousChecksConfig)
    confirmation: ConfirmationConfig = field(default_factory=ConfirmationConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    cc: CCConfig = field(default_factory=CCConfig)
    briefing: BriefingConfig = field(default_factory=BriefingConfig)
    snapshots: SnapshotConfig = field(default_factory=SnapshotConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    storage_pool: StoragePoolConfig = field(default_factory=StoragePoolConfig)
    provisioning: ProvisioningConfig = field(default_factory=ProvisioningConfig)

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
        ("GUARDIAN_CC_EFFORT", "effort", str),
        ("GUARDIAN_CC_TIMEOUT", "timeout_s", int),
        ("GUARDIAN_CC_MAX_TURNS", "max_turns", int),
        ("GUARDIAN_CC_PATH", "path", str),
    ]:
        val = os.environ.get(env_var)
        if val is not None:
            setattr(config.cc, attr, typ(val))

    # Provisioning kill-switch — env can force-disable a config-enabled adapter
    # (an operator escape hatch) or enable one for a live test.
    prov_enabled = os.environ.get("GUARDIAN_PROVISIONING_ENABLED")
    if prov_enabled is not None:
        config.provisioning.enabled = prov_enabled.lower() in ("1", "true", "yes")

    return config


def _build_sub(cls: type, raw: dict, key: str) -> object:
    """Build a sub-config dataclass from a YAML dict section."""
    section = raw.get(key, {})
    if not isinstance(section, dict):
        return cls()
    valid_fields = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in section.items() if k in valid_fields})


# Provisioning override lives in the guardian STATE dir (not the git checkout),
# so `configure-provisioning` can land host-specific provisioning config that
# survives `update.sh` guardian redeploys without editing (or skip-worktree-ing)
# the tracked guardian.yaml.
_PROVISIONING_OVERRIDE_FILE = "provisioning.local.yaml"


def _coerce_provisioning_value(default: object, value: object) -> object:
    """Coerce a raw override value to the ProvisioningConfig field's type.

    bool is checked before int (bool is an int subclass). Accepts real YAML
    types (already correct) or strings (from the ``key=value`` CLI path).
    """
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(value)
    return str(value)


def write_provisioning_override(state_dir: str, params: dict) -> Path:
    """Write/replace the provisioning override file; return its path.

    Only keys valid on ProvisioningConfig are accepted — an unknown key raises
    ValueError so a typo can't silently no-op. No secrets belong here (the two
    Proxmox tokens cross the credential bridge, never this file).
    """
    defaults = {f.name: f.default for f in dataclasses.fields(ProvisioningConfig)}
    coerced: dict = {}
    for k, v in params.items():
        if k not in defaults:
            raise ValueError(f"unknown provisioning field: {k!r}")
        coerced[k] = _coerce_provisioning_value(defaults[k], v)
    state_path = Path(state_dir).expanduser()
    state_path.mkdir(parents=True, exist_ok=True)
    dest = state_path / _PROVISIONING_OVERRIDE_FILE
    # Atomic write: a mid-write kill must never leave a truncated override
    # (it would be ignored and fall back to defaults, but a clean swap is free).
    tmp = dest.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump({"provisioning": coerced}, f, default_flow_style=False, sort_keys=True)
    os.replace(tmp, dest)
    return dest


def _apply_provisioning_override(config: GuardianConfig) -> None:
    """Merge the state-dir provisioning override onto the loaded config.

    An absent or unreadable file is a silent no-op — a broken override must
    never crash a guardian check cycle. Only fields valid on ProvisioningConfig
    are applied; env overrides (incl. the GUARDIAN_PROVISIONING_ENABLED kill
    switch) run AFTER this, so they always win.
    """
    override = config.state_path / _PROVISIONING_OVERRIDE_FILE
    try:
        if not override.exists():
            return
        with open(override) as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Ignoring unreadable provisioning override %s: %s", override, exc)
        return
    section = raw.get("provisioning") if isinstance(raw, dict) and "provisioning" in raw else raw
    if not isinstance(section, dict):
        return
    valid = {f.name for f in dataclasses.fields(ProvisioningConfig)}
    applied = False
    for k, v in section.items():
        if k in valid:
            setattr(config.provisioning, k, v)
            applied = True
    if applied:
        logger.info("Applied provisioning override from %s", override)


def _finalize(config: GuardianConfig) -> GuardianConfig:
    """Apply the provisioning override then env overrides (env wins)."""
    _apply_provisioning_override(config)
    return _env_override(config)


def load_config(path: Path | None = None) -> GuardianConfig:
    """Load Guardian config from YAML with env var overrides.

    Returns sensible defaults if the config file is missing.
    """
    config_path = path or Path(os.environ.get("GUARDIAN_CONFIG", str(_DEFAULT_CONFIG_PATH)))

    if not config_path.exists():
        logger.info("Guardian config not found at %s, using defaults", config_path)
        return _finalize(GuardianConfig())

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    # Top-level scalar fields
    top_fields = {
        "container_name", "container_ip", "container_user",
        "health_api_port", "check_interval_s", "state_dir",
        "host_ip", "host_user", "maintenance_file",
    }
    top_kwargs = {k: v for k, v in raw.items() if k in top_fields}

    config = GuardianConfig(
        **top_kwargs,
        probes=_build_sub(ProbeConfig, raw, "probes"),
        suspicious=_build_sub(SuspiciousChecksConfig, raw, "suspicious"),
        confirmation=_build_sub(ConfirmationConfig, raw, "confirmation"),
        alert=_build_sub(AlertConfig, raw, "alert"),
        cc=_build_sub(CCConfig, raw, "cc"),
        briefing=_build_sub(BriefingConfig, raw, "briefing"),
        snapshots=_build_sub(SnapshotConfig, raw, "snapshots"),
        recovery=_build_sub(RecoveryConfig, raw, "recovery"),
        storage_pool=_build_sub(StoragePoolConfig, raw, "storage_pool"),
        provisioning=_build_sub(ProvisioningConfig, raw, "provisioning"),
    )

    return _finalize(config)


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
