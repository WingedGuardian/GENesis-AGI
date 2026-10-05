"""Foreground billing-route selection; never consumed by automated CC failover.

Credentials are resolved only into the child environment and the child's
``--settings`` file, never into route metadata. Selection is stateless and runs
again on resume; no provider request is made.

WHY THE ROUTE IS PINNED THROUGH ``--settings`` AND NOT ONLY THE ENVIRONMENT.
Claude Code applies settings-file ``env`` blocks over the inherited environment,
and it RE-APPLIES them in the running session: when a saved change alters the
merged ``env``, and after ``/cd`` (the new directory's project and local values,
CC 2.1.246+). Source: Claude Code settings reference, ``env`` -> "When Claude
Code applies env values" and "How env values interact with your shell", read
2026-10-04. So a launch-time check of the user's settings files is a snapshot a
later edit can overturn. The ``--settings`` layer outranks local, project and
user settings for every key it sets (settings docs, "Settings precedence"), and
reloads keep that order, so pinning the route there holds for the whole session.
Only managed settings outrank it, a documented residual (``gmodel_settings``).
The MODEL is pinned at launch only: ``/model`` can still change it for the
session, against the same endpoint and key (see ``route_settings``).
This is the layer ``cc/invoker.py`` already uses for dispatched sessions
(``_settings_env_pins``).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from genesis.cc import roster
from genesis.cc.types import VALID_MODEL_NAMES
from genesis.util.atomic import atomic_write_text

ROUTES = ("subscription", "api", "openrouter")
#: What each route bills against. Auto prefers the routes in ROUTES order, so a
#: per-token route is only ever reached after a subscription candidate was
#: skipped — and the launcher must say why (see SelectedRoute.skipped).
COST_BASIS = {"subscription": "subscription quota", "api": "per-token", "openrouter": "per-token"}
PROVIDER_SELECTORS = (
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_MANTLE",
)
#: Names that are never foreground catalog models: the native tiers (derived from
#: the CCModel enum, never restated), the native roster entry, and `default`.
#: Checked BEFORE any configuration is loaded so a broken overlay cannot break
#: `gmodel opus`.
NATIVE_NAMES = frozenset(VALID_MODEL_NAMES | {roster.CLAUDE, "default"})
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CONTEXT_VARS = ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_AUTO_COMPACT_WINDOW")

#: Switches a settings file could flip to disable compaction or thinking.
#: Pinned to an OFF value: CC reads these with its truthy test ("1", "true",
#: "yes", "on"), so "0" is off. MAX_THINKING_TOKENS is pinned EMPTY rather than
#: "0", because 0 is what DISABLES thinking; CC treats an empty value as unset
#: (`if(process.env.MAX_THINKING_TOKENS)` in the 2.1.280 binary). Disabled
#: thinking matters beyond quality: Kimi serves K3 requests without thinking
#: from K2.8 Preview (Kimi Code Claude Code guide, read 2026-10-04).
FEATURE_SWITCH_PINS = {
    "DISABLE_AUTO_COMPACT": "0",
    "DISABLE_COMPACT": "0",
    "CLAUDE_CODE_DISABLE_THINKING": "0",
    "MAX_THINKING_TOKENS": "",
    "CLAUDE_CODE_SIMPLE": "0",
}
#: Credential and header variables pinned EMPTY so no settings file can add a
#: second credential next to the selected one. ANTHROPIC_AUTH_TOKEN outranks
#: ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN and the saved /login credential
#: (Claude Code authentication docs, "Authentication precedence"); the empty
#: pins remove the lower-ranked ones as well rather than relying on rank alone.
EMPTY_CREDENTIAL_PINS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS")
def settings_dir() -> Path:
    """Where routed launches keep their ``--settings`` files (resolved per call)."""
    return Path.home() / ".genesis" / "gmodel-settings"


@dataclass(frozen=True)
class SelectedRoute:
    name: str
    route: str
    anthropic_base_url: str | None
    auth_env: str
    model_id: str
    context_tokens: int
    effort: str = "high"
    interactive_only: bool = False
    #: Why each preferred route was passed over before this one was chosen.
    skipped: tuple[str, ...] = ()

    @property
    def cost_basis(self) -> str:
        return COST_BASIS[self.route]


def _catalog_models(roster_data: dict) -> dict:
    block = roster_data.get("gmodel", {})
    if not isinstance(block, dict) or not isinstance(block.get("models", {}), dict):
        raise roster.RosterError("gmodel.models must be a mapping")
    return block.get("models", {})


def _is_member(roster_data: dict, name: str) -> bool:
    block = roster_data.get("gmodel")
    models = block.get("models") if isinstance(block, dict) else None
    return isinstance(models, dict) and name in models


def _validated_entry(roster_data: dict, name: str, model: object) -> dict[str, SelectedRoute]:
    """Validate ONE catalog entry, including the namespace rule that applies to it."""
    if not isinstance(name, str) or not name.strip() or not isinstance(model, dict):
        raise roster.RosterError("gmodel model names must be strings with mapping definitions")
    legacy = roster_data.get("models", {})
    if name in NATIVE_NAMES or (isinstance(legacy, dict) and name in legacy):
        raise roster.RosterError(f"Ambiguous gmodel model name: {name}")
    return _parsed_routes(name, model)


def catalog(roster_data: dict) -> dict:
    """Return the foreground catalog, validating EVERY entry (the listing path).

    Selection (``resolve_route``) validates only the requested entry, so an
    invalid sibling cannot stop a valid route from launching.
    """
    models = _catalog_models(roster_data)
    for name, model in models.items():
        _validated_entry(roster_data, name, model)
    return models


def _route_from(name: str, kind: str, raw: dict) -> SelectedRoute:
    label = f"gmodel.models.{name}.routes.{kind}"
    if not isinstance(raw, dict):
        raise roster.RosterError(f"{label} must be a mapping")
    if "auth_mode" in raw:
        # Every route authenticates with ANTHROPIC_AUTH_TOKEN (bearer). An API-key
        # mode would put the key in ANTHROPIC_API_KEY, which interactive Claude
        # Code asks the user to approve; declining it falls through to the saved
        # /login credential, which would then be sent to this third-party URL.
        raise roster.RosterError(f"{label}.auth_mode is not supported; routes always use a bearer token")
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
    context = raw.get("context_tokens", 1_048_576)
    if type(context) is not int or context <= 0:
        raise roster.RosterError(f"{label}.context_tokens must be a positive integer")
    if "[1m]" in model_id and context < 1_000_000:
        raise roster.RosterError(f"{label}: remove [1m] when configuring a smaller context")
    effort = raw.get("effort", "high")
    if effort not in EFFORTS:
        raise roster.RosterError(f"{label}.effort must be one of {', '.join(EFFORTS)}")
    interactive_only = raw.get("interactive_only", False)
    if type(interactive_only) is not bool:
        raise roster.RosterError(f"{label}.interactive_only must be a boolean")
    return SelectedRoute(name, kind, base_url, auth_env, model_id, context, effort, interactive_only)


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
    """Resolve a foreground route; return None for a native tier or flat roster name.

    Missing credentials/explicitly unconfigured URLs may advance Auto. Invalid
    configuration never does. No retry or billing-route fallback is performed.
    The returned route records, in ``skipped``, why every preferred route was
    passed over, so a launcher can say why Auto landed on a per-token route.
    """
    if name in NATIVE_NAMES:
        # Before any configuration load: the strict load below must never be
        # able to break a native launch (`gmodel opus` with a broken overlay).
        return None
    if roster_data is not None:
        data = roster_data
    else:
        try:
            data = roster.load_roster(strict=True)
        except roster.RosterError:
            # Strict loading binds only foreground-catalog members. Membership
            # comes from the base file, which is what the lenient loader keeps
            # when the overlay is broken: a member re-raises (a broken overlay
            # must not silently restore the shipped Auto preference), anything
            # else returns None and keeps the flat roster's lenient path. The
            # base is read strictly, without the overlay, so this read logs no
            # parser text. If the base itself is unreadable, membership cannot
            # be known: return None, and the flat path reports the broken base.
            try:
                base = roster._load_yaml(roster._CONFIG_DIR / roster._ROSTER_FILE, strict=True)
            except roster.RosterError:
                return None
            if _is_member(base, name):
                raise
            return None
    if not _is_member(data, name):
        return None
    model = _catalog_models(data)[name]
    # Only the requested entry is validated; `catalog()` (the listing) checks all.
    parsed = _validated_entry(data, name, model)
    preference = model.get("route", "auto")
    requested = route if route is not None else preference
    if requested not in ("auto", *ROUTES):
        raise roster.RosterError("--route must be auto, subscription, api or openrouter")
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
            return dataclasses.replace(selected, skipped=tuple(unavailable))
    raise roster.RosterError(f"No available {requested} route for {name} ({'; '.join(unavailable)})")


def route_env_pins(selected: SelectedRoute, token: str) -> dict[str, str]:
    """Every environment value that pins the selected route, for env AND --settings.

    One builder for both layers, so the environment that is launched and the
    settings layer that holds it in place cannot disagree.
    """
    if not token.strip() or not selected.anthropic_base_url:
        raise roster.RosterError(f"Selected route requires {selected.auth_env} and an endpoint")
    pins: dict[str, str] = {}
    roster.apply_routing_env(
        pins, base_url=selected.anthropic_base_url, auth_token=token, model_id=selected.model_id,
    )
    for variable in (*EMPTY_CREDENTIAL_PINS, *PROVIDER_SELECTORS):
        # An empty value counts as unset for provider selection, and overrides a
        # lower-level value (settings reference, "How env values interact with
        # your shell").
        pins[variable] = ""
    for variable in _CONTEXT_VARS:
        pins[variable] = str(selected.context_tokens)
    pins["CLAUDE_CODE_EFFORT_LEVEL"] = selected.effort
    # Every catalog model ID is a custom spelling. Claude Code documents this
    # switch as the one that sends effort "even when Claude Code does not
    # recognize the model ID as effort-capable" (env-vars reference,
    # code.claude.com/docs/en/env-vars, read 2026-10-04). MEASURED on CC 2.1.280
    # against a local listener: `output_config.effort` was sent for k3,
    # kimi-k3 and moonshotai/kimi-k3 with and without it, so today it changes
    # nothing; it is pinned because it is the documented contract and that
    # recognition logic is CC's to change between versions.
    pins["CLAUDE_CODE_ALWAYS_ENABLE_EFFORT"] = "1"
    pins.update(FEATURE_SWITCH_PINS)
    pins["GENESIS_ROSTER_MODEL"] = selected.name
    return pins


def apply_route_env(env: dict[str, str], selected: SelectedRoute) -> dict[str, str]:
    """Apply the resolved route to a child; the caller passes an isolated copy."""
    pins = route_env_pins(selected, env.get(selected.auth_env, ""))
    env.update(pins)
    return env


def route_settings(selected: SelectedRoute, pins: Mapping[str, str]) -> dict:
    """The ``--settings`` document for a routed launch.

    ``env`` carries the pins. The keys beside it pin the settings that would
    otherwise switch features off or change the model from a lower settings
    level: ``model`` (the env model already outranks it; set for consistency),
    ``fallbackModel`` (the highest file that defines it supplies the whole
    chain, so a user-level chain naming a Claude model would otherwise be sent
    to this endpoint on overload; a chain equal to the primary was accepted by
    CC 2.1.280, measured 2026-10-04), thinking and automatic compaction. A
    managed value for any of these outranks this layer; that residual is
    documented, not checked (``gmodel_settings``).

    NOT pinnable here: ``maxEffortLevel``. When several files set it the LOWEST
    applies, and it caps CLAUDE_CODE_EFFORT_LEVEL too (settings reference,
    ``maxEffortLevel``), so a cap in any settings file still lowers the effort.
    ``gmodel_settings.effort_cap_warnings`` reports one at launch.

    NOT pinned either: the session model after launch. ``/model`` outranks
    ``ANTHROPIC_MODEL`` and the ``model`` key (model-config docs, "Setting your
    model"), and changes the model for the session against the SAME routed
    endpoint and key. The alias slots all point at the route's model, so
    ``/model opus`` stays on it; a full ID such as ``/model claude-opus-4-6``
    does not. On Kimi or MiMo endpoints that fails as an unknown model. On
    OpenRouter it would not: OpenRouter serves Claude model IDs on the same key
    (its Claude Code guide configures only the base URL and token, and its
    "Anthropic Skin" maps the model; openrouter.ai/docs, Claude Code guide, read
    2026-10-04), so the switch would bill an Anthropic model per token. The
    launcher says so in the OpenRouter route's launch notice.

    NOT pinned: thinking within the session. Alt+T (Option+T) turns it off,
    and Kimi then serves a K3 model ID from K2.8 Preview (Kimi Code Claude Code
    guide, read 2026-10-04). The launcher says so on Kimi routes.
    """
    return {
        "env": dict(sorted(pins.items())),
        "model": selected.model_id,
        "fallbackModel": [selected.model_id],
        "alwaysThinkingEnabled": True,
        "autoCompactEnabled": True,
    }


def write_route_settings(document: Mapping, directory: Path | None = None) -> Path:
    """Write ``document`` to an owner-only settings file and return its path.

    The file holds the selected credential, so it is created 0600 inside a 0700
    directory, never passed inline (an inline ``--settings`` JSON argument is
    readable by every local user through the process list). It is named for its
    content, as the invoker names its pin files: concurrent launches of the same
    route agree on the bytes, and a later launch with different content writes a
    different file instead of rewriting one a running session reloads from.
    """
    directory = directory or settings_dir()
    payload = json.dumps(document, indent=2, sort_keys=True)
    path = directory / f"route-{hashlib.sha256(payload.encode()).hexdigest()[:16]}.json"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise roster.RosterError(f"Routing settings directory {directory} is not a directory you own")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(directory, 0o700)
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            existing = None
        if (existing is not None and stat.S_ISREG(existing.st_mode)
                and not stat.S_IMODE(existing.st_mode) & 0o077
                and path.read_text(encoding="utf-8") == payload):
            return path
        # The shared helper: mkstemp creates the temp 0600 with O_EXCL (never
        # through a link), it is fsynced, replaced atomically, and unlinked on
        # any failure.
        atomic_write_text(path, payload)
    except OSError:
        # No path to fall back to: launching without the pins is the unpinned
        # session this file exists to prevent.
        raise roster.RosterError(f"Cannot write routing settings file in {directory}") from None
    return path
