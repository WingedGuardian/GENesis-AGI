"""Config loader for model routing — YAML → RoutingConfig, and save back."""

from __future__ import annotations

import copy
import dataclasses
import logging
import math
import os
import re
import shutil
from pathlib import Path

import yaml

from genesis.cc.types import CCModel
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


def _load_local_overlay(path: Path) -> object:
    """Read the .local.yaml overlay for a config path. Returns {} if none.

    RAISES on a file that cannot be read or is not valid YAML, and returns
    whatever shape the YAML holds. Deciding what an unusable overlay means is
    the caller's job: the loader falls back to the base config loudly, and the
    save path refuses to write over a file it could not read.
    """
    local = _local_path_for(path)
    if not local.is_file():
        return {}
    loaded = yaml.safe_load(local.read_text())
    return {} if loaded is None else loaded


#: The overlay sections ``_parse`` iterates. Each must be a mapping when present.
_OVERLAY_SECTIONS = ("providers", "call_sites", "retry")


def _merge_overlay(base_raw: dict, local_raw: object) -> dict:
    """Validate the overlay's structure, sanitize it, and merge it onto the base.

    Raises ValueError when the overlay is not a mapping or one of its
    ``_OVERLAY_SECTIONS`` is present but is not a mapping. The case this is
    needed for is a NULL section, a bare ``call_sites:``: it replaces the base
    section, and ``_parse`` reads a null section as empty, so without this
    check routing would load zero call sites with nothing to fall back from.
    """
    if not isinstance(local_raw, dict):
        msg = f"overlay must be a YAML mapping, got {type(local_raw).__name__}"
        raise ValueError(msg)
    for section in _OVERLAY_SECTIONS:
        if section in local_raw and not isinstance(local_raw[section], dict):
            msg = (
                f"overlay section '{section}' must be a mapping, got "
                f"{type(local_raw[section]).__name__}"
            )
            raise ValueError(msg)
    sanitized = _sanitize_local_overlay(base_raw, local_raw)
    return _deep_merge(base_raw, sanitized) if sanitized else base_raw


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
    base_call_sites = set((base_raw.get("call_sites") or {}).keys())
    local_call_sites = (result.get("call_sites") or {})

    for cs_name, cs in list(local_call_sites.items()):
        # A local overlay may OVERRIDE an existing base call site, never
        # resurrect one the base removed. Drop overlay entries whose ID is
        # absent from the base (e.g. a stale dashboard edit to a since-deleted
        # site like 7_ego_cycle) so a .local.yaml can't re-introduce a removed
        # routed call site after it is deleted upstream.
        if cs_name not in base_call_sites:
            logger.warning(
                "Local override for call site '%s' has no matching base entry "
                "(removed upstream?) — dropping the stale overlay",
                cs_name,
            )
            del local_call_sites[cs_name]
            continue
        if not isinstance(cs, dict) or "chain" not in cs:
            continue
        original_chain = cs["chain"]
        if not isinstance(original_chain, list):
            # A string would otherwise be iterated character by character and
            # every character filtered out as an unknown provider.
            msg = (
                f"call site '{cs_name}': chain must be a list, got "
                f"{type(original_chain).__name__}"
            )
            raise ValueError(msg)
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


def load_config(
    path: str | Path, *, check_api_keys: bool = True, strict_overlay: bool = False
) -> RoutingConfig:
    """Load routing config from a YAML file path.

    Checks for a ``{stem}.local.yaml`` overlay in the same directory and
    deep-merges it on top of the base config before parsing. Local overlays
    are gitignored and survive upstream updates.

    If the overlay cannot be read, merged, or parsed, the WHOLE overlay is set
    aside and the base config is loaded on its own. The failure is logged at
    ERROR and recorded as a health observation. Without this, most malformed
    overlays raised out of here, and ``runtime/init/router.py`` catches that and
    leaves the runtime with no router, so every LLM call site went dark behind
    one log line. The base is validated in CI (``test_config_invariants``), so
    it is the safe thing to fall back to. A base that fails to parse still
    raises.

    ``strict_overlay=True`` re-raises the overlay error instead. It is for
    operator-initiated reloads, where the right answer to a broken file is to
    say so and keep the running config.
    """
    return _load_effective(
        Path(path), check_api_keys=check_api_keys, strict_overlay=strict_overlay
    )[0]


