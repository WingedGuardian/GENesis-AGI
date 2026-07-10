"""settings tools — read and modify Genesis configuration via conversation.

Exposes 3 generic tools: settings_list, settings_get, settings_update.
Each config domain has its own validator. Writable domains use atomic
YAML writes (tempfile + rename). Read-only domains are enforced by the
registry, not by filesystem permissions.
"""

from __future__ import annotations

import copy
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from genesis.cc.types import VALID_EFFORT_NAMES, VALID_MODEL_NAMES
from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"


# ── Domain registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingsDomain:
    """Metadata for a configurable settings domain."""

    name: str
    description: str
    config_filename: str
    readonly: bool
    needs_restart: bool
    dedicated_tool: str | None = None
    readonly_reason: str = ""
    hidden_fields: frozenset[str] = frozenset()  # Fields excluded from UI


_DOMAIN_REGISTRY: dict[str, SettingsDomain] = {
    "tts": SettingsDomain(
        name="tts",
        description="Text-to-speech voice settings (provider, voice, synthesis params)",
        config_filename="tts.yaml",
        readonly=False,
        needs_restart=False,
    ),
    "cc_roster": SettingsDomain(
        name="cc_roster",
        description=(
            "CC model roster — active model + roster members for running "
            "non-Anthropic models behind Claude Code. `default` selects the "
            "active model (Claude unless overridden)."
        ),
        config_filename="cc_roster.yaml",
        readonly=False,
        needs_restart=False,  # read live per-invocation by genesis.cc.roster
    ),
    "ws3_immunity": SettingsDomain(
        name="ws3_immunity",
        description=(
            "WS-3 immunity kill switch — master `enabled` + per-gate "
            "off/shadow/enforce for procedure/identity/autonomy/injection. "
            "Read live per-call by genesis.security.immunity (no restart); "
            "owner/first-party content is never blocked in any mode."
        ),
        config_filename="ws3_immunity.yaml",
        readonly=False,
        needs_restart=False,  # read live per-call by genesis.security.immunity
        hidden_fields=frozenset({"auto_demote_state"}),
    ),
    "resilience": SettingsDomain(
        name="resilience",
        description="Resilience thresholds (flapping detection, recovery, CC rate limits)",
        config_filename="resilience.yaml",
        readonly=False,
        needs_restart=True,
    ),
    "inbox_monitor": SettingsDomain(
        name="inbox_monitor",
        description="Inbox monitor (watch path, batch size, model, effort)",
        config_filename="inbox_monitor.yaml",
        readonly=False,
        needs_restart=True,
        hidden_fields=frozenset({"timezone"}),
    ),
    "autonomy": SettingsDomain(
        name="autonomy",
        description="Autonomy levels, ceilings, approval policy, watchdog",
        config_filename="autonomy.yaml",
        readonly=True,
        needs_restart=True,
        readonly_reason="Controls autonomous action limits and approval requirements. Ask Genesis to review and adjust.",
    ),
    "guardian": SettingsDomain(
        name="guardian",
        description="Host VM guardian health monitoring thresholds",
        config_filename="guardian.yaml",
        readonly=True,
        needs_restart=True,
        readonly_reason="Configured on the host VM during Guardian installation. Not editable from the container.",
    ),
    "autonomy_rules": SettingsDomain(
        name="autonomy_rules",
        description="Data-driven autonomy decision rules evaluated by RuleEngine",
        config_filename="autonomy_rules.yaml",
        readonly=True,
        needs_restart=False,
        readonly_reason="Decision rules that gate autonomous actions. Ask Genesis to review changes.",
    ),
    "content_sanitization": SettingsDomain(
        name="content_sanitization",
        description="Content sanitization and injection detection patterns",
        config_filename="content_sanitization.yaml",
        readonly=True,
        needs_restart=True,
        readonly_reason="Security filters for prompt injection detection. Changes require careful review — ask Genesis.",
    ),
    "model_profiles": SettingsDomain(
        name="model_profiles",
        description="Model intelligence tiers, costs, and capabilities",
        config_filename="model_profiles.yaml",
        readonly=True,
        needs_restart=False,
        readonly_reason="System reference data — model capabilities, costs, and intelligence tiers.",
    ),
    "model_routing": SettingsDomain(
        name="model_routing",
        description="Model routing call sites, provider chains, retry profiles",
        config_filename="model_routing.yaml",
        readonly=True,
        needs_restart=False,
        readonly_reason="Managed in the Routing panel on the Internals tab.",
    ),
    "outreach": SettingsDomain(
        name="outreach",
        description="Outreach preferences (quiet hours, rate limits, channels, voice alert IDs)",
        config_filename="outreach.yaml",
        readonly=False,
        needs_restart=False,
    ),
    "recon_schedules": SettingsDomain(
        name="recon_schedules",
        description="Recon gathering cron schedules",
        config_filename="recon_schedules.yaml",
        readonly=False,
        needs_restart=False,
        dedicated_tool="recon_config",
    ),
    "recon_watchlist": SettingsDomain(
        name="recon_watchlist",
        description="Recon project watchlist",
        config_filename="recon_watchlist.yaml",
        readonly=True,
        needs_restart=False,
        dedicated_tool="recon_config",
        readonly_reason="Editable via the recon_config tool — ask Genesis to add or remove items.",
    ),
    "recon_sources": SettingsDomain(
        name="recon_sources",
        description="Recon dynamic watch sources",
        config_filename="recon_sources.yaml",
        readonly=False,
        needs_restart=False,
        dedicated_tool="recon_config",
    ),
    "confidence_gates": SettingsDomain(
        name="confidence_gates",
        description="Confidence gating thresholds for observations, memory, and reflection",
        config_filename="confidence_gates.yaml",
        readonly=False,
        needs_restart=False,
    ),
    "autonomous_cli_policy": SettingsDomain(
        name="autonomous_cli_policy",
        description="Autonomous Claude Code fallback policy (global fallback, approval, channel, shared export)",
        config_filename="autonomous_cli_policy.yaml",
        readonly=False,
        needs_restart=False,
    ),
    "updates": SettingsDomain(
        name="updates",
        description="Update checking, notification, and auto-apply settings",
        config_filename="updates.yaml",
        readonly=False,
        needs_restart=False,
    ),
    "surplus": SettingsDomain(
        name="surplus",
        description="Surplus compute scheduler (dispatch intervals, job frequencies, task defaults)",
        config_filename="surplus.yaml",
        readonly=False,
        needs_restart=True,
    ),
    "ego": SettingsDomain(
        name="ego",
        description="Ego cycle settings (model, cadence, budget, effort)",
        config_filename="ego.yaml",
        readonly=False,
        needs_restart=True,
        hidden_fields=frozenset({"morning_report_timezone"}),
    ),
    "channels": SettingsDomain(
        name="channels",
        description="Channel defaults (model and effort for new Telegram sessions)",
        config_filename="channels.yaml",
        readonly=False,
        needs_restart=True,
    ),
    "contribution": SettingsDomain(
        name="contribution",
        description="Contribution offer pipeline (proactive upstream fix offers)",
        config_filename="contribution.yaml",
        readonly=False,
        needs_restart=False,
    ),
    "observability": SettingsDomain(
        name="observability",
        description="Tracing/spans capture switch + retention (needs restart to toggle capture)",
        config_filename="observability.yaml",
        readonly=False,
        needs_restart=True,
    ),
}


