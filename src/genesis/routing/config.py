"""Config loader for model routing — YAML → RoutingConfig, and save back."""

from __future__ import annotations

import copy
import logging
import os
import re
import shutil
from pathlib import Path

import yaml

from genesis.routing.types import (
    CallSiteConfig,
    ProviderConfig,
    RetryPolicy,
    RoutingConfig,
)

logger = logging.getLogger(__name__)
_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-(.*?))?\}")

# Canonical set of runtime dispatch modes honoured by
# ``AutonomousDispatchRouter.route()``.  Also used by
# ``update_call_site_in_yaml`` to validate save payloads — keep in sync
# with the dashboard neural-monitor selector and with
# ``CallSiteConfig.dispatch``.
_VALID_DISPATCH_MODES = frozenset({"api", "cli", "dual"})


def _normalize_dispatch(raw: object, *, call_site_name: str) -> str:
    """Return the canonical dispatch mode for a raw YAML value.

    Missing / None → ``"dual"`` (current behaviour, zero-change default).
    Legacy alias ``"cc"`` (written by earlier UI code before the three-
    state selector landed) → ``"cli"``.  Unknown values are downgraded
    to ``"dual"`` with a WARNING log so misconfiguration never silently
    bypasses the CLI gate.
    """
    if raw is None:
        return "dual"
    if not isinstance(raw, str):
        logger.warning(
            "Call site '%s' has non-string dispatch value %r — defaulting to 'dual'",
            call_site_name, raw,
        )
        return "dual"
    value = raw.strip().lower()
    if value == "cc":
        return "cli"
    if value in _VALID_DISPATCH_MODES:
        return value
    logger.warning(
        "Call site '%s' has unknown dispatch mode %r — defaulting to 'dual'. "
        "Valid values: %s",
        call_site_name, raw, sorted(_VALID_DISPATCH_MODES),
    )
    return "dual"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base. Lists are replaced, not appended."""
    merged = copy.deepcopy(base)
    for key, val in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def _local_path_for(path: Path) -> Path:
    """Derive the .local.yaml path for a base config file."""
    return path.with_name(f"{path.stem}.local.yaml")


def _load_local_overlay(path: Path) -> dict:
    """Load the .local.yaml overlay for a config path. Returns {} if none."""
    local = _local_path_for(path)
    if not local.is_file():
        return {}
    try:
        return yaml.safe_load(local.read_text()) or {}
    except Exception:
        logger.warning("Failed to read local overlay %s", local, exc_info=True)
        return {}


def _sanitize_local_overlay(base_raw: dict, local_raw: dict) -> dict:
    """Filter stale references from a local overlay before merging.

    Removes provider references from local call site chains that don't
    exist in the base config's providers section. This prevents a stale
    .local.yaml from breaking startup after an upstream update removes
    a provider.

    Returns a sanitized copy — does NOT mutate the input.
    """
    result = copy.deepcopy(local_raw)
    base_providers = set((base_raw.get("providers") or {}).keys())
    local_call_sites = (result.get("call_sites") or {})

    for cs_name, cs in list(local_call_sites.items()):
        if not isinstance(cs, dict) or "chain" not in cs:
            continue
        original_chain = cs["chain"]
        filtered = [p for p in original_chain if p in base_providers]
        stale = set(original_chain) - set(filtered)
        if stale:
            logger.warning(
                "Local override for call site '%s' references unknown "
                "provider(s) %s (removed upstream?) — skipping them",
                cs_name, sorted(stale),
            )
        if not filtered:
            logger.warning(
                "Local override for call site '%s' has no valid providers "
                "after filtering — dropping local chain override",
                cs_name,
            )
            del cs["chain"]
            if not cs:
                del local_call_sites[cs_name]
        else:
            cs["chain"] = filtered

    return result


def load_config(path: str | Path) -> RoutingConfig:
    """Load routing config from a YAML file path.

    Checks for a ``{stem}.local.yaml`` overlay in the same directory and
    deep-merges it on top of the base config before parsing. Local overlays
    are gitignored and survive upstream updates.
    """
    path = Path(path)
    text = path.read_text()
    base_raw = yaml.safe_load(_expand_env_vars(text))

    local_raw = _load_local_overlay(path)
    if local_raw:
        local_raw = _sanitize_local_overlay(base_raw, local_raw)
        if local_raw:
            base_raw = _deep_merge(base_raw, local_raw)

    return _parse(base_raw)


def load_config_from_string(text: str) -> RoutingConfig:
    """Load routing config from a YAML string (no overlay support)."""
    raw = yaml.safe_load(_expand_env_vars(text))
    return _parse(raw)


def _expand_env_vars(text: str) -> str:
    """Expand ${VAR} and ${VAR:-default} placeholders in config text."""

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        default = match.group(2)
        return os.environ.get(key, default if default is not None else match.group(0))

    return _ENV_PATTERN.sub(repl, text)


def _parse(raw: dict) -> RoutingConfig:
    """Parse raw YAML dict into a validated RoutingConfig."""
    if not isinstance(raw, dict):
        msg = "Config must be a YAML mapping"
        raise ValueError(msg)

    # --- Retry profiles ---
    retry_profiles: dict[str, RetryPolicy] = {}
    for name, rp in (raw.get("retry") or {}).items():
        retry_profiles[name] = RetryPolicy(
            max_retries=rp.get("max_retries", 3),
            base_delay_ms=rp.get("base_delay_ms", 500),
            max_delay_ms=rp.get("max_delay_ms", 30000),
            backoff_multiplier=rp.get("backoff_multiplier", 2.0),
            jitter_pct=rp.get("jitter_pct", 0.25),
        )
    # Ensure "default" always exists
    if "default" not in retry_profiles:
        retry_profiles["default"] = RetryPolicy()

    # --- Providers ---
    providers: dict[str, ProviderConfig] = {}
    disabled_providers: set[str] = set()
    for name, p in (raw.get("providers") or {}).items():
        # Parse enabled field — supports bool, string from env var expansion
        enabled_raw = p.get("enabled", True)
        if isinstance(enabled_raw, str):
            enabled = enabled_raw.strip().lower() not in {"0", "false", "no", "off", ""}
        else:
            enabled = bool(enabled_raw)

        if not enabled:
            disabled_providers.add(name)
            logger.info("Provider '%s' disabled via config", name)
            continue

        providers[name] = ProviderConfig(
            name=name,
            provider_type=p["type"],
            model_id=p["model"],
            is_free=p.get("free", False),
            rpm_limit=p.get("rpm_limit"),
            open_duration_s=p.get("open_duration_s", 120),
            base_url=p.get("base_url"),
            keep_alive=p.get("keep_alive"),
            enabled=True,
            profile=p.get("profile"),
        )

    # --- Call sites ---
    call_sites: dict[str, CallSiteConfig] = {}
    for name, cs in (raw.get("call_sites") or {}).items():
        chain = cs["chain"]
        # Filter out disabled providers from chain
        chain = [p for p in chain if p not in disabled_providers]
        if not chain:
            logger.warning(
                "Call site '%s' has no enabled providers — all were disabled", name,
            )
            continue
        # Validate remaining providers exist
        for provider in chain:
            if provider not in providers:
                msg = f"Call site '{name}' references unknown provider '{provider}'"
                raise ValueError(msg)

        retry_profile = cs.get("retry_profile", "default")
        if retry_profile not in retry_profiles:
            msg = (
                f"Call site '{name}' references unknown "
                f"retry profile '{retry_profile}'"
            )
            raise ValueError(msg)

        dispatch = _normalize_dispatch(cs.get("dispatch"), call_site_name=name)

        call_sites[name] = CallSiteConfig(
            id=name,
            chain=chain,
            default_paid=cs.get("default_paid", False),
            never_pays=cs.get("never_pays", False),
            retry_profile=retry_profile,
            dispatch=dispatch,
        )

    return RoutingConfig(
        providers=providers,
        call_sites=call_sites,
        retry_profiles=retry_profiles,
    )


def update_call_site_in_yaml(
    path: str | Path,
    call_site_id: str,
    *,
    chain: list[str] | None = None,
    default_paid: bool | None = None,
    never_pays: bool | None = None,
    cc_model: str | None = None,
    cc_position: int | None = None,
    dispatch: str | None = None,
) -> RoutingConfig:
    """Update a single call site, writing changes to the local overlay.

    Reads the base config for validation (provider existence, etc.) but
    writes user changes to ``{stem}.local.yaml`` so the base file stays
    clean for upstream git updates.

    Uses atomic write with rolling backups on the local overlay file.
    Returns the newly loaded (merged) config if successful.
    Raises ValueError on validation failure.

    ``dispatch`` is the user-controlled runtime mode:
      - 'api'  → force API chain execution (hard fail if unavailable)
      - 'cli'  → force CC subprocess execution
      - 'dual' → auto (dispatcher picks; legacy behavior)
      - None   → leave the existing yaml value unchanged
    """
    path = Path(path)
    base_raw = yaml.safe_load(path.read_text())

    if call_site_id not in (base_raw.get("call_sites") or {}):
        msg = f"Unknown call site: {call_site_id}"
        raise ValueError(msg)

    # Build the change dict for the local overlay
    providers = base_raw.get("providers") or {}

    if dispatch is not None and dispatch not in _VALID_DISPATCH_MODES:
        msg = f"Invalid dispatch mode: {dispatch!r}. Must be one of {_VALID_DISPATCH_MODES}"
        raise ValueError(msg)

    # Early return if nothing to change
    if (
        chain is None
        and default_paid is None
        and never_pays is None
        and cc_model is None
        and cc_position is None
        and dispatch is None
    ):
        return load_config(path)

    # Start with existing local overlay for this call site
    local_path = _local_path_for(path)
    local_raw = _load_local_overlay(path)
    local_cs = local_raw.setdefault("call_sites", {}).setdefault(call_site_id, {})

    # Resolve effective call site (base + existing local) for validation
    base_cs = base_raw["call_sites"][call_site_id]
    effective_cs = _deep_merge(base_cs, local_cs)

    if chain is not None:
        if not chain:
            msg = "Chain must have at least one provider"
            raise ValueError(msg)
        if len(chain) != len(set(chain)):
            msg = "Chain must not contain duplicate providers"
            raise ValueError(msg)
        for p in chain:
            if p not in providers:
                msg = f"Unknown provider in chain: {p}"
                raise ValueError(msg)
        local_cs["chain"] = chain
        effective_cs["chain"] = chain

    if default_paid is not None:
        local_cs["default_paid"] = default_paid
        effective_cs["default_paid"] = default_paid

    if never_pays is not None:
        local_cs["never_pays"] = never_pays
        effective_cs["never_pays"] = never_pays

    # CC dispatch metadata (stored in YAML, read by dashboard)
    _VALID_CC_MODELS = {"Haiku", "Sonnet", "Opus"}
    if cc_model is not None and cc_model not in _VALID_CC_MODELS:
        msg = f"Invalid CC model: {cc_model!r}. Must be one of {_VALID_CC_MODELS}"
        raise ValueError(msg)
    if cc_position is not None:
        cc_position = int(cc_position)
        if cc_position < 0:
            cc_position = None
    if cc_model is not None:
        local_cs["cc_model"] = cc_model
        if dispatch is None:
            local_cs["dispatch"] = "dual" if chain else effective_cs.get("dispatch", "cc")
        if cc_position is not None:
            local_cs["cc_position"] = cc_position
        else:
            local_cs.pop("cc_position", None)
    elif chain is not None and cc_model is None and dispatch is None:
        local_cs.pop("cc_model", None)
        local_cs.pop("dispatch", None)
        local_cs.pop("cc_position", None)

    if dispatch is not None:
        local_cs["dispatch"] = dispatch
        if dispatch == "api":
            local_cs.pop("cc_model", None)
            local_cs.pop("cc_position", None)

    # Validate: never_pays sites must have at least one free provider
    effective_chain = effective_cs.get("chain", base_cs.get("chain", []))
    if effective_cs.get("never_pays"):
        free_in_chain = [p for p in effective_chain if providers.get(p, {}).get("free")]
        if not free_in_chain:
            msg = f"never_pays site '{call_site_id}' must have at least one free provider"
            raise ValueError(msg)

    # Validate the merged config in-memory before touching disk.
    # Merges local_raw (with new changes) onto base_raw and parses it.
    try:
        merged_raw = _deep_merge(base_raw, local_raw)
        new_config = _parse(merged_raw)
    except Exception as e:
        msg = f"Generated config failed validation: {e}"
        raise ValueError(msg) from e

    # Atomic write to local overlay: .new → rotate backups → rename
    new_text = yaml.dump(local_raw, default_flow_style=False, sort_keys=False)
    new_local_path = local_path.with_suffix(".yaml.new")
    new_local_path.write_text(new_text)

    # Rolling backups on the local overlay (.bak.3 → .bak.2 → .bak.1)
    for i in range(3, 1, -1):
        older = local_path.with_suffix(f".yaml.bak.{i}")
        newer = local_path.with_suffix(f".yaml.bak.{i - 1}")
        if newer.exists():
            shutil.copy2(newer, older)
    bak1 = local_path.with_suffix(".yaml.bak.1")
    if local_path.exists():
        shutil.copy2(local_path, bak1)

    # Atomic rename
    new_local_path.rename(local_path)
    logger.info(
        "Routing config updated: call site '%s' modified in local overlay",
        call_site_id,
    )

    return new_config