def _load_effective(
    path: Path, *, check_api_keys: bool = True, strict_overlay: bool = False
) -> tuple[RoutingConfig, dict]:
    """``load_config``, also returning the raw dict the config was parsed from.

    Readers that need raw fields ``_parse`` does not keep (the dashboard's CC
    display) use this so they see the same overlay-or-base choice the router
    made, not an overlay the loader rejected.
    """
    base_raw = yaml.safe_load(_expand_env_vars(path.read_text()))
    local_path = _local_path_for(path)
    if not local_path.is_file():
        return _parse(base_raw, check_api_keys=check_api_keys), base_raw
    try:
        local_raw = _load_local_overlay(path)
        merged = _merge_overlay(base_raw, local_raw) if local_raw else base_raw
        return _parse(merged, check_api_keys=check_api_keys), merged
    except Exception as exc:
        if strict_overlay:
            raise
        config = _parse(base_raw, check_api_keys=check_api_keys)
        _report_overlay_rejected(local_path, exc)
        return config, base_raw


#: Last-reported (mtime, cause) per overlay path. ``load_config`` runs on every
#: dashboard vitals read and in every eval/standalone process, so a broken
#: overlay is reported once per file version AND cause: an edit, or a different
#: error at the same mtime (the base changed underneath it), reports again.
_REPORTED_OVERLAYS: dict[str, tuple[float, str]] = {}


def _report_overlay_rejected(local_path: Path, exc: BaseException) -> None:
    """Log at ERROR and record a health observation. Never raises."""
    try:
        mtime = local_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = str(local_path)
    detail = f"{type(exc).__name__}: {exc}"
    if _REPORTED_OVERLAYS.get(key) == (mtime, detail):
        return
    _REPORTED_OVERLAYS[key] = (mtime, detail)
    logger.error(
        "Routing overlay %s rejected; running on the shipped routing config "
        "without it. Fix or remove the file. Cause: %s",
        local_path, detail, exc_info=exc,
    )
    try:
        from genesis.db.crud.observations import create_sync
        from genesis.env import genesis_db_path

        # create_sync never raises and returns False on a failed or deduped
        # write; the ERROR line above is the record that cannot be lost.
        create_sync(
            str(genesis_db_path()),
            source="routing",
            type="init_degradation",
            category="infrastructure",
            priority="high",
            content=(
                f"[routing] {local_path.name} was rejected and every override in "
                f"it is inactive; routing runs on the shipped config. Cause: {detail}"
            ),
        )
    except Exception:  # noqa: BLE001 - an import failure must not break config loading
        logger.warning("Could not record the routing overlay observation", exc_info=True)


def load_config_from_string(text: str, *, check_api_keys: bool = True) -> RoutingConfig:
    """Load routing config from a YAML string (no overlay support)."""
    raw = yaml.safe_load(_expand_env_vars(text))
    return _parse(raw, check_api_keys=check_api_keys)


#: Placeholders that have a real accessor in ``genesis.env``. For these the
#: accessor is authoritative, because it — and not this function — implements the
#: documented precedence: environment, then ~/.genesis/config/genesis.yaml, then a
#: hardcoded default.
#:
#: WHY THIS EXISTS. Expanding from ``os.environ`` alone made the routing layer the
#: ONE consumer that could not see the yaml config, and the split was silent: an
#: install pointing ``network.ollama_url`` at a remote server had its dashboard,
#: health check and embeddings reach that server while routed model calls still
#: went to localhost. It was masked for as long as secrets.env.example force-
#: assigned the same values, since env then agreed with the default by accident;
#: removing those assignments so the yaml lever could work is what exposed it.
#: Nothing here changes when the environment variable IS set — the accessor
#: returns it first, so env still wins.
_ENV_ACCESSORS: dict[str, str] = {
    "OLLAMA_URL": "ollama_url",
    "LM_STUDIO_URL": "lm_studio_url",
    "LM_STUDIO_HEALTH_URL": "lm_studio_health_url",
    "GENESIS_ENABLE_OLLAMA": "ollama_enabled",
}