# ── YAML utilities ─────────────────────────────────────────────────────


def _load_yaml(filename: str) -> dict:
    """Read a base YAML file from the config dir. Returns empty dict if missing.

    This reads ONLY the base (git-tracked) config file. For the merged
    view (base + local overrides), use ``_load_yaml_merged()``.
    """
    path = _CONFIG_DIR / filename
    if not path.is_file():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _local_filename(filename: str) -> str:
    """Derive the .local.yaml filename from a base config filename."""
    stem = Path(filename).stem
    return f"{stem}.local.yaml"


def _load_yaml_local(filename: str) -> dict:
    """Read the .local.yaml overlay for a config file. Returns {} if none.

    Checks user config dir first (~/.genesis/config/), falls back to
    repo config/ for backwards compatibility with existing installs.
    """
    local_name = _local_filename(filename)
    user_path = _USER_CONFIG_DIR / local_name
    path = user_path if user_path.is_file() else _CONFIG_DIR / local_name
    if not path.is_file():
        return {}
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        logger.warning("Failed to read local overlay %s", path, exc_info=True)
        return {}


def _load_yaml_merged(filename: str) -> dict:
    """Read base config + local overlay, deep-merged.

    The local overlay (``{stem}.local.yaml``) contains user customizations
    that survive git updates. The base file is upstream-tracked defaults.
    """
    base = _load_yaml(filename)
    local = _load_yaml_local(filename)
    if not local:
        return base
    return _deep_merge(base, local)


