"""Config loader for model routing — YAML → RoutingConfig, and save back."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import logging
import math
import os
import re
import shutil
import time
from pathlib import Path
from typing import NamedTuple

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
    the caller's job: the loader refuses the load (nothing in the file can be
    read, so no restriction could be kept), and the save path refuses to write
    over a file it could not read.
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


#: Providers renamed upstream, mapped legacy -> current.
#:
#: A rename is a BREAKING UPGRADE for any install carrying a local overlay, because
#: the overlay is the one reference set the renaming diff cannot see. Without this
#: map a partial provider override deep-merges onto nothing and `_parse` skips the
#: resulting entry, so the operator's deliberate override silently disappears;
#: a chain pinned to a legacy name is filtered out as "unknown" and the shipped
#: chain runs in its place.
#:
#: ADD AN ENTRY HERE IN THE SAME PR THAT RENAMES A PROVIDER. Entries stay
#: indefinitely: an install can upgrade from any older version, so removing one
#: re-opens the hole for exactly the installs that upgrade least often.
_RENAMED_PROVIDERS: dict[str, str] = {
    # 2026-09: names that encoded a model version.
    "glm51": "glm",
    "kimi-k2.5": "kimi",
    "minimax-m25": "minimax",
    "openrouter-deepseek-v4-flash": "openrouter-deepseek-flash",
    "openrouter-gpt55": "openrouter-gpt-max",
    "gpt-5.4": "openrouter-gpt",
    "openai-gpt5": "openai-gpt",
}


def _rename_depth(name: str) -> int:
    """How many hops from *name* to its current name. 0 if it is current.

    The ORDER the migration walks the map is decided by this, and it is not
    cosmetic — see `_migrate_renamed_providers`.
    """
    seen: set[str] = set()
    hops = 0
    while name in _RENAMED_PROVIDERS and name not in seen:
        seen.add(name)
        name = _RENAMED_PROVIDERS[name]
        hops += 1
    return hops


def _current_provider_name(name: str) -> str:
    """Resolve a provider name through CHAINED renames, with a cycle guard.

    The map is append-only by contract, so a second rename of an already-renamed
    provider leaves two hops (`glm51 -> glm`, `glm -> glm-v2`). A single `.get()`
    would stop at `glm`, which no longer exists in base — and the override would
    then be dropped as stale, reproducing the original bug one upgrade later.
    Resolving transitively is what makes "entries stay indefinitely" actually safe.
    """
    seen: set[str] = set()
    while name in _RENAMED_PROVIDERS and name not in seen:
        seen.add(name)
        name = _RENAMED_PROVIDERS[name]
    return name


def _resolve_provider_alias(name: str, known: set[str] | dict) -> str:
    """The name to USE for ``name`` against a provider set, aliasing only when it helps.

    ONE rule, and every boundary that accepts a provider name from outside the
    config needs it:

      * ``name`` present in ``known`` -> return it unchanged. An exact key is a
        live provider, never a retired one, whatever the alias map says. A
        non-shipped config is free to define a key the shipped one renamed.
      * otherwise, if the resolved current name is present -> return that. This
        is the upgrade path the alias map exists for.
      * otherwise -> return ``name`` unchanged, and let the caller's own
        unknown-provider handling report it. Substituting a name that is ALSO
        absent just changes which name appears in the error.

    Applying the alias unconditionally is what two reviewers found independently:
    it renames a working override onto a key the base does not have, the entry is
    then dropped as unknown, and the operator's customization is silently
    replaced by the base default with only a warning to show for it.
    """
    if name in known:
        return name
    current = _current_provider_name(name)
    return current if current in known else name


def _migrate_renamed_providers(result: dict, base_providers: set[str]) -> None:
    """Rewrite legacy provider keys in an overlay's ``providers`` section, in place.

    Collision rule, stated rather than left to merge order: if the overlay carries
    BOTH a legacy key and its replacement, the replacement is the operator's
    current intent — it wins, and the legacy entry is dropped with a warning.

    THE WALK ORDER IS WHAT MAKES THAT RULE TRUE, and getting it wrong inverts the
    rule silently. Once a provider has been renamed TWICE (`A -> B -> C`), an
    overlay can hold both `A` and `B`, and both resolve to `C`. Walking the map in
    insertion order migrates whichever happens to come first, and the collision
    check then drops the other — so with `A` first, the OLDEST generation wins and
    the newer override is discarded. MEASURED before this ordering existed: an
    overlay with `A.rpm_limit = 1` and `B.rpm_limit = 99` resolved to 1.

    Walking SHALLOWEST-FIRST fixes it by construction: `B` is one hop from `C` and
    `A` is two, so `B` migrates first and `A` then collides with it and is dropped
    as the older generation. No entry in the shipped map is more than one hop
    today, so this changes nothing now — it is the guarantee that the rule still
    holds the first time a provider is renamed twice, which is exactly when nobody
    will be looking.
    """
    local_providers = result.get("providers")
    if not isinstance(local_providers, dict):
        return

    for legacy in sorted(_RENAMED_PROVIDERS, key=_rename_depth):
        if legacy not in local_providers:
            continue
        # THE ALIAS ONLY APPLIES TO A KEY THE BASE HAS ACTUALLY RETIRED.
        # `load_config` accepts a caller-supplied path, so the base need not be
        # the shipped file — and a base that still DEFINES this key means the
        # operator's override is for a live provider, not a stale one. Migrating
        # it then renames a working override onto a key the base does not have,
        # and the check below drops it: the load succeeds, a warning is logged,
        # and the customization is silently replaced by the base default.
        #
        # MEASURED before this guard (base defines `glm51`, overlay sets
        # `glm51.rpm_limit: 99`): loaded with rpm_limit 5, the base value. The
        # override was gone. Same shape at the chain boundary, where an overlay
        # rung was dropped from the chain entirely — worse, because that changes
        # what gets routed rather than one limit.
        if legacy in base_providers:
            continue
        current = _current_provider_name(legacy)
        entry = local_providers.pop(legacy)
        # A half-edited overlay leaves `glm51:` with no body (None) or a scalar.
        # Migrating that onto the live key would DESTROY the base provider's
        # entry — strictly worse than the stale key it replaces — so drop it.
        if not isinstance(entry, dict):
            logger.warning(
                "Local overlay provider '%s' is not a mapping (%s) — dropping it "
                "rather than migrating an unusable value onto '%s'",
                legacy, type(entry).__name__, current,
            )
            continue
        if current in local_providers:
            logger.warning(
                "Local overlay has both '%s' and its replacement '%s' — keeping "
                "'%s' and dropping the legacy entry",
                legacy, current, current,
            )
            continue
        if current not in base_providers:
            logger.warning(
                "Local overlay provider '%s' migrates to '%s', which the base "
                "config does not define — dropping it",
                legacy, current,
            )
            continue
        local_providers[current] = entry
        logger.info(
            "Migrated local overlay provider '%s' -> '%s' (renamed upstream)",
            legacy, current,
        )


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

    # Before anything reads the overlay's names, bring them up to date. Doing this
    # FIRST is what lets the stale-reference filter below stay a pure filter.
    _migrate_renamed_providers(result, base_providers)

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
        if not isinstance(cs["chain"], list):
            # A string would otherwise be iterated character by character and
            # every character filtered out as an unknown provider.
            msg = (
                f"call site '{cs_name}': chain must be a list, got "
                f"{type(cs['chain']).__name__}"
            )
            raise ValueError(msg)
        # Translate renamed providers BEFORE the stale-reference filter, or a
        # legitimately-pinned legacy name is dropped as "unknown" and the
        # operator's deliberate pin is silently replaced by the shipped chain.
        #
        # `dict.fromkeys` rather than a plain comprehension: a chain naming BOTH a
        # legacy name and its replacement (`[glm51, glm]`) would otherwise
        # translate to `['glm', 'glm']` — a duplicate the WRITE path explicitly
        # rejects ("Chain must not contain duplicate providers"), and which would
        # make failover retry one provider twice against the same breaker and
        # daily-budget ledger. Pre-fix the legacy entry was filtered as stale, so
        # this duplicate is only reachable once translation exists.
        # Same rule as the provider keys, via the shared resolver: a rung the
        # base still defines is left alone. Translating it unconditionally sent
        # a live rung to a name the base lacked, and the stale filter two lines
        # down then removed it from the chain — changing what gets ROUTED, not
        # just a limit. MEASURED: an overlay chain `[glm51, groq-free]` against
        # a base that defines `glm51` loaded as `['groq-free']`.
        original_chain = list(
            dict.fromkeys(_resolve_provider_alias(p, base_providers) for p in cs["chain"])
        )
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

    If the overlay cannot be read, merged, or parsed, its overrides are set
    aside and the base config is loaded instead, with every RESTRICTION the
    overlay makes still applied (see ``_restrict_base`` and ``_FALLBACK_RULES``):
    a typo must not lift a limit, a provider exclusion, a dispatch mode or a
    retry cap the operator set. The failure is logged at ERROR and recorded as a
    health observation. Without this, most malformed overlays raised out of
    here, and ``runtime/init/router.py`` catches that and leaves the runtime
    with no router, so every LLM call site went dark behind one log line. The
    base is validated in CI (``test_config_invariants``), so it is the safe
    thing to fall back to. A base that fails to parse still raises.

    One overlay failure is REFUSED rather than contained: a file that cannot be
    read or parsed as YAML at all. None of its restrictions can be read, so the
    base would load with every one of them lifted (disabled providers,
    ``never_pays``, narrowed chains, lower limits). That raises here, after the
    same ERROR log and health observation, so routing stays down until the file
    is fixed or removed. An overlay that parses but fails validation still falls
    back, with its readable restrictions applied.

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
    local_raw: object = _UNREADABLE
    try:
        local_raw = _load_local_overlay(path)
        # Always merged, even when falsy: `[]`, `false`, `0` and `''` are
        # malformed overlays, not absent ones. An absent or empty file is `{}`.
        merged = _merge_overlay(base_raw, local_raw)
        return _parse(merged, check_api_keys=check_api_keys), merged
    except Exception as exc:
        if strict_overlay:
            raise
        if local_raw is _UNREADABLE:
            # Nothing in the file can be read, so there is no restriction to
            # keep, and the base alone would lift all of them. Refuse the load.
            _report_overlay_rejected(local_path, exc, [_OVERLAY_REFUSED])
            raise
        try:
            fallback, kept = _restrict_base(base_raw, local_raw)
        except Exception:  # noqa: BLE001 - a defect here must not take routing dark
            logger.error(
                "Could not apply the rejected overlay's restrictions; using the base as is",
                exc_info=True,
            )
            fallback = copy.deepcopy(base_raw)
            kept = [_EXCLUSIONS_FAILED]
        config = _parse(fallback, check_api_keys=check_api_keys)
        _report_overlay_rejected(local_path, exc, kept)
        return config, fallback


#: Marks an overlay whose text could not be read or parsed as YAML at all.
_UNREADABLE = object()

#: The kept-restrictions entry when applying them failed.
_EXCLUSIONS_FAILED = "__exclusions_failed__"

#: The kept-restrictions entry for an overlay that could not be read at all, so
#: the load was refused and nothing was loaded in its place.
_OVERLAY_REFUSED = "__overlay_refused__"

#: Strings the `enabled` field reads as off. Shared by `_parse` and the fallback
#: so both read an overlay's `enabled` the same way.
_OFF_STRINGS = frozenset({"0", "false", "no", "off", ""})


def _is_enabled(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in _OFF_STRINGS
    return bool(value)


#: Per-provider limits that bound how much a provider is used. On the fallback
#: path the lower of the base's and the overlay's value applies.
_LIMIT_KEYS = ("rpd_limit", "tpd_limit", "rpm_limit")


def _limit_value(provider: str, key: str, value: object) -> float | None:
    """A limit as `_parse` reads it, or None when `_parse` would refuse it.

    Runs the accepted path's own validators rather than a copy of them, so the
    two paths cannot disagree about which values are valid. The result is a
    number to compare, where "no limit" is infinity: `rpm_limit: 0` turns the
    rate gate off (``router.py`` gates only on ``rpm_limit > 0``).
    """
    try:
        if key == "rpm_limit":
            parsed = _number(provider, key, value, allow_none=True)
            return math.inf if parsed is None or parsed == 0 else parsed
        parsed = _parse_daily_limit(provider, key, value)
        return math.inf if parsed is None else parsed
    except Exception:  # noqa: BLE001 - any refusal, including OverflowError
        return None


#: A retry profile's CAPS: the fields that bound how many executions a call
#: makes (`max_retries`) and for how long (`max_total_s`), with the value
#: `_parse` uses when the field is absent. None means no cap.
_RETRY_CAPS: dict[str, object] = {"max_retries": 3, "max_total_s": None}

#: A retry profile's PACING fields, with the value `_parse` uses when absent.
_PACING_DEFAULTS: dict[str, float] = {
    "base_delay_ms": 500,
    "max_delay_ms": 30000,
    "backoff_multiplier": 2.0,
    "jitter_pct": 0.25,
}

#: `open_duration_s` when a provider does not set it. One constant, so the
#: fallback compares against the value `_parse` actually uses.
_OPEN_DURATION_DEFAULT_S = 120

#: How an unreadable cap is read: the tightest value that still ROUTES.
#: `max_retries` 0 is one attempt per provider. `max_total_s` has no such
#: value: the router checks the deadline before the FIRST provider too
#: (`router.py`, the chain walk), so 0 means no call at all, which is the
#: outage this fallback exists to prevent. An unreadable deadline therefore
#: narrows nothing (infinity), and the base's deadline stands.
_UNREADABLE_CAP: dict[str, float] = {"max_retries": 0, "max_total_s": math.inf}


def _cap_value(key: str, value: object) -> float:
    """A retry cap as `_parse` reads it, as a number to compare. No cap is
    infinity. A value `_parse` would refuse reads as `_UNREADABLE_CAP`."""
    try:
        parsed = _number(
            "retry", key, value, integer=key == "max_retries", allow_none=key == "max_total_s"
        )
    except Exception:  # noqa: BLE001 - any refusal
        return _UNREADABLE_CAP[key]
    return math.inf if parsed is None else parsed


def _profile_caps(profile: object) -> dict[str, float]:
    """The caps a raw retry profile sets. An unreadable profile reads as
    `_UNREADABLE_CAP`."""
    if not isinstance(profile, dict):
        return dict(_UNREADABLE_CAP)
    return {key: _cap_value(key, profile.get(key, default)) for key, default in _RETRY_CAPS.items()}


#: The executors each dispatch mode permits. `api` and `cli` each permit a
#: subset of `dual`, and share nothing with each other.
_DISPATCH_EXECUTORS: dict[str, frozenset[str]] = {
    "dual": frozenset({"api", "cli"}),
    "api": frozenset({"api"}),
    "cli": frozenset({"cli"}),
}


def _dispatch_mode(raw: object) -> str:
    """`_normalize_dispatch` without its warning: the fallback reads the value,
    it does not run on it. An unrecognised value is `dual`, as on the accepted
    path, so it narrows nothing."""
    if isinstance(raw, str):
        value = raw.strip().lower()
        value = "cli" if value == "cc" else value
        if value in _DISPATCH_EXECUTORS:
            return value
    return "dual"


class _Rule(NamedTuple):
    """How a rejected overlay's value for one field still restricts the base.

    ``kind`` names the operation, each of which can only NARROW the base:

      * ``off_wins``    a switch: the value that permits less wins.
      * ``on_wins``     the same, for a switch whose ON value restricts.
      * ``lower``       a limit or cap: the lower value wins.
      * ``higher``      a wait: the longer value wins. For a provider, fewer
        probes of it (the rest of its chain carries the load meanwhile, never
        for longer than the overlay itself asked); for retry pacing, fewer
        attempts before the deadline.
      * ``same_or_excluded``  a value with no order (which model, which
        endpoint). The only restrictive reading of two different values is
        neither, so an overlay that changes it excludes the provider.
      * ``chain``       the providers in both chains, in the overlay's order.
      * ``dispatch``    the executors both modes permit; with none in common the
        site is BLOCKED (kept as API-only with no providers, so nothing runs).
      * ``retry_profile``  the tighter of the two profiles' caps.
      * ``neutral``     bounds nothing the router does; the base value is used.
        ``why`` must say why.
    """

    kind: str
    why: str


#: EVERY field `_parse` reads from an overlay-settable entry, plus the fields
#: the dashboard writes, with how a rejected overlay's value for it still
#: applies. `test_every_overlay_settable_field_has_a_restrictiveness_rule` walks
#: `_parse` and the shipped config and fails on a field missing here, so a field
#: added later cannot be silently dropped by the fallback.
_FALLBACK_RULES: dict[str, dict[str, _Rule]] = {
    "providers": {
        "enabled": _Rule("off_wins", "a disabled provider is never called"),
        "free": _Rule("off_wins", "a never_pays site skips a provider that is not free"),
        "rpd_limit": _Rule("lower", "requests per day"),
        "tpd_limit": _Rule("lower", "tokens per day"),
        "rpm_limit": _Rule("lower", "requests per minute; 0 is no limit"),
        "type": _Rule("same_or_excluded", "which API is called"),
        "model": _Rule("same_or_excluded", "which model is called, and its price"),
        "base_url": _Rule("same_or_excluded", "where prompts are sent"),
        "profile": _Rule("same_or_excluded", "the pricing and capability profile"),
        "params": _Rule("same_or_excluded", "request parameters, including fallback models"),
        "open_duration_s": _Rule(
            "higher", "how long a tripped provider rests before it is probed again"
        ),
        "keep_alive": _Rule(
            "neutral", "how long a local model stays loaded: changes neither the call nor its cost"
        ),
    },
    "call_sites": {
        "chain": _Rule("chain", "which providers the site may call, and in what order"),
        "dispatch": _Rule("dispatch", "whether the site may use the API chain, the CLI, or both"),
        "never_pays": _Rule("on_wins", "keeps the site on free providers"),
        "retry_profile": _Rule("retry_profile", "how many attempts and how long"),
        "default_paid": _Rule("neutral", "not read by the router; the dashboard displays it"),
        "cc_model": _Rule("neutral", "display only; the router does not read it"),
        "cc_position": _Rule("neutral", "display only; the router does not read it"),
        "description": _Rule("neutral", "documentation"),
    },
    "retry": {
        "max_retries": _Rule("lower", "attempts per provider"),
        "max_total_s": _Rule("lower", "the deadline for the whole chain walk; null is none"),
        # PACING is not neutral. The router checks `max_total_s` before each
        # retry, so a longer wait between attempts means FEWER attempts fit
        # before the deadline: `max_retries` is only the ceiling.
        "base_delay_ms": _Rule("higher", "a longer first wait fits fewer attempts in the deadline"),
        "max_delay_ms": _Rule("higher", "a longer ceiling on each wait fits fewer attempts"),
        "backoff_multiplier": _Rule("higher", "faster-growing waits fit fewer attempts"),
        "jitter_pct": _Rule("lower", "less jitter keeps the shortest wait closer to the backoff"),
    },
}

#: Every operation a rule may name. The coverage test checks the table against it.
_RULE_KINDS = frozenset(
    {
        "off_wins",
        "on_wins",
        "lower",
        "higher",
        "same_or_excluded",
        "chain",
        "dispatch",
        "retry_profile",
        "neutral",
    }
)

#: How `off_wins` / `on_wins` read a value, matching `_parse`: `enabled` through
#: `_is_enabled`, the other switches through `_flag`, where only `True` is on
#: and anything else it would refuse reads restrictively.
_PERMITS: dict[str, object] = {
    "enabled": _is_enabled,
    "free": lambda value: value is True,
}
_SWITCH_DEFAULTS = {"enabled": True, "free": False}
_SWITCH_LABELS = {"enabled": "disabled", "free": "not free"}


def _restrict_base(base_raw: dict, local_raw: object) -> tuple[dict, list[str]]:
    """The base config, with everything the rejected overlay restricts still applied.

    Setting the whole overlay aside must not undo what the operator restricted.
    Falling back to the base alone would lift all of it, so a typo anywhere in
    the file would route to providers, executors and spend the operator had
    ruled out.

    Every field the overlay can set has a rule in ``_FALLBACK_RULES``, and each
    rule can only NARROW the base: the result permits no call, no provider, no
    executor, no attempt and no spend that either the base or the overlay would
    refuse. See ``_Rule`` for the operations. A field with no order (which model,
    which endpoint) that the overlay changes excludes its provider, because the
    base's value is not the one the operator runs.

    Where the overlay names a provider, call site or retry profile but the value
    there cannot be read, the reading is the restrictive one: an unreadable
    provider entry or limit disables the provider, an unreadable call-site
    entry blocks the site, an unreadable chain leaves the site no providers,
    and an unreadable `max_retries` is 0 (see ``_UNREADABLE_CAP`` for why an
    unreadable `max_total_s` narrows nothing). A null ENTRY holds nothing and restricts
    nothing; a null FIELD is read the way the accepted path reads it.

    Provider names go through the same rename resolution as the accepted path,
    so an overlay that restricts a provider under its old name still restricts it.

    A file that is not YAML never reaches here: ``_load_effective`` refuses it.
    An overlay that parses but in which no name can be read (not a mapping, a
    section that is not a mapping) has nothing to apply, and the base is used as
    it is. The ERROR log and the health observation say so.

    Returns the restricted copy and a description of each restriction kept.
    """
    fallback = copy.deepcopy(base_raw)
    kept: list[str] = []
    if not isinstance(local_raw, dict):
        return fallback, kept

    # A base with no `retry` section, or `retry: null`, still has a `default`
    # profile: `_parse` supplies one. Materialise both so a restriction on
    # them has somewhere to land. A section of another type makes `_parse`
    # raise on the base itself, so it is left alone.
    if fallback.get("retry") is None:
        fallback["retry"] = {}
    base_retry = fallback["retry"] if isinstance(fallback["retry"], dict) else {}
    if base_retry is fallback["retry"] and "default" not in base_retry:
        base_retry["default"] = {}
    local_retry = local_raw.get("retry")
    local_retry = local_retry if isinstance(local_retry, dict) else {}
    _restrict_retry(base_retry, local_retry, kept)

    base_providers = fallback.get("providers")
    local_providers = local_raw.get("providers")
    known = set(base_providers) if isinstance(base_providers, dict) else set()
    if isinstance(base_providers, dict) and isinstance(local_providers, dict):
        # The accepted path's own rename chokepoint, on a copy: a legacy key and
        # its replacement are settled exactly as `_sanitize_local_overlay`
        # settles them (the replacement wins), not by applying both.
        migrated = {"providers": copy.deepcopy(local_providers)}
        _migrate_renamed_providers(migrated, known)
        _restrict_providers(base_providers, migrated["providers"], kept)

    base_sites = fallback.get("call_sites")
    local_sites = local_raw.get("call_sites")
    if isinstance(base_sites, dict) and isinstance(local_sites, dict):
        _restrict_call_sites(base_sites, local_sites, known, fallback, base_raw, local_retry, kept)
    return fallback, kept


def _restrict_retry(base_retry: dict, local_retry: dict, kept: list[str]) -> None:
    for name, entry in local_retry.items():
        base_entry = base_retry.get(name)
        # A profile the base does not define is reachable only through a call
        # site's retry_profile, where `_restrict_call_sites` reads it.
        if not isinstance(base_entry, dict) or entry is None:
            continue
        if not isinstance(entry, dict):
            entry = dict.fromkeys(_RETRY_CAPS, "unreadable")
        # The overlay entry merges onto the base profile, so a field it omits
        # keeps the base's value: only the fields it sets are compared.
        changes, unreadable = _tighten_pacing(base_entry, entry, absent_is_default=False)
        kept.extend(f"retry {name} {change}" for change in changes)
        if unreadable:
            entry = {**entry, "max_retries": "unreadable"}
        for key, value in entry.items():
            rule = _FALLBACK_RULES["retry"].get(key)
            if rule is None or rule.kind == "neutral" or key in _PACING_DEFAULTS:
                continue
            limit = _cap_value(key, value)
            base_limit = _cap_value(key, base_entry.get(key, _RETRY_CAPS[key]))
            if limit < base_limit:
                # The parsed number, never the raw value: an unreadable value
                # reads as `_UNREADABLE_CAP`, and writing it back would make
                # `_parse` refuse the fallback itself. Below the base, so finite.
                base_entry[key] = limit
                kept.append(f"retry {name} {key} {base_entry[key]}")


def _tighten_pacing(target: dict, overlay: dict, *, absent_is_default: bool) -> tuple[list[str], bool]:
    """Apply the overlay's retry pacing to ``target`` where it waits longer.

    Longer waits (``higher``) and less jitter (``lower``) win, per
    ``_FALLBACK_RULES``. With ``absent_is_default`` a field the overlay omits
    reads as `_parse`'s default (a whole profile); without it, an omitted field
    compares nothing (a partial entry that merges onto ``target``).

    Returns a description of each change made, and whether any overlay pacing
    value was unreadable. The caller reads an unreadable wait restrictively, as
    no retries, the same as an unreadable cap. A huge kept multiplier is safe:
    ``compute_delay`` caps an overflowing backoff at ``max_delay_ms``.
    """
    changes: list[str] = []
    unreadable = False
    for key, default in _PACING_DEFAULTS.items():
        if key not in overlay and not absent_is_default:
            continue
        try:
            wanted = _number("retry", key, overlay.get(key, default))
        except Exception:  # noqa: BLE001 - any refusal
            unreadable = True
            continue
        try:
            current = _number("retry", key, target.get(key, default))
        except Exception:  # noqa: BLE001 - `_parse` raises on the base itself
            continue
        kind = _FALLBACK_RULES["retry"][key].kind
        if (kind == "higher" and wanted > current) or (kind == "lower" and wanted < current):
            target[key] = wanted
            changes.append(f"{key} {wanted}")
    return changes, unreadable


def _restrict_providers(base_providers: dict, local_providers: dict, kept: list[str]) -> None:
    """``local_providers`` has already been through `_migrate_renamed_providers`,
    so its names are current and a legacy/current collision is already settled
    the way the accepted path settles it."""
    for name, entry in local_providers.items():
        base_entry = base_providers.get(name)
        if not isinstance(base_entry, dict) or entry is None:
            continue
        if not isinstance(entry, dict):
            base_entry["enabled"] = False
            kept.append(f"provider {name} disabled (unreadable entry)")
            continue
        for key, value in entry.items():
            rule = _FALLBACK_RULES["providers"].get(key)
            if rule is None or rule.kind == "neutral":
                continue
            if rule.kind == "off_wins":
                permits = _PERMITS[key]
                default = _SWITCH_DEFAULTS[key]
                if not permits(value) and permits(base_entry.get(key, default)):
                    base_entry[key] = False
                    kept.append(f"provider {name} {_SWITCH_LABELS[key]}")
            elif rule.kind == "lower":
                if value is None:
                    continue
                limit = _limit_value(name, key, value)
                if limit is None:
                    base_entry["enabled"] = False
                    kept.append(f"provider {name} disabled (unreadable {key})")
                    continue
                # An unreadable BASE value is left alone: `_parse` raises on it,
                # as it would with no overlay at all.
                base_limit = _limit_value(name, key, base_entry.get(key))
                if base_limit is not None and limit < base_limit:
                    base_entry[key] = value
                    kept.append(f"provider {name} {key} {value}")
            elif rule.kind == "higher":
                # Only `open_duration_s`, with the default `_parse` supplies.
                try:
                    wait = _number(name, key, value)
                except Exception:  # noqa: BLE001 - any refusal
                    base_entry["enabled"] = False
                    kept.append(f"provider {name} disabled (unreadable {key})")
                    continue
                try:
                    base_wait = _number(name, key, base_entry.get(key, _OPEN_DURATION_DEFAULT_S))
                except Exception:  # noqa: BLE001 - `_parse` raises on the base itself
                    continue
                if wait > base_wait:
                    base_entry[key] = wait
                    kept.append(f"provider {name} {key} {wait}")
            elif rule.kind == "same_or_excluded":
                current = base_entry.get(key)
                # The value the accepted path would have run: mappings merge.
                effective = (
                    _deep_merge(current, value)
                    if isinstance(current, dict) and isinstance(value, dict)
                    else value
                )
                if effective != current and _is_enabled(base_entry.get("enabled", True)):
                    base_entry["enabled"] = False
                    kept.append(f"provider {name} disabled (the overlay changes its {key})")
            else:  # pragma: no cover - the coverage test pins the kinds per section
                msg = f"no provider handling for rule kind {rule.kind!r}"
                raise AssertionError(msg)


def _restrict_call_sites(
    base_sites: dict,
    local_sites: dict,
    known: set[str],
    fallback: dict,
    base_raw: dict,
    local_retry: dict,
    kept: list[str],
) -> None:
    excluded: list[str] = []
    for name, entry in local_sites.items():
        base_entry = base_sites.get(name)
        if not isinstance(base_entry, dict) or entry is None:
            continue
        if not isinstance(entry, dict):
            excluded.append(name)
            kept.append(f"call site {name} blocked (unreadable entry)")
            continue
        for key, value in entry.items():
            rule = _FALLBACK_RULES["call_sites"].get(key)
            if rule is None or rule.kind == "neutral":
                continue
            if rule.kind == "on_wins":
                # `_flag` reads null as false, and refuses anything but a bool.
                if value is not None and value is not False and base_entry.get(key) is not True:
                    base_entry[key] = True
                    kept.append(f"call site {name} {key}")
            elif rule.kind == "chain":
                _restrict_chain(name, base_entry, value, known, kept)
            elif rule.kind == "dispatch":
                base_mode = _dispatch_mode(base_entry.get("dispatch"))
                allowed = (
                    _DISPATCH_EXECUTORS[base_mode] & _DISPATCH_EXECUTORS[_dispatch_mode(value)]
                )
                if not allowed:
                    excluded.append(name)
                    kept.append(f"call site {name} blocked (dispatch {value!r} vs {base_mode})")
                elif allowed != _DISPATCH_EXECUTORS[base_mode]:
                    mode = next(m for m, ex in _DISPATCH_EXECUTORS.items() if ex == allowed)
                    base_entry["dispatch"] = mode
                    kept.append(f"call site {name} dispatch {mode}")
            elif rule.kind == "retry_profile":
                _restrict_site_retry(name, base_entry, value, fallback, base_raw, local_retry, kept)
            else:  # pragma: no cover - the coverage test pins the kinds per section
                msg = f"no call-site handling for rule kind {rule.kind!r}"
                raise AssertionError(msg)
    # BLOCKED, not removed. The autonomous dispatcher reads a call site missing
    # from the config as `dual`, which can escalate to the CLI; an API-only
    # site with no providers is refused there instead ("dispatch=api: API chain
    # exhausted"), and `route_call` finds no provider to call.
    for name in dict.fromkeys(excluded):
        base_sites[name]["chain"] = []
        base_sites[name]["dispatch"] = "api"


def _restrict_chain(
    name: str, base_entry: dict, chain: object, known: set[str], kept: list[str]
) -> None:
    if chain is None:
        return
    base_chain = base_entry.get("chain")
    base_chain = base_chain if isinstance(base_chain, list) else []
    if isinstance(chain, list):
        # Mirrors `_sanitize_local_overlay` on the accepted path: renamed names
        # are translated, names the base does not define are dropped, and a
        # chain left with none is no override at all.
        named = [_resolve_provider_alias(p, known) for p in chain if isinstance(p, str)]
        named = [p for p in named if p in known]
        if not named:
            return
        in_base = set(base_chain)
        narrowed = [p for p in dict.fromkeys(named) if p in in_base]
    else:
        narrowed = []
    if narrowed != base_chain:
        base_entry["chain"] = narrowed
        kept.append(f"call site {name} chain {narrowed}")


def _restrict_site_retry(
    name: str,
    base_entry: dict,
    value: object,
    fallback: dict,
    base_raw: dict,
    local_retry: dict,
    kept: list[str],
) -> None:
    """Give the site the tighter caps of its base profile and the overlay's.

    The overlay may point the site at another profile, possibly one it defines
    or edits itself. The fallback keeps the site on its base profile's settings
    but caps it at the lower `max_retries` and `max_total_s` of the two, in a
    profile made for the site. An unreadable name or profile reads as
    ``_UNREADABLE_CAP``.
    """
    retry = fallback.setdefault("retry", {})
    if not isinstance(retry, dict):
        return
    base_profile_name = base_entry.get("retry_profile") or "default"
    base_profile = retry.get(base_profile_name)
    base_profile = base_profile if isinstance(base_profile, dict) else {}

    if value is None:
        wanted: object = "default"
    else:
        wanted = value if isinstance(value, str) and value else None
    if wanted is None:
        overlay_profile: object = None  # unreadable name
    else:
        shipped = (
            (base_raw.get("retry") or {}).get(wanted)
            if isinstance(base_raw.get("retry"), dict)
            else None
        )
        local = local_retry.get(wanted)
        if wanted not in local_retry:
            overlay_profile = (
                shipped if isinstance(shipped, dict) else ({} if wanted == "default" else None)
            )
        elif isinstance(local, dict):
            overlay_profile = _deep_merge(shipped, local) if isinstance(shipped, dict) else local
        elif local is None and isinstance(shipped, dict):
            overlay_profile = shipped
        else:
            overlay_profile = None

    base_caps = _profile_caps(base_profile)
    overlay_caps = _profile_caps(overlay_profile)
    tighter = {key: min(base_caps[key], overlay_caps[key]) for key in _RETRY_CAPS}
    site_profile = copy.deepcopy(base_profile)
    # The overlay's profile is a whole profile here, so a field it omits is
    # `_parse`'s default for it.
    pacing: list[str] = []
    if isinstance(overlay_profile, dict):
        pacing, unreadable = _tighten_pacing(site_profile, overlay_profile, absent_is_default=True)
        if unreadable:
            tighter["max_retries"] = _UNREADABLE_CAP["max_retries"]
    if tighter == base_caps and not pacing:
        return
    for key, cap in tighter.items():
        site_profile[key] = None if cap == math.inf else cap
    site_name = f"fallback:{name}"
    retry[site_name] = site_profile
    base_entry["retry_profile"] = site_name
    kept.append(
        f"call site {name} retry max_retries {site_profile['max_retries']} "
        f"max_total_s {site_profile['max_total_s']}"
        + "".join(f" {change}" for change in pacing)
    )


#: Last-LOGGED (mtime, cause, restrictions kept) per overlay path.
#: ``load_config`` runs on every dashboard vitals read and in every
#: eval/standalone process, so a broken overlay is logged once per file
#: version, cause AND kept restrictions: an edit, or a base change underneath it
#: that changes the error or what the fallback kept, reports again.
_REPORTED_OVERLAYS: dict[str, tuple[float, str, tuple[str, ...]]] = {}

#: The report whose health observation is RECORDED, per overlay path: written,
#: or already present as an unresolved row. Kept apart from the log dedup so a
#: write that failed (a locked or missing database) is retried, not forgotten.
_RECORDED_OVERLAYS: dict[str, tuple[float, str, tuple[str, ...]]] = {}

#: Monotonic time of the last FAILED observation write, per (path, report).
_OBSERVATION_FAILED_AT: dict[tuple[str, tuple[float, str, tuple[str, ...]]], float] = {}

#: Seconds between retries of a failed observation write. `load_config` runs on
#: every dashboard vitals poll and each attempt can wait out create_sync's 1 s
#: lock timeout, so a database that stays unavailable must cost one attempt per
#: interval rather than one per poll. A minute keeps that cost negligible while
#: the observation still lands within a minute of the database coming back.
_OBSERVATION_RETRY_S = 60.0


def _report_overlay_rejected(
    local_path: Path, exc: BaseException, kept: list[str] | None = None
) -> None:
    """Log at ERROR and record a health observation. Never raises."""
    try:
        mtime = local_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = str(local_path)
    detail = f"{type(exc).__name__}: {exc}"
    kept = kept or []
    # The kept restrictions are part of the report's identity: the base can
    # change under an unchanged overlay and change what the fallback kept.
    report = (mtime, detail, tuple(kept))
    if kept == [_OVERLAY_REFUSED]:
        if _REPORTED_OVERLAYS.get(key) != report:
            _REPORTED_OVERLAYS[key] = report
            logger.error(
                # Worded for every caller: this also runs in a live server (the
                # dashboard vitals poll), where the running router is unaffected.
                "Routing overlay %s could not be read as YAML; this load was REFUSED "
                "(the shipped config would lift every restriction in it). A router "
                "started now will NOT come up; a router already running keeps its "
                "current config. Fix or remove the file. Cause: %s",
                local_path, detail, exc_info=exc,
            )
        content = (
            f"[routing] {local_path.name} could not be read as YAML, so loading "
            "routing config is refused: the shipped config would lift every "
            "restriction in it. A router started now will not come up; one already "
            f"running keeps its config. Fix or remove the file. Cause: {detail}"
        )
        _record_overlay_observation(key, report, content)
        return
    if kept == [_EXCLUSIONS_FAILED]:
        still = "Its restrictions could NOT be applied; see the ERROR log."
    elif kept:
        still = f"Its restrictions are still applied: {'; '.join(kept)}."
    else:
        still = "It carried no restrictions that could be read."
    if _REPORTED_OVERLAYS.get(key) != report:
        _REPORTED_OVERLAYS[key] = report
        logger.error(
            "Routing overlay %s rejected; running on the shipped routing config "
            "without its overrides. %s Fix or remove the file. Cause: %s",
            local_path, still, detail, exc_info=exc,
        )
    _record_overlay_observation(
        key,
        report,
        f"[routing] {local_path.name} was rejected and its overrides are "
        f"inactive; routing runs on the shipped config. {still} Cause: {detail}",
    )


def _record_overlay_observation(
    key: str, report: tuple[float, str, tuple[str, ...]], content: str
) -> None:
    """Record the health observation for one overlay report. Never raises."""
    if _RECORDED_OVERLAYS.get(key) == report:
        return
    failed_at = _OBSERVATION_FAILED_AT.get((key, report))
    if failed_at is not None and time.monotonic() - failed_at < _OBSERVATION_RETRY_S:
        return
    try:
        from genesis.db.crud.observations import create_sync_status
        from genesis.env import genesis_db_path

        # The hash names the file VERSION as well as the cause. create_sync
        # dedups on it, so hashing the content alone would suppress the report
        # for an edited file whose error text did not change.
        version = hashlib.sha256(f"{key}|{report!r}".encode()).hexdigest()
        status = create_sync_status(
            str(genesis_db_path()),
            source="routing",
            type="init_degradation",
            category="infrastructure",
            priority="high",
            content=content,
            content_hash=version,
        )
    except Exception:  # noqa: BLE001 - an import failure must not break config loading
        logger.warning("Could not record the routing overlay observation", exc_info=True)
        status = "failed"
    if status == "failed":
        # The ERROR line above is already written; only the row is retried.
        _OBSERVATION_FAILED_AT[(key, report)] = time.monotonic()
        return
    _OBSERVATION_FAILED_AT.pop((key, report), None)
    _RECORDED_OVERLAYS[key] = report


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
        if not _is_enabled(p.get("enabled", True)):
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
            open_duration_s=_number(where, "open_duration_s", p.get("open_duration_s", _OPEN_DURATION_DEFAULT_S)),
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
            # An API-only site with no providers is KEPT too, as a site that
            # cannot run: dropped, it would be missing from the config, and the
            # autonomous dispatcher reads a missing site as `dual`, which can
            # escalate to the CLI the operator ruled out. Kept, `route_call`
            # finds no provider and the dispatcher's api-only branch refuses.
            if dispatch == "dual":
                logger.warning(
                    "Call site '%s' has empty chain after `enabled: false` filter — dropping",
                    name,
                )
                continue
            if dispatch == "api":
                logger.warning(
                    "Call site '%s' is dispatch=api with no providers — kept as BLOCKED "
                    "(it cannot run)",
                    name,
                )
            else:
                logger.info("Call site '%s' has no API providers but dispatch=cli — keeping", name)
        # Validate remaining providers exist
        for provider in chain:
            if provider not in providers:
                msg = f"Call site '{name}' references unknown provider '{provider}'"
                raise ValueError(msg)

        # null clears an inherited profile back to the default, the same way
        # `params: null` clears an inherited params map.
        # Only null means "default". An empty string is a malformed name and is
        # refused below like any other unknown profile.
        retry_profile = _optional_str(where, "retry_profile", cs.get("retry_profile"))
        if retry_profile is None:
            retry_profile = "default"
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
        # Strict: the caller reloads the router with what this returns, so a
        # broken overlay must be refused here, not swapped for the fallback.
        try:
            return load_config(path, strict_overlay=True)
        except Exception as e:
            msg = f"Local overlay {_local_path_for(path).name} could not be loaded; fix or remove it: {e}"
            raise ValueError(msg) from e

    # Start with existing local overlay for this call site. A file that cannot
    # be read is refused rather than overwritten: writing the new entry over it
    # would discard every override the operator had in it.
    #
    # SANITIZED once its shape is known. This is the THIRD reader of the overlay
    # and the only one that also WRITES it back, so reading it raw here makes the
    # rename migration true for the loader and false for the dashboard: a chain
    # pinned to a legacy name would fail every save citing a name shown nowhere,
    # and an override on a renamed key would be written back under the legacy
    # name. Routing the read through the same chokepoint closes both, and heals
    # the overlay on disk at the next save.
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
    # A chain this save replaces is never read, so a malformed one must not stop
    # the save that repairs it.
    target = local_raw["call_sites"].get(call_site_id)
    if chain is not None and isinstance(target, dict):
        target.pop("chain", None)
    # Sanitizing rewrites stale and legacy content by design (dropped sites and
    # chains, renamed provider keys); the loader ignores all of it anyway. What
    # is refused is content it cannot read: a chain that is not a list, or a
    # rung that is not a name.
    try:
        local_raw = _sanitize_local_overlay(base_raw, local_raw)
    except (ValueError, TypeError) as e:
        msg = f"Local overlay {local_path.name} could not be read; fix it first: {e}"
        raise ValueError(msg) from e
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

    # The dispatch this update will leave in effect. An edit may leave a chain
    # empty only for a cli site, which spawns CC directly. The loader also
    # tolerates an empty api chain, keeping the site BLOCKED so the dispatcher
    # cannot read it as missing, but an edit must not author an unrunnable
    # site, so the save refuses it. Without the cli exemption the first empty-chain
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