def _expand_env_vars(text: str) -> str:
    """Expand ${VAR} and ${VAR:-default} placeholders in config text.

    A placeholder listed in ``_ENV_ACCESSORS`` resolves through that accessor
    rather than the raw environment, so routing agrees with every other consumer
    of the same setting. Everything else keeps the previous behaviour exactly:
    environment, else the inline default, else the placeholder untouched.
    """

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        default = match.group(2)
        accessor = _ENV_ACCESSORS.get(key)
        if accessor is not None:
            try:
                from genesis import env as _genesis_env  # noqa: PLC0415 — lazy: keep import light

                value = getattr(_genesis_env, accessor)()
            except Exception:
                # Never let a config-resolution problem take routing down: fall
                # back to the previous behaviour rather than raising into a
                # module that every model call depends on.
                logger.warning("env accessor %s failed for %s", accessor, key, exc_info=True)
            else:
                # yaml booleans must render as the lowercase tokens the config
                # expects, not Python's "True"/"False".
                return str(value).lower() if isinstance(value, bool) else str(value)
        return os.environ.get(key, default if default is not None else match.group(0))

    return _ENV_PATTERN.sub(repl, text)


# OpenRouter free-tier convention: a genuinely-free model carries a ":free"
# slug suffix; a BARE slug routes to PAID endpoints. So an OpenRouter provider
# flagged `free: true` whose slug is NOT ":free"-suffixed is a mislabel — a paid
# model billed at OpenRouter while the router records $0 (`is_free` zeroes cost),
# so real spend is invisible (the openrouter-free regression, 2026-08). This
# allowlist holds genuine $0 OpenRouter endpoints that legitimately lack the
# ":free" suffix (the free-pool meta-router).
_FREE_OPENROUTER_ALLOWLIST = frozenset({"openrouter/free"})


def _detect_mislabeled_free_openrouter(
    providers: dict[str, ProviderConfig],
) -> list[str]:
    """Return warning strings for OpenRouter providers flagged ``free: true``
    whose model slug — or any curated ``params.extra_body.models`` fallback
    member — is not ``:free``-suffixed (and not an allowlisted $0 meta-router).

    A config-only, load-time guard (no profile/network dependency). It does NOT
    gate routing — visibility only, per "cost is observability, not control".

    Scoped to OpenRouter deliberately: the ``:free`` slug convention is
    OpenRouter-specific. Other providers are free-by-account-tier and
    legitimately keep list prices in their model_profiles, so a
    profile-rate-based check would false-positive on them.
    """
    findings: list[str] = []
    for name, cfg in providers.items():
        if not cfg.is_free or cfg.provider_type != "openrouter":
            continue
        slugs = [cfg.model_id]
        params = cfg.params if isinstance(cfg.params, dict) else {}
        extra = params.get("extra_body")
        models = extra.get("models") if isinstance(extra, dict) else None
        if isinstance(models, list):
            slugs.extend(m for m in models if isinstance(m, str))
        suspect = [
            s
            for s in slugs
            if not s.endswith(":free") and s not in _FREE_OPENROUTER_ALLOWLIST
        ]
        if suspect:
            findings.append(
                f"{name}: free:true but OpenRouter slug(s) are not ':free' "
                f"(paid endpoint — bills while tracked as $0): {suspect}"
            )
    return findings


def _parse_daily_limit(provider: str, key: str, value) -> int | None:
    """Validate a daily-budget limit at PARSE time, so the hot-path
    ``exhausted()`` comparison can trust the type. Env expansion / quoted
    YAML can deliver strings (same reality the ``enabled`` parser handles),
    and a string limit would raise inside the router's chain walk — turning
    this deliberately fail-open feature fail-closed for every chain holding
    the provider. Zero/negative is rejected outright: a born-exhausted
    provider is deselected all day with no crossing event to announce it.
    """
    if value is None:
        return None
    # `int(value)` is NOT integer validation, and the docstring above promised
    # it was. YAML gives `1.9` as a float and `true` as a bool (a subclass of
    # int), and both survive: 1.9 truncates to 1 and True IS 1, so a typo in a
    # user overlay silently becomes a ONE-REQUEST daily limit that deselects the
    # provider after a single call, with no correction until the UTC day rolls
    # over (Codex P2, PR #1624). Booleans are rejected before the int check
    # because `isinstance(True, int)` is True — testing the type after coercion
    # would let them through.
    if isinstance(value, bool) or not isinstance(value, int | str):
        msg = f"provider '{provider}': {key} must be an integer, got {value!r}"
        raise ValueError(msg)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        msg = f"provider '{provider}': {key} must be an integer, got {value!r}"
        raise ValueError(msg) from None
    if parsed <= 0:
        msg = f"provider '{provider}': {key} must be positive, got {parsed}"
        raise ValueError(msg)
    return parsed


