"""Foreground billing-route selection; never consumed by automated CC failover.

Credentials are resolved only into the child environment, not route metadata.
Selection is stateless and runs again on resume; no provider request is made.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from genesis.cc import roster

ROUTES = ("subscription", "api", "openrouter")
PROVIDER_SELECTORS = (
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_MANTLE",
)
_NATIVE_NAMES = {"claude", "opus", "sonnet", "haiku", "fable", "default"}
_CONTEXT_VARS = ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_AUTO_COMPACT_WINDOW")


@dataclass(frozen=True)
class SelectedRoute:
    name: str
    route: str
    anthropic_base_url: str | None
    auth_env: str
    model_id: str
    auth_mode: str
    context_tokens: int
    effort: str = "high"
    interactive_only: bool = False


def catalog(roster_data: dict) -> dict:
    """Return the foreground catalog, validating its shape and name namespace."""
    block = roster_data.get("gmodel", {})
    if not isinstance(block, dict) or not isinstance(block.get("models", {}), dict):
        raise roster.RosterError("gmodel.models must be a mapping")
    models = block.get("models", {})
    legacy = roster_data.get("models", {})
    for name, model in models.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(model, dict):
            raise roster.RosterError("gmodel model names must be strings with mapping definitions")
        if name in _NATIVE_NAMES or (isinstance(legacy, dict) and name in legacy):
            raise roster.RosterError(f"Ambiguous gmodel model name: {name}")
        _parsed_routes(name, model)
    return models


def _route_from(name: str, kind: str, raw: dict) -> SelectedRoute:
    label = f"gmodel.models.{name}.routes.{kind}"
    if not isinstance(raw, dict):
        raise roster.RosterError(f"{label} must be a mapping")
    auth_env = raw.get("auth_env")
    if not isinstance(auth_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", auth_env):
        raise roster.RosterError(f"{label}.auth_env must name a credential environment variable")
    model_id = raw.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise roster.RosterError(f"{label}.model_id must be a nonempty string")
    base_url = raw.get("anthropic_base_url")
    if base_url is not None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise roster.RosterError(f"{label}.anthropic_base_url must be a URL or null")
        try:
            url = urlsplit(base_url)
            valid = (url.scheme == "https" and url.hostname and not url.username
                     and not url.password and not url.query and not url.fragment)
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            raise roster.RosterError(f"{label}.anthropic_base_url must be HTTPS without credentials")
    auth_mode = raw.get("auth_mode", "bearer")
    if auth_mode not in ("bearer", "api_key"):
        raise roster.RosterError(f"{label}.auth_mode must be bearer or api_key")
    context = raw.get("context_tokens", 1_048_576)
    if type(context) is not int or context <= 0:
        raise roster.RosterError(f"{label}.context_tokens must be a positive integer")
    if "[1m]" in model_id and context < 1_000_000:
        raise roster.RosterError(f"{label}: remove [1m] when configuring a smaller context")
    effort = raw.get("effort", "high")
    if effort not in ("low", "medium", "high", "max"):
        raise roster.RosterError(f"{label}.effort must be low, medium, high or max")
    interactive_only = raw.get("interactive_only", False)
    if type(interactive_only) is not bool:
        raise roster.RosterError(f"{label}.interactive_only must be a boolean")
    return SelectedRoute(name, kind, base_url, auth_env, model_id, auth_mode,
                         context, effort, interactive_only)


def _parsed_routes(name: str, model: dict) -> dict[str, SelectedRoute]:
    if model.get("route", "auto") not in ("auto", *ROUTES):
        raise roster.RosterError(f"gmodel.models.{name}.route must be auto, subscription, api or openrouter")
    raw_routes = model.get("routes")
    if not isinstance(raw_routes, dict) or any(k not in ROUTES for k in raw_routes):
        raise roster.RosterError(f"gmodel.models.{name}.routes must map subscription, api or openrouter")
    return {kind: _route_from(name, kind, raw) for kind, raw in raw_routes.items()}


def resolve_route(
    name: str, *, route: str | None = None, interactive: bool = True,
    roster_data: dict | None = None, environ: Mapping[str, str] | None = None,
) -> SelectedRoute | None:
    """Resolve a foreground route; return None for an existing flat roster name.

    Missing credentials/explicitly unconfigured URLs may advance Auto. Invalid
    configuration never does. No retry or billing-route fallback is performed.
    """
    data = roster_data if roster_data is not None else roster.load_roster(strict=True)
    # Strict foreground validation must not disable an unrelated existing tier
    # or automated peer. Inspect membership before validating the catalog.
    block = data.get("gmodel")
    raw_models = block.get("models") if isinstance(block, dict) else None
    if not isinstance(raw_models, dict) or name not in raw_models:
        return None
    models = catalog(data)
    if name not in models:
        return None
    model = models[name]
    preference = model.get("route", "auto")
    requested = route if route is not None else preference
    if requested not in ("auto", *ROUTES):
        raise roster.RosterError("--route must be auto, subscription, api or openrouter")
    parsed = _parsed_routes(name, model)
    keys = os.environ if environ is None else environ
    candidates = ROUTES if requested == "auto" else (requested,)
    unavailable = []
    for kind in candidates:
        selected = parsed.get(kind)
        if selected is None:
            unavailable.append(f"{kind}: not configured")
        elif selected.interactive_only and not interactive:
            unavailable.append(f"{kind}: requires personal interactive use")
        elif not selected.anthropic_base_url:
            unavailable.append(f"{kind}: configure the account-console endpoint")
        elif not keys.get(selected.auth_env, "").strip():
            unavailable.append(f"{kind}: missing {selected.auth_env}")
        else:
            return selected
    raise roster.RosterError(f"No available {requested} route for {name} ({'; '.join(unavailable)})")


def apply_route_env(env: dict[str, str], selected: SelectedRoute) -> dict[str, str]:
    """Apply the resolved route to a child; the caller passes an isolated copy."""
    token = env.get(selected.auth_env, "")
    if env.get("ANTHROPIC_CUSTOM_HEADERS", "").strip():
        raise roster.RosterError("Remove ANTHROPIC_CUSTOM_HEADERS before selecting a foreground billing route")
    if not token.strip() or not selected.anthropic_base_url:
        raise roster.RosterError(f"Selected route requires {selected.auth_env} and an endpoint")
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    for variable in PROVIDER_SELECTORS:
        env.pop(variable, None)
    roster.apply_routing_env(
        env, base_url=selected.anthropic_base_url,
        auth_token=token if selected.auth_mode == "bearer" else None,
        model_id=selected.model_id,
    )
    if selected.auth_mode == "api_key":
        env["ANTHROPIC_API_KEY"] = token
    elif selected.route == "openrouter":
        env["ANTHROPIC_API_KEY"] = ""
    for variable in _CONTEXT_VARS:
        env[variable] = str(selected.context_tokens)
    env["CLAUDE_CODE_EFFORT_LEVEL"] = selected.effort
    env["GENESIS_ROSTER_MODEL"] = selected.name
    return env
