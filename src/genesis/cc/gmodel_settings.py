"""Launch checks for foreground catalog routes: what ``--settings`` cannot pin.

The route itself is pinned by the ``--settings`` file the launcher passes
(``gmodel_routes.route_settings``), which outranks local, project and user
settings for the whole session, including reloads and ``/cd``. Two things sit
above or beside that layer, and only those are checked here:

* MANAGED settings outrank ``--settings`` ("Nothing you set overrides them: a
  key you pass with --settings doesn't override the same managed key"; Claude
  Code settings docs, "Settings precedence", read 2026-10-04). Only the file
  tier under ``/etc/claude-code`` is readable here; MDM and server-managed
  settings are not, and stay a documented residual.
* COMMAND-LINE flags the user passes through. ``--model`` outranks
  ``ANTHROPIC_MODEL``; ``--effort`` and ``--autocompact`` are overridden by the
  pinned environment, so a mismatching value would be silently ignored, which is
  refused rather than allowed to mislead.

Nothing here GATES on user, project or local settings: the pins make them
irrelevant to the route, and reading them once could not stop a later reload.
The one key the pins cannot override from those files is ``maxEffortLevel``
(the lowest cap across files applies); ``effort_cap_warnings`` reports one at
launch as advice, never as a refusal, because a later edit could add one anyway.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path

from genesis.cc import gmodel_routes, roster

_MANAGED_DIR = Path("/etc/claude-code")
# Option arities read from installed CC --help; argparse handles optional values,
# aliases and short clusters. Hidden prompt-file options are retained for parity
# with Genesis's invoker. Unknown options remain CC's responsibility.
_VALUE_OPTIONS = frozenset({
    "--agent", "--agents", "--append-system-prompt", "--append-system-prompt-file",
    "--system-prompt", "--system-prompt-file", "--system-prompt-snapshot",
    "--permission-mode", "--allowedTools", "--allowed-tools", "--disallowedTools",
    "--disallowed-tools", "--tools", "--mcp-config", "--plugin-dir", "--add-dir",
    "--input-format", "--output-format", "--json-schema", "--max-budget-usd",
    "--max-turns", "--betas", "--session-id", "--name", "-n", "--teammate-mode",
    "--debug-file", "--file", "--cwd", "--plugin-url", "--environment",
    "--permission-prompts", "--remote-control-session-name-prefix", "--setting-sources",
})
_CHECKED_OPTIONS = frozenset({
    "--model", "--fallback-model", "--effort", "--autocompact", "--settings", "--route",
})
_OPTIONAL_OPTIONS = {"--from-pr", "--resume", "-r", "--worktree", "-w", "--debug", "-d",
                     "--cloud", "--remote", "--prompt-suggestions", "--remote-control", "--teleport"}
_VARIADIC_OPTIONS = {"--add-dir", "--allowedTools", "--allowed-tools", "--disallowedTools",
                     "--disallowed-tools", "--tools", "--mcp-config", "--betas", "--file"}
_FLAG_OPTIONS = {"-p", "--print", "--bg", "--background", "--continue", "-c",
                 "--bare", "--resume", "-r", "--from-pr", "--teleport", "--cloud", "--remote", "--environment"}
#: Flags that move the session somewhere this launcher's environment and
#: settings file do not reach, or (``--bare``) change how CC authenticates.
_REFUSED_FLAGS = frozenset({"--cloud", "--remote", "--teleport", "--environment", "--bare"})
_EFFORT_RANK = {level: rank for rank, level in enumerate(gmodel_routes.EFFORTS)}
_MANAGED_READ_LIMIT = 2 * 1024 * 1024  # CC's own --settings bound; the same cap here.


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's normal error includes raw values; these can be secrets.
        raise roster.RosterError("Cannot parse Claude arguments; use --option=value for literal flag values")


class _Record(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        namespace.records.append((option_string, values if isinstance(values, str) else ""))


def _scan(args: list[str]) -> list[tuple[str, str, int, int]]:
    parser = _Parser(add_help=False, allow_abbrev=False)
    required = (_VALUE_OPTIONS | _CHECKED_OPTIONS) - _VARIADIC_OPTIONS
    known = required | _OPTIONAL_OPTIONS | _VARIADIC_OPTIONS | _FLAG_OPTIONS
    for key in sorted(known):
        arity = "?" if key in _OPTIONAL_OPTIONS else ("+" if key in _VARIADIC_OPTIONS else (None if key in required else 0))
        parser.add_argument(key, nargs=arity, action=_Record)
    # Bind required option values atomically. Commander accepts a required
    # prompt value such as '--model'; argparse requires the equivalent '=value'.
    normalized = []
    route_spans = []
    i = 0
    while i < len(args):
        start = i
        argument = args[i]
        if argument == "--":
            normalized.extend(args[i:])
            break
        key, equal, value = argument.partition("=")
        if key in required and not equal:
            i += 1
            if i >= len(args):
                raise roster.RosterError(f"Missing value for {key}")
            argument = f"{key}={args[i]}"
        normalized.append(argument)
        if key == "--route":
            route_spans.append((start, i + 1))
        i += 1
    namespace = argparse.Namespace(records=[])
    parser.parse_known_args(normalized, namespace=namespace)
    result = []
    for key, value in namespace.records:
        start, end = route_spans.pop(0) if key == "--route" else (0, 0)
        if key in _CHECKED_OPTIONS | _FLAG_OPTIONS:
            result.append((key, value, start, end))
    return result


def launch_flags(args: list[str]) -> frozenset[str]:
    """Recognize launch mode flags, excluding prompt values and the delimiter."""
    return frozenset(key for key, _, _, _ in _scan(args) if key in _FLAG_OPTIONS)


def extract_route(args: list[str]) -> tuple[str | None, list[str]]:
    """Consume one launcher route flag without consuming CC option values."""
    routes = [(value, start, end) for key, value, start, end in _scan(args)
              if key == "--route"]
    if len(routes) > 1:
        raise roster.RosterError("Specify --route only once")
    if not routes:
        return None, list(args)
    value, start, end = routes[0]
    if not value or value.startswith("--"):
        raise roster.RosterError("Missing value for --route")
    return value, args[:start] + args[end:]


def _conflict(source: str, key: str, hint: str = "") -> None:
    # Never interpolate values: credentials and inline JSON can contain secrets.
    raise roster.RosterError(f"Routing conflict in {source}: {key}{hint}")


def validate_cli(passthrough: list[str], selected: gmodel_routes.SelectedRoute) -> None:
    """Refuse pass-through flags that would override or silently contradict the route."""
    for key, value, _start, _end in _scan(passthrough):
        if key in {"--model", "--fallback-model"}:
            if value != selected.model_id:
                _conflict("command line", key)
        elif key == "--effort":
            # Exact: CLAUDE_CODE_EFFORT_LEVEL (pinned) overrides --effort, and
            # `max` is a distinct, deeper level, not an alias of `high` (Claude
            # Code env-vars reference; Kimi maps high and max to distinct levels).
            if value != selected.effort:
                _conflict("command line", key, f" (this route pins effort {selected.effort})")
        elif key == "--autocompact":
            if value != str(selected.context_tokens):
                _conflict("command line", key)
        elif key == "--settings":
            _conflict("command line", key, " (gmodel passes its own --settings for a routed launch;"
                      " put other settings in a settings file)")
        elif key in _REFUSED_FLAGS:
            _conflict("command line", key)


def _read(path: Path) -> object | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MANAGED_READ_LIMIT + 1)
        if len(raw) > _MANAGED_READ_LIMIT:
            raise ValueError
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError
        return document
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError):
        pass
    raise roster.RosterError(f"Cannot read valid settings from {path}")


def _is_off(value: object) -> bool:
    return str(value).strip().lower() in ("", "0", "false", "no", "off")


def _env_conflicts(key: str, value: object, pinned: str) -> bool:
    """Would this MANAGED env value override a pin in a way that matters?"""
    if key == "MAX_THINKING_TOKENS":
        # Positive budgets keep thinking on; only 0 (or garbage) turns it off.
        text = str(value).strip()
        return bool(text) and not (text.isdigit() and int(text) > 0)
    if pinned in ("", "0") and key not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                                            "ANTHROPIC_CUSTOM_HEADERS"):
        return not _is_off(value)
    if pinned == "":
        return str(value) != ""
    return str(value) != pinned


def _model_listed(entries: list, model_id: str) -> bool:
    """Exact match only, with or without the `[1m]` context tag.

    CC also matches family aliases and version prefixes for Claude IDs; how it
    applies prefixes to a third-party ID is undocumented, so a prefix-only match
    is treated as NOT listed and the launch is refused rather than guessed.
    """
    bare = model_id.replace("[1m]", "").replace("[1M]", "")
    return any(isinstance(e, str) and e in (model_id, bare) for e in entries)


def _model_denied(entries: list, model_id: str) -> bool:
    """Conservative: a denied entry contained anywhere in the ID refuses the launch."""
    bare = model_id.lower().replace("[1m]", "")
    return any(isinstance(e, str) and e.strip() and e.strip().lower() in bare for e in entries)


def _check_managed(document: object, source: str, selected: gmodel_routes.SelectedRoute,
                   pins: Mapping[str, str]) -> None:
    if not isinstance(document, dict):
        raise roster.RosterError(f"Invalid settings object in {source}")
    settings_env = document.get("env", {})
    if not isinstance(settings_env, dict):
        raise roster.RosterError(f"Invalid settings env in {source}")
    for key in sorted(pins.keys() & settings_env.keys()):
        if _env_conflicts(key, settings_env[key], pins[key]):
            _conflict(source, f"env.{key}")
    available = document.get("availableModels")
    if available is not None and not (isinstance(available, list)
                                      and _model_listed(available, selected.model_id)):
        _conflict(source, "availableModels", " (the managed allowlist does not list this route's model ID)")
    denied = document.get("deniedModels")
    if isinstance(denied, list) and _model_denied(denied, selected.model_id):
        _conflict(source, "deniedModels")
    if "fallbackModel" in document:
        chain = document["fallbackModel"]
        chain = chain if isinstance(chain, list) else [chain]
        if any(model != selected.model_id for model in chain):
            _conflict(source, "fallbackModel")
    if document.get("alwaysThinkingEnabled") is False:
        _conflict(source, "alwaysThinkingEnabled")
    if document.get("autoCompactEnabled") is False:
        _conflict(source, "autoCompactEnabled")
    # An unrecognised managed cap counts as the strictest: CC may fall back to a
    # stricter value for an invalid managed entry, and this must not guess.
    if any(_cap_rank(cap, -1) < _EFFORT_RANK[selected.effort]
           for cap in _effort_caps(document, selected.model_id)):
        _conflict(source, "maxEffortLevel")
    # A gateway sign-in outranks every credential variable, the bearer token
    # included, and these managed keys require it (Claude Code authentication
    # docs, "Authentication precedence"). The route would not be the one used.
    if document.get("forceLoginMethod") == "gateway" or document.get("forceLoginGatewayUrl"):
        _conflict(source, "forceLoginMethod" if document.get("forceLoginMethod") == "gateway"
                  else "forceLoginGatewayUrl")
    # A managed `model` is NOT a conflict: ANTHROPIC_MODEL (pinned) takes
    # precedence over the `model` key from every source, managed included
    # (settings reference, `model`: "both take precedence over this key for one
    # session, including over a managed model").


def validate_managed_settings(selected: gmodel_routes.SelectedRoute, pins: Mapping[str, str],
                              *, managed_dir: Path | None = None) -> None:
    """Refuse a launch whose managed settings would override the pinned route."""
    directory = managed_dir or _MANAGED_DIR
    paths = [directory / "managed-settings.json"]
    try:
        paths.extend(p for p in sorted((directory / "managed-settings.d").glob("*.json"))
                     if not p.name.startswith("."))
    except OSError:
        raise roster.RosterError(f"Cannot read settings directory {directory}") from None
    for path in paths:
        document = _read(path)
        if document is not None:
            _check_managed(document, str(path), selected, pins)


def _effort_caps(document: object, model_id: str) -> list[str]:
    if not isinstance(document, dict):
        return []
    caps = []
    per_model = document.get("modelSettings")
    entry = per_model.get(model_id) if isinstance(per_model, dict) else None
    if isinstance(entry, dict) and "maxEffortLevel" in entry:
        # A per-model entry replaces the file-wide key for that model.
        caps.append(entry["maxEffortLevel"])
    elif "maxEffortLevel" in document:
        caps.append(document["maxEffortLevel"])
    return caps


def _cap_rank(cap: object, invalid: int) -> int:
    return _EFFORT_RANK.get(cap, invalid) if isinstance(cap, str) else invalid


def effort_cap_warnings(selected: gmodel_routes.SelectedRoute, *, cwd: Path,
                        environ: Mapping[str, str]) -> list[str]:
    """Advisory: settings files whose ``maxEffortLevel`` lowers this route's effort.

    Read best-effort and never fatal: an unreadable file is skipped, and a file
    edited after launch can still add a cap, so this cannot be a guarantee.
    """
    config = Path(environ.get("CLAUDE_CONFIG_DIR") or Path(environ.get("HOME") or Path.home()) / ".claude")
    wanted = _EFFORT_RANK[selected.effort]
    warnings = []
    for path in (config / "settings.json", cwd / ".claude" / "settings.json",
                 cwd / ".claude" / "settings.local.json"):
        try:
            document = _read(path)
        except roster.RosterError:
            continue
        for cap in _effort_caps(document, selected.model_id):
            if _cap_rank(cap, len(gmodel_routes.EFFORTS)) < wanted:
                warnings.append(f"{path} caps effort at {cap}; this route asks for {selected.effort}")
    return warnings