def _entry(section: str, name: str, value: object) -> dict:
    """An entry under providers / call_sites / retry must be a mapping."""
    if not isinstance(value, dict):
        msg = f"{section} entry '{name}' must be a mapping, got {type(value).__name__}"
        raise ValueError(msg)
    return value


def _number(
    where: str, key: str, value: object, *, integer: bool = False, allow_none: bool = False
):
    """Type-check a numeric field at PARSE time.

    These fields feed arithmetic on the routing hot path (``route_start +
    max_total_s``, rate-gate intervals, backoff). A wrong type there does not
    fail at load: it fails as a TypeError inside every call that reaches the
    field. Raising here instead turns it into a load error, which the overlay
    fallback in ``load_config`` can contain. ``bool`` is rejected explicitly
    because it is a subclass of ``int``.
    """
    if value is None and allow_none:
        return None
    kinds = int if integer else (int, float)
    # NaN passes `< 0` and every later comparison is False, so a NaN deadline
    # never fires and a NaN breaker window never recloses. Infinity likewise.
    # `None` (allow_none) is the only way to spell "no limit".
    if (
        isinstance(value, bool)
        or not isinstance(value, kinds)
        or not math.isfinite(value)
        or value < 0
    ):
        kind = "a non-negative integer" if integer else "a non-negative number"
        msg = f"{where}: {key} must be {kind}, got {value!r}"
        raise ValueError(msg)
    return value


def _flag(where: str, key: str, value: object) -> bool:
    """A boolean field. null clears it to false, like the other clearing nulls.
    Anything else that is not a bool is refused: the string "false" is truthy."""
    if value is None:
        return False
    if not isinstance(value, bool):
        msg = f"{where}: {key} must be true or false, got {value!r}"
        raise ValueError(msg)
    return value


