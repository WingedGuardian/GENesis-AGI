"""Config loader for model routing — YAML → RoutingConfig, and save back."""

from __future__ import annotations

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


def load_config(path: str | Path) -> RoutingConfig:
    """Load routing config from a YAML file path."""
    text = Path(path).read_text()
    return load_config_from_string(text)


def load_config_from_string(text: str) -> RoutingConfig:
    """Load routing config from a YAML string."""
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

        call_sites[name] = CallSiteConfig(
            id=name,
            chain=chain,
            default_paid=cs.get("default_paid", False),
            never_pays=cs.get("never_pays", False),
            retry_profile=retry_profile,
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
) -> RoutingConfig:
    """Update a single call site in the YAML config file.

    Uses atomic write (write .new, validate, rename) with rolling backups.
    Returns the newly loaded config if successful.
    Raises ValueError on validation failure.
    """
    path = Path(path)
    raw = yaml.safe_load(path.read_text())

    if call_site_id not in (raw.get("call_sites") or {}):
        msg = f"Unknown call site: {call_site_id}"
        raise ValueError(msg)

    cs = raw["call_sites"][call_site_id]
    providers = raw.get("providers") or {}

    # Early return if nothing to change
    if chain is None and default_paid is None and never_pays is None:
        return load_config(path)

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
        cs["chain"] = chain

    if default_paid is not None:
        cs["default_paid"] = default_paid

    if never_pays is not None:
        cs["never_pays"] = never_pays

    # Validate: never_pays sites must have at least one free provider
    if cs.get("never_pays"):
        free_in_chain = [p for p in cs["chain"] if providers.get(p, {}).get("free")]
        if not free_in_chain:
            msg = f"never_pays site '{call_site_id}' must have at least one free provider"
            raise ValueError(msg)

    # Atomic write: .new → validate parse → rotate backups → rename
    new_text = yaml.dump(raw, default_flow_style=False, sort_keys=False)
    new_path = path.with_suffix(".yaml.new")
    new_path.write_text(new_text)

    # Validate the new config parses correctly
    try:
        new_config = load_config(new_path)
    except Exception as e:
        new_path.unlink(missing_ok=True)
        msg = f"Generated config failed validation: {e}"
        raise ValueError(msg) from e

    # Rolling backups (.bak.3 → .bak.2 → .bak.1 → current)
    for i in range(3, 1, -1):
        older = path.with_suffix(f".yaml.bak.{i}")
        newer = path.with_suffix(f".yaml.bak.{i - 1}")
        if newer.exists():
            shutil.copy2(newer, older)
    bak1 = path.with_suffix(".yaml.bak.1")
    if path.exists():
        shutil.copy2(path, bak1)

    # Atomic rename
    new_path.rename(path)
    logger.info("Routing config updated: call site '%s' modified", call_site_id)

    return new_config