_USER_CONFIG_DIR = Path.home() / ".genesis" / "config"


def _atomic_yaml_write(filename: str, data: dict) -> Path:
    """Write YAML atomically to user config dir (~/.genesis/config/).

    Runtime config writes go to the user directory, not the repo tree,
    so git status stays clean and user settings don't leak into PRs.
    """
    _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = _USER_CONFIG_DIR / filename
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".yaml.tmp",
    )
    try:
        with open(tmp_fd, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        Path(tmp_path).replace(path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    return path


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base. Lists are replaced, not appended."""
    merged = copy.deepcopy(base)
    for key, val in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


# ── Domain validators ──────────────────────────────────────────────────


def _validate_tts(changes: dict) -> list[str]:
    """Validate TTS config changes."""
    errors: list[str] = []
    valid_providers = {"elevenlabs", "fish_audio", "cartesia"}
    valid_top_keys = {
        "provider", "elevenlabs", "fish_audio", "cartesia",
        "sanitization", "voice_gate",
    }

    for key in changes:
        if key not in valid_top_keys:
            errors.append(f"Unknown key '{key}'. Valid: {', '.join(sorted(valid_top_keys))}")

    if "provider" in changes and changes["provider"] not in valid_providers:
        errors.append(
            f"provider must be one of {sorted(valid_providers)}, got '{changes['provider']}'"
        )

    el = changes.get("elevenlabs", {})
    if isinstance(el, dict):
        _validate_float_range(el, "stability", 0.0, 1.0, errors)
        _validate_float_range(el, "similarity_boost", 0.0, 1.0, errors)
        _validate_float_range(el, "style", 0.0, 1.0, errors)
        _validate_float_range(el, "speed", 0.7, 1.2, errors)

    san = changes.get("sanitization", {})
    if isinstance(san, dict) and "max_chars" in san:
        _validate_positive_int(san, "max_chars", errors)

    return errors


def _validate_resilience(changes: dict) -> list[str]:
    """Validate resilience config changes."""
    errors: list[str] = []
    valid_top_keys = {"flapping", "recovery", "cc", "status", "notifications"}

    for key in changes:
        if key not in valid_top_keys:
            errors.append(f"Unknown key '{key}'. Valid: {', '.join(sorted(valid_top_keys))}")

    flapping = changes.get("flapping", {})
    if isinstance(flapping, dict):
        for field in ("transition_count", "window_seconds", "stabilization_seconds"):
            _validate_positive_int(flapping, field, errors)

    recovery = changes.get("recovery", {})
    if isinstance(recovery, dict):
        for field in (
            "confirmation_probes", "confirmation_interval_s",
            "drain_pace_s", "embedding_pace_per_min", "queue_overflow_threshold",
        ):
            _validate_positive_int(recovery, field, errors)

    cc = changes.get("cc", {})
    if isinstance(cc, dict):
        _validate_positive_int(cc, "max_sessions_per_hour", errors)
        _validate_float_range(cc, "throttle_threshold_pct", 0.0, 1.0, errors)

    return errors


def _validate_inbox_monitor(changes: dict) -> list[str]:
    """Validate inbox monitor config changes."""
    errors: list[str] = []

    # The YAML has a top-level `inbox_monitor:` wrapper — auto-wrap flat changes
    if "inbox_monitor" not in changes:
        changes = {"inbox_monitor": changes}
    section = changes["inbox_monitor"]
    if not isinstance(section, dict):
        errors.append("inbox_monitor must be a mapping")
        return errors

    if "enabled" in section and not isinstance(section["enabled"], bool):
        errors.append("inbox_monitor.enabled must be a boolean")

    _validate_positive_int(section, "check_interval_seconds", errors)
    _validate_positive_int(section, "timeout_s", errors)

    if "batch_size" in section:
        try:
            val = int(section["batch_size"])
            if val < 1 or val > 10:
                errors.append("inbox_monitor.batch_size must be 1-10")
        except (ValueError, TypeError):
            errors.append("inbox_monitor.batch_size must be an integer")

    valid_models = VALID_MODEL_NAMES
    if "model" in section and section["model"] not in valid_models:
        errors.append(
            f"inbox_monitor.model must be one of {sorted(valid_models)}, "
            f"got '{section['model']}'"
        )

    valid_efforts = VALID_EFFORT_NAMES
    if "effort" in section and section["effort"] not in valid_efforts:
        errors.append(
            f"inbox_monitor.effort must be one of {sorted(valid_efforts)}, "
            f"got '{section['effort']}'"
        )

    # timezone removed — uses system timezone from genesis.env.user_timezone()

    return errors


def _validate_autonomous_cli_policy(changes: dict) -> list[str]:
    """Validate autonomous CLI policy changes."""
    errors: list[str] = []
    valid_top_keys = {
        "autonomous_cli_fallback_enabled",
        "manual_approval_required",
        "reask_interval_hours",
        "approval_channel",
        "shared_export_enabled",
    }
    for key in changes:
        if key not in valid_top_keys:
            errors.append(
                f"Unknown key '{key}'. Valid: {', '.join(sorted(valid_top_keys))}",
            )

    for key in (
        "autonomous_cli_fallback_enabled",
        "manual_approval_required",
        "shared_export_enabled",
    ):
        if key in changes and not isinstance(changes[key], bool):
            errors.append(f"{key} must be a boolean")

    if "reask_interval_hours" in changes:
        try:
            value = int(changes["reask_interval_hours"])
            if value < 1 or value > 168:
                errors.append("reask_interval_hours must be between 1 and 168")
        except (TypeError, ValueError):
            errors.append("reask_interval_hours must be an integer")

    if "approval_channel" in changes:
        channel = str(changes["approval_channel"] or "").strip().lower()
        if channel not in {"telegram"}:
            errors.append("approval_channel must currently be 'telegram'")

    return errors


def _validate_updates(changes: dict) -> list[str]:
    """Validate updates config changes."""
    errors: list[str] = []
    valid_top_keys = {"check", "notify", "auto_apply", "backup_before_update"}

    for key in changes:
        if key not in valid_top_keys:
            errors.append(f"Unknown key '{key}'. Valid: {', '.join(sorted(valid_top_keys))}")

    if "check" in changes:
        check = changes["check"]
        if not isinstance(check, dict):
            errors.append("check must be a mapping")
        else:
            if "enabled" in check and not isinstance(check["enabled"], bool):
                errors.append("check.enabled must be a boolean")
            if "interval_hours" in check:
                try:
                    val = int(check["interval_hours"])
                    if val < 1 or val > 168:
                        errors.append("check.interval_hours must be between 1 and 168")
                except (TypeError, ValueError):
                    errors.append("check.interval_hours must be an integer")

    if "notify" in changes:
        notify = changes["notify"]
        if not isinstance(notify, dict):
            errors.append("notify must be a mapping")
        else:
            if "enabled" in notify and not isinstance(notify["enabled"], bool):
                errors.append("notify.enabled must be a boolean")
            if "channel" in notify and notify["channel"] not in {"telegram"}:
                errors.append("notify.channel must currently be 'telegram'")

    if "auto_apply" in changes:
        auto_apply = changes["auto_apply"]
        if not isinstance(auto_apply, dict):
            errors.append("auto_apply must be a mapping")
        else:
            if "enabled" in auto_apply and not isinstance(auto_apply["enabled"], bool):
                errors.append("auto_apply.enabled must be a boolean")
            # Only safe impacts can be auto-applied. action_needed and
            # breaking ALWAYS require manual approval — enforced here so
            # the validator matches the config comment, even if a user
            # tries to override via settings_update.
            safe_impacts = {"none", "informational"}
            if "allowed_impacts" in auto_apply:
                impacts = auto_apply["allowed_impacts"]
                if not isinstance(impacts, list):
                    errors.append("auto_apply.allowed_impacts must be a list")
                else:
                    for impact in impacts:
                        if impact not in safe_impacts:
                            errors.append(
                                f"auto_apply.allowed_impacts: '{impact}' not allowed for "
                                f"auto-apply. Only {sorted(safe_impacts)} may be auto-applied; "
                                "action_needed and breaking always require manual approval."
                            )

    if "backup_before_update" in changes and not isinstance(changes["backup_before_update"], bool):
        errors.append("backup_before_update must be a boolean")

    return errors


def _validate_surplus(changes: dict) -> list[str]:
    errors: list[str] = []
    if "dispatch" in changes:
        d = changes["dispatch"]
        if isinstance(d, dict):
            _validate_positive_int(d, "interval_minutes", errors)
            _validate_positive_int(d, "task_expiry_hours", errors)
            _validate_positive_int(d, "max_iterations_per_cycle", errors)
    if "jobs" in changes:
        j = changes["jobs"]
        if isinstance(j, dict):
            for key in j:
                _validate_positive_int(j, key, errors)
    if "task_defaults" in changes:
        td = changes["task_defaults"]
        if isinstance(td, dict):
            valid_tiers = {"free_api", "cheap_paid", "local_30b", "never"}
            valid_drives = {"competence", "cooperation", "curiosity", "preservation"}
            for task_name, cfg in td.items():
                if not isinstance(cfg, dict):
                    errors.append(f"task_defaults.{task_name} must be a dict")
                    continue
                if "priority" in cfg:
                    _validate_float_range(cfg, "priority", 0.0, 1.0, errors)
                if "tier" in cfg and cfg["tier"] not in valid_tiers:
                    errors.append(f"task_defaults.{task_name}.tier must be one of {valid_tiers}")
                if "drive" in cfg and cfg["drive"] not in valid_drives:
                    errors.append(f"task_defaults.{task_name}.drive must be one of {valid_drives}")
    return errors


def _validate_ego(changes: dict) -> list[str]:
    from genesis.ego.config import validate_ego_config
    return validate_ego_config(changes)


def _validate_channels(changes: dict) -> list[str]:
    """Validate channel defaults config changes."""
    errors: list[str] = []
    valid_top_keys = {"telegram"}
    valid_models = VALID_MODEL_NAMES
    valid_efforts = VALID_EFFORT_NAMES

    for key in changes:
        if key not in valid_top_keys:
            errors.append(f"Unknown key '{key}'. Valid: {', '.join(sorted(valid_top_keys))}")

    tg = changes.get("telegram", {})
    if not isinstance(tg, dict):
        errors.append("telegram must be a mapping")
        return errors

    for key in tg:
        if key not in ("default_model", "default_effort"):
            errors.append(f"Unknown key 'telegram.{key}'. Valid: default_model, default_effort")

    if "default_model" in tg and tg["default_model"] not in valid_models:
        errors.append(
            f"telegram.default_model must be one of {sorted(valid_models)}, "
            f"got '{tg['default_model']}'"
        )

    if "default_effort" in tg and tg["default_effort"] not in valid_efforts:
        errors.append(
            f"telegram.default_effort must be one of {sorted(valid_efforts)}, "
            f"got '{tg['default_effort']}'"
        )

    return errors


def _validate_contribution(changes: dict) -> list[str]:
    """Validate contribution config changes."""
    errors: list[str] = []
    valid_keys = {"offer_enabled"}
    for key in changes:
        if key not in valid_keys:
            errors.append(f"Unknown key '{key}'. Valid: {', '.join(sorted(valid_keys))}")
    if "offer_enabled" in changes and not isinstance(changes["offer_enabled"], bool):
        errors.append("offer_enabled must be a boolean")
    return errors


def _validate_observability(changes: dict) -> list[str]:
    """Validate observability (spans) config changes."""
    errors: list[str] = []
    if set(changes) - {"spans"}:
        errors.append("Only the 'spans' key is configurable")
    spans = changes.get("spans", {})
    if not isinstance(spans, dict):
        errors.append("'spans' must be a mapping")
        return errors
    if set(spans) - {"enabled", "retention_days"}:
        errors.append("spans keys: enabled (bool), retention_days (int >= 1)")
    if "enabled" in spans and not isinstance(spans["enabled"], bool):
        errors.append("spans.enabled must be a boolean")
    if "retention_days" in spans and (
        not isinstance(spans["retention_days"], int)
        or isinstance(spans["retention_days"], bool)
        or spans["retention_days"] < 1
    ):
        errors.append("spans.retention_days must be an integer >= 1")
    return errors


def _validate_cc_roster(changes: dict) -> list[str]:
    """Validate cc_roster updates: a new `default` must be a known roster model,
    and — for a non-native model — must be actually usable (base_url/model_id set
    and its auth_env present in the environment).

    The runtime guardrail (roster.apply_active) degrades a misconfigured default to
    native Claude rather than going dark; this validator surfaces that misconfig
    LOUDLY at config-write time so a user never believes they're on GLM while
    silently running on Claude (no-silent-degrade)."""
    errors: list[str] = []
    if "default" in changes:
        default = changes["default"]
        if not isinstance(default, str):
            errors.append("default must be a string (a roster model name)")
        else:
            import os

            from genesis.cc.roster import load_roster

            model_defs = dict(load_roster().get("models") or {})
            chg_models = changes.get("models")
            if isinstance(chg_models, dict):
                model_defs.update(chg_models)
            if default not in model_defs:
                avail = ", ".join(sorted(model_defs)) or "(none)"
                errors.append(
                    f"default '{default}' is not a roster model; available: {avail}"
                )
            else:
                entry = model_defs[default] or {}
                native = bool(entry.get("native_subscription")) or default == "claude"
                if not native:
                    base_url = entry.get("anthropic_base_url")
                    model_id = entry.get("model_id")
                    auth_env = entry.get("auth_env")
                    if not (base_url and model_id and auth_env):
                        errors.append(
                            f"default '{default}' is missing "
                            "anthropic_base_url/model_id/auth_env"
                        )
                    elif not os.environ.get(auth_env):
                        errors.append(
                            f"default '{default}' requires env var {auth_env}, "
                            "which is not set — set it before routing or Genesis "
                            "would silently run on native Claude"
                        )
    return errors


def _validate_ws3_immunity(changes: dict) -> list[str]:
    """Validate ws3_immunity kill-switch changes (see genesis.security.immunity)."""
    from genesis.security.immunity import GATES, MODES

    errors: list[str] = []
    valid_top_keys = {"enabled", "auto_demote", "auto_demote_state", *GATES}
    for key, value in changes.items():
        if key not in valid_top_keys:
            errors.append(
                f"Unknown key '{key}'. Valid: {', '.join(sorted(valid_top_keys))}"
            )
        elif key == "enabled":
            if not isinstance(value, bool):
                errors.append("'enabled' must be a boolean")
        elif key in GATES:
            if not isinstance(value, dict):
                errors.append(f"'{key}' must be a mapping like {{mode: shadow}}")
            elif value.get("mode") not in MODES:
                errors.append(
                    f"'{key}.mode' must be one of {', '.join(MODES)}; "
                    f"got {value.get('mode')!r}"
                )
        elif key == "auto_demote":
            if not isinstance(value, dict):
                errors.append("'auto_demote' must be a mapping")
            else:
                if "enabled" in value and not isinstance(value["enabled"], bool):
                    errors.append("'auto_demote.enabled' must be a boolean")
                for int_key in ("window_minutes", "would_block_threshold"):
                    if int_key in value and (
                        not isinstance(value[int_key], int) or value[int_key] <= 0
                    ):
                        errors.append(f"'auto_demote.{int_key}' must be a positive int")
        # auto_demote_state: written by immunity.record_demotion via this
        # same overlay; accepted opaquely (hidden from the UI).
    return errors


_DOMAIN_VALIDATORS: dict[str, Any] = {
    "tts": _validate_tts,
    "ws3_immunity": _validate_ws3_immunity,
    "cc_roster": _validate_cc_roster,
    "resilience": _validate_resilience,
    "inbox_monitor": _validate_inbox_monitor,
    "autonomous_cli_policy": _validate_autonomous_cli_policy,
    "updates": _validate_updates,
    "surplus": _validate_surplus,
    "ego": _validate_ego,
    "channels": _validate_channels,
    "contribution": _validate_contribution,
    "observability": _validate_observability,
}


# ── Shared validation helpers ──────────────────────────────────────────


def _validate_float_range(
    d: dict, key: str, lo: float, hi: float, errors: list[str],
) -> None:
    if key not in d:
        return
    try:
        val = float(d[key])
        if val < lo or val > hi:
            errors.append(f"{key} must be {lo}-{hi}, got {val}")
    except (ValueError, TypeError):
        errors.append(f"{key} must be a number, got {d[key]!r}")


def _validate_positive_int(d: dict, key: str, errors: list[str]) -> None:
    if key not in d:
        return
    try:
        val = int(d[key])
        if val <= 0:
            errors.append(f"{key} must be a positive integer, got {val}")
    except (ValueError, TypeError):
        errors.append(f"{key} must be an integer, got {d[key]!r}")


# ── Tool implementations ──────────────────────────────────────────────


async def _impl_settings_list() -> list[dict]:
    return [
        {
            "domain": d.name,
            "description": d.description,
            "readonly": d.readonly,
            "readonly_reason": d.readonly_reason,
            "needs_restart": d.needs_restart,
            "dedicated_tool": d.dedicated_tool,
        }
        for d in _DOMAIN_REGISTRY.values()
    ]


async def _impl_settings_get(domain: str) -> dict:
    entry = _DOMAIN_REGISTRY.get(domain)
    if entry is None:
        available = ", ".join(sorted(_DOMAIN_REGISTRY))
        return {"error": f"Unknown domain '{domain}'. Available: {available}"}

    if entry.dedicated_tool:
        return {
            "domain": domain,
            "note": f"Use the '{entry.dedicated_tool}' tool for richer access to {domain} settings.",
            "readonly": entry.readonly,
            "dedicated_tool": entry.dedicated_tool,
        }

    config = _load_yaml_merged(entry.config_filename)
    # Strip deprecated/hidden fields before serving
    for field in entry.hidden_fields:
        config.pop(field, None)
        wrapper = config.get(domain)
        if isinstance(wrapper, dict):
            wrapper.pop(field, None)
    local_file = _local_filename(entry.config_filename)
    has_local = (
        (_USER_CONFIG_DIR / local_file).is_file()
        or (_CONFIG_DIR / local_file).is_file()
    )
    result = {
        "domain": domain,
        "config": config,
        "readonly": entry.readonly,
        "needs_restart": entry.needs_restart,
        "source_file": f"config/{entry.config_filename}",
    }
    if has_local:
        result["local_override_file"] = f"~/.genesis/config/{local_file}"
    return result


async def _impl_settings_update(
    domain: str, changes: dict, dry_run: bool = False,
) -> dict:
    entry = _DOMAIN_REGISTRY.get(domain)
    if entry is None:
        available = ", ".join(sorted(_DOMAIN_REGISTRY))
        return {"error": f"Unknown domain '{domain}'. Available: {available}"}

    if entry.readonly:
        return {
            "domain": domain,
            "error": f"Domain '{domain}' is read-only. {entry.description}",
        }

    if entry.dedicated_tool:
        return {
            "domain": domain,
            "error": f"Use the '{entry.dedicated_tool}' tool to modify {domain} settings.",
        }

    # Normalize: inbox_monitor YAML has a top-level wrapper key
    if domain == "inbox_monitor" and "inbox_monitor" not in changes:
        changes = {"inbox_monitor": changes}

    # Validate
    validator = _DOMAIN_VALIDATORS.get(domain)
    if validator:
        errors = validator(changes)
        if errors:
            return {
                "domain": domain,
                "error": "validation failed",
                "validation_errors": errors,
            }

    # Merge changes into the local overlay (NOT the base file).
    # The base file stays git-tracked and clean for upstream updates.
    local = _load_yaml_local(entry.config_filename)
    new_local = _deep_merge(local, changes)

    if dry_run:
        # Show what the full merged config would look like
        base = _load_yaml(entry.config_filename)
        return {
            "domain": domain,
            "status": "dry_run_ok",
            "changes_applied": changes,
            "merged_preview": _deep_merge(base, new_local),
            "needs_restart": entry.needs_restart,
        }

    # Atomic write to .local.yaml
    local_file = _local_filename(entry.config_filename)
    try:
        _atomic_yaml_write(local_file, new_local)
    except Exception:
        logger.error(
            "Failed to write local settings for %s", domain, exc_info=True,
        )
        return {"domain": domain, "error": "Failed to write local config file"}

    result: dict = {
        "domain": domain,
        "status": "applied",
        "changes_applied": changes,
        "local_override_file": f"~/.genesis/config/{local_file}",
        "needs_restart": entry.needs_restart,
    }
    if entry.needs_restart:
        result["note"] = "Changes saved. Restart genesis-server for them to take effect."

    return result


# ── MCP tool wrappers ──────────────────────────────────────────────────


@mcp.tool()
async def settings_list() -> list[dict]:
    """List all configurable settings domains.

    Returns each domain's name, description, whether it is read-only,
    whether changes require a restart, and whether it has a dedicated
    MCP tool. Use this to discover what can be configured.
    """
    return await _impl_settings_list()


@mcp.tool()
async def settings_get(domain: str) -> dict:
    """Read the current configuration for a settings domain.

    Returns the full config as a structured dict. Use settings_list()
    first to see available domains. For outreach settings, prefer the
    outreach_preferences tool which has richer semantics.
    """
    return await _impl_settings_get(domain)


@mcp.tool()
async def settings_update(
    domain: str,
    changes: dict,
    dry_run: bool = False,
) -> dict:
    """Update configuration for a settings domain.

    Provide a dict of changes to merge (partial update — only specified
    keys change, existing keys are preserved). Set dry_run=True to
    validate without saving. Read-only domains are rejected.

    Example: settings_update("tts", {"elevenlabs": {"stability": 0.9}})
    """
    return await _impl_settings_update(domain, changes, dry_run=dry_run)