def _optional_str(where: str, key: str, value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        msg = f"{where}: {key} must be a string or null, got {value!r}"
        raise ValueError(msg)
    return value


def _parse(raw: dict, *, check_api_keys: bool = True) -> RoutingConfig:
    """Parse raw YAML dict into a validated RoutingConfig.

    Raises ValueError on any malformed entry or field. That is deliberate: the
    overlay fallback in ``load_config`` is what contains a bad overlay, and it
    can only contain an error it sees. Skipping bad entries here would hide
    them from it.
    """
    if not isinstance(raw, dict):
        msg = "Config must be a YAML mapping"
        raise ValueError(msg)
    for section in _OVERLAY_SECTIONS:
        if raw.get(section) is not None and not isinstance(raw[section], dict):
            msg = f"section '{section}' must be a mapping, got {type(raw[section]).__name__}"
            raise ValueError(msg)

    # --- Retry profiles ---
    retry_profiles: dict[str, RetryPolicy] = {}
    for name, rp in (raw.get("retry") or {}).items():
        rp = _entry("retry", name, rp)
        where = f"retry profile '{name}'"
        retry_profiles[name] = RetryPolicy(
            max_retries=_number(where, "max_retries", rp.get("max_retries", 3), integer=True),
            base_delay_ms=_number(where, "base_delay_ms", rp.get("base_delay_ms", 500)),
            max_delay_ms=_number(where, "max_delay_ms", rp.get("max_delay_ms", 30000)),
            backoff_multiplier=_number(
                where, "backoff_multiplier", rp.get("backoff_multiplier", 2.0)
            ),
            jitter_pct=_number(where, "jitter_pct", rp.get("jitter_pct", 0.25)),
            # None = no aggregate cap, an explicit operator choice.
            max_total_s=_number(where, "max_total_s", rp.get("max_total_s"), allow_none=True),
        )
    # Ensure "default" always exists
    if "default" not in retry_profiles:
        retry_profiles["default"] = RetryPolicy()

    # --- Providers ---
    from genesis.observability.snapshots.api_keys import has_api_key

    providers: dict[str, ProviderConfig] = {}
    disabled_providers: set[str] = set()
    disabled_provider_types: dict[str, str] = {}  # name → provider_type
    for name, p in (raw.get("providers") or {}).items():
        p = _entry("providers", name, p)
        # Parse enabled field — supports bool, string from env var expansion
        enabled_raw = p.get("enabled", True)
        if isinstance(enabled_raw, str):
            enabled = enabled_raw.strip().lower() not in {"0", "false", "no", "off", ""}
        else:
            enabled = bool(enabled_raw)

        if not enabled:
            disabled_providers.add(name)
            disabled_provider_types[name] = p.get("type", "unknown")
            logger.info("Provider '%s' disabled via config", name)
            continue

        where = f"provider '{name}'"
        for required in ("type", "model"):
            if not isinstance(p.get(required), str) or not p[required]:
                msg = f"{where}: {required} must be a non-empty string, got {p.get(required)!r}"
                raise ValueError(msg)
        params = p.get("params")
        if params is not None and not isinstance(params, dict):
            msg = f"{where}: params must be a mapping or null, got {params!r}"
            raise ValueError(msg)
        cfg = ProviderConfig(
            name=name,
            provider_type=p["type"],
            model_id=p["model"],
            is_free=_flag(where, "free", p.get("free", False)),
            rpd_limit=_parse_daily_limit(name, "rpd_limit", p.get("rpd_limit")),
            tpd_limit=_parse_daily_limit(name, "tpd_limit", p.get("tpd_limit")),
            # A number, not only an integer: 0.5 means one call per two minutes.
            rpm_limit=_number(where, "rpm_limit", p.get("rpm_limit"), allow_none=True),
            open_duration_s=_number(where, "open_duration_s", p.get("open_duration_s", 120)),
            base_url=_optional_str(where, "base_url", p.get("base_url")),
            keep_alive=p.get("keep_alive"),
            enabled=True,
            profile=_optional_str(where, "profile", p.get("profile")),
            params=params,
        )

        # Keyless providers stay registered with has_api_key=False. The
        # router treats them as down (same code path as a tripped
        # breaker), and the snapshot surfaces them as "disabled" so the
        # dashboard can show "NO API KEY CONFIGURED". Partial API-key
        # configuration is the normal install state, not an error — call
        # sites whose chain depends on keyless providers stay visible so
        # users can see what they need to enable.
        if check_api_keys and not has_api_key(cfg):
            cfg = dataclasses.replace(cfg, has_api_key=False)
            logger.info(
                "Provider '%s' has no API key configured — staying registered as down",
                name,
            )

        providers[name] = cfg

    # Class-fix guard (2026-08): warn loudly if any OpenRouter provider is
    # flagged free:true but points at a paid (non-":free") slug — the
    # openrouter-free billing blind spot. Visibility only; never gates routing.
    for _finding in _detect_mislabeled_free_openrouter(providers):
        logger.warning("Mislabeled free provider — %s", _finding)

    # --- Call sites ---
    call_sites: dict[str, CallSiteConfig] = {}
    for name, cs in (raw.get("call_sites") or {}).items():
        cs = _entry("call_sites", name, cs)
        where = f"call site '{name}'"
        chain = cs.get("chain")
        # Entries are checked against the provider set below, so only the
        # container type is checked here.
        if not isinstance(chain, list):
            msg = f"{where}: chain must be a list of provider names, got {chain!r}"
            raise ValueError(msg)
        # Chains stay intact — keyless providers are NOT filtered. The
        # router skips them at routing time (treats them as down).
        # disabled_providers is still filtered (explicit `enabled: false`
        # in YAML is a deliberate user choice; chains referencing those
        # providers would fail validation otherwise).
        chain = [p for p in chain if p not in disabled_providers]
        dispatch = _normalize_dispatch(cs.get("dispatch"), call_site_name=name)

        if not chain:
            # CLI-dispatch call sites don't use the provider chain — they
            # spawn CC sessions directly.  An empty chain is valid for them.
            if dispatch != "cli":
                logger.warning(
                    "Call site '%s' has empty chain after `enabled: false` filter — dropping",
                    name,
                )
                continue
            logger.info(
                "Call site '%s' has no API providers but dispatch=cli — keeping", name,
            )
        # Validate remaining providers exist
        for provider in chain:
            if provider not in providers:
                msg = f"Call site '{name}' references unknown provider '{provider}'"
                raise ValueError(msg)

        # null clears an inherited profile back to the default, the same way
        # `params: null` clears an inherited params map.
        retry_profile = _optional_str(where, "retry_profile", cs.get("retry_profile")) or "default"
        if retry_profile not in retry_profiles:
            msg = (
                f"Call site '{name}' references unknown "
                f"retry profile '{retry_profile}'"
            )
            raise ValueError(msg)

        call_sites[name] = CallSiteConfig(
            id=name,
            chain=chain,
            default_paid=_flag(where, "default_paid", cs.get("default_paid", False)),
            never_pays=_flag(where, "never_pays", cs.get("never_pays", False)),
            retry_profile=retry_profile,
            dispatch=dispatch,
        )

    return RoutingConfig(
        providers=providers,
        call_sites=call_sites,
        retry_profiles=retry_profiles,
        disabled_providers=disabled_provider_types,
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
    base_raw = yaml.safe_load(_expand_env_vars(path.read_text()))

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

    # Start with existing local overlay for this call site. A file that cannot
    # be read is refused rather than overwritten: writing the new entry over it
    # would discard every override the operator had in it.
    local_path = _local_path_for(path)
    try:
        local_raw = _load_local_overlay(path)
    except Exception as e:
        msg = f"Local overlay {local_path.name} could not be read; fix or remove it: {e}"
        raise ValueError(msg) from e
    if not isinstance(local_raw, dict):
        msg = f"Local overlay {local_path.name} is not a YAML mapping; fix or remove it"
        raise ValueError(msg)
    # A bare `call_sites:` or `<id>:` holds nothing, so the edit may replace it
    # with the mapping it needs. Any other non-mapping holds something the
    # operator typed, and is not overwritten.
    if local_raw.get("call_sites") is None:
        local_raw["call_sites"] = {}
    if not isinstance(local_raw["call_sites"], dict):
        msg = f"Local overlay {local_path.name}: call_sites is not a mapping; fix it first"
        raise ValueError(msg)
    if local_raw["call_sites"].get(call_site_id) is None:
        local_raw["call_sites"][call_site_id] = {}
    local_cs = local_raw["call_sites"][call_site_id]
    if not isinstance(local_cs, dict):
        msg = (
            f"Local overlay {local_path.name}: call site '{call_site_id}' is not a "
            "mapping; fix it first"
        )
        raise ValueError(msg)

    # Resolve effective call site (base + existing local) for validation
    base_cs = base_raw["call_sites"][call_site_id]
    effective_cs = _deep_merge(base_cs, local_cs)

    # The dispatch this update will leave in effect. Mirrors the loader's
    # empty-chain exemption (_parse: an empty chain is valid ONLY for cli
    # sites, which spawn CC directly). Without it the first empty-chain
    # cli site (ambient_arbiter) is un-editable — the dashboard editor
    # round-trips the chain, and [] was rejected unconditionally here.
    intended_dispatch = _normalize_dispatch(
        dispatch if dispatch is not None else effective_cs.get("dispatch"),
        call_site_name=call_site_id,
    )

    if chain is not None:
        if not chain and intended_dispatch != "cli":
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

    # CC dispatch metadata (stored in YAML, read by dashboard). Capitalized to
    # match the routing-registry convention; derived from CCModel so a new tier
    # (e.g. Fable) is accepted here automatically.
    _VALID_CC_MODELS = {m.value.capitalize() for m in CCModel}
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

    # Validate: never_pays sites must have at least one free provider.
    # Vacuous for an empty-chain cli site (nothing to pay for — the same
    # class the loader exempts), so skip it there or the site can never
    # revalidate through this path.
    effective_chain = effective_cs.get("chain", base_cs.get("chain", []))
    if effective_cs.get("never_pays") and not (
        intended_dispatch == "cli" and not effective_chain
    ):
        free_in_chain = [p for p in effective_chain if providers.get(p, {}).get("free")]
        if not free_in_chain:
            msg = f"never_pays site '{call_site_id}' must have at least one free provider"
            raise ValueError(msg)

    # Validate EXACTLY what will be persisted: the serialized text, read back
    # and run through the same merge and parse `load_config` uses at boot. If
    # this passes, the next load passes; if it fails, nothing is written. An
    # overlay the loader would reject must never be saved, because the loader
    # would then set the whole file aside at the next restart.
    new_text = yaml.dump(local_raw, default_flow_style=False, sort_keys=False)
    try:
        written = yaml.safe_load(new_text)
        new_config = _parse(_merge_overlay(base_raw, written if written is not None else {}))
    except Exception as e:
        msg = f"Generated config failed validation: {e}"
        raise ValueError(msg) from e

    # Atomic write to local overlay: .new → rotate backups → rename
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
