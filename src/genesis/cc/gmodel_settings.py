"""Read-only settings preflight for foreground catalog launches.

Checks documented local sources without rewriting settings or reproducing their
full precedence algorithm. Conflicting pins are refused conservatively.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from genesis.cc import gmodel_routes, roster

_MANAGED_DIR = Path("/etc/claude-code")
_PROTECTED_ENV = frozenset(roster._ROSTER_MODEL_ENV_VARS) | set(gmodel_routes.PROVIDER_SELECTORS) | {
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_EFFORT_LEVEL",
    "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_SIMPLE",
    roster._SUBAGENT_MODEL_FORCE_VAR,
}
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
    "--permission-prompts", "--remote-control-session-name-prefix",
})
_CHECKED_OPTIONS = frozenset({
    "--model", "--fallback-model", "--effort", "--autocompact",
    "--settings", "--setting-sources", "--route",
})
_OPTIONAL_OPTIONS = {"--from-pr", "--resume", "-r", "--worktree", "-w", "--debug", "-d",
                     "--cloud", "--remote", "--prompt-suggestions", "--remote-control", "--teleport"}
_VARIADIC_OPTIONS = {"--add-dir", "--allowedTools", "--allowed-tools", "--disallowedTools",
                     "--disallowed-tools", "--tools", "--mcp-config", "--betas", "--file"}
_FLAG_OPTIONS = {"--restricted", "-p", "--print", "--bg", "--background", "--continue", "-c",
                 "--bare", "--resume", "-r", "--from-pr", "--teleport", "--cloud", "--remote", "--environment"}


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


def _conflict(source: str, key: str) -> None:
    # Never interpolate values: credentials and inline JSON can contain secrets.
    raise roster.RosterError(f"Routing conflict in {source}: {key}")


def _effort_matches(value: object, env: dict[str, str]) -> bool:
    expected = env.get("CLAUDE_CODE_EFFORT_LEVEL")
    return value == expected or (expected == "high" and value == "max")


def _check(document: object, source: str, env: dict[str, str]) -> None:
    if not isinstance(document, dict):
        raise roster.RosterError(f"Invalid settings object in {source}")
    expected_model = env["ANTHROPIC_MODEL"]
    for key in ("model", "fallbackModel"):
        if key not in document:
            continue
        value = document[key]
        models = value if isinstance(value, list) else [value]
        if any(model != expected_model for model in models):
            _conflict(source, key)
    settings_env = document.get("env", {})
    if not isinstance(settings_env, dict):
        raise roster.RosterError(f"Invalid settings env in {source}")
    for key in _PROTECTED_ENV & settings_env.keys():
        if key == "CLAUDE_CODE_SIMPLE":
            if env.get("ANTHROPIC_AUTH_TOKEN") and str(settings_env[key]).lower() not in ("", "0", "false", "no", "off"):
                _conflict(source, f"env.{key}")
            continue
        if key in gmodel_routes.PROVIDER_SELECTORS:
            if str(settings_env[key]).lower() not in ("", "0", "false", "no", "off"):
                _conflict(source, f"env.{key}")
            continue
        if settings_env[key] != env.get(key, ""):
            _conflict(source, f"env.{key}")
    if ("effortLevel" in document and "CLAUDE_CODE_EFFORT_LEVEL" in env
            and not _effort_matches(document["effortLevel"], env)):
        _conflict(source, "effortLevel")
    if "CLAUDE_CODE_EFFORT_LEVEL" in env and document.get("alwaysThinkingEnabled") is False:
        _conflict(source, "alwaysThinkingEnabled")
    if settings_env.get("MAX_THINKING_TOKENS") in (0, "0"):
        _conflict(source, "env.MAX_THINKING_TOKENS")


def _read(path: Path, *, required: bool = False) -> object | None:
    try:
        # CC limits --settings to 2 MiB; apply the same bound to preflight reads.
        with path.open("rb") as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError
        return document
    except FileNotFoundError:
        if not required:
            return None
    except (OSError, ValueError, UnicodeError):
        pass
    raise roster.RosterError(f"Cannot read valid settings from {path}")


def _local_roots(cwd: Path) -> list[Path]:
    """Include legacy cwd settings and the current Git/main root local file."""
    roots = [cwd]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return roots
    if result.returncode == 0:
        lines = result.stdout.splitlines()
        if len(lines) == 2:
            root, common = map(Path, lines)
            # Custom external Git directories do not identify a main checkout.
            roots.append(common.parent if common.name == ".git" else root)
    return list(dict.fromkeys(roots))


def validate_settings(
    passthrough: list[str], child_env: dict[str, str], *, cwd: Path | None = None,
) -> None:
    """Refuse enabled file/CLI settings that conflict with selected routing."""
    cwd = (cwd or Path.cwd()).resolve()
    options = _scan(passthrough)
    sources = {"user", "project", "local"}
    supplied = []
    if child_env.get("MAX_THINKING_TOKENS") == "0":
        _conflict("environment", "MAX_THINKING_TOKENS")
    if (child_env.get("ANTHROPIC_AUTH_TOKEN")
            and child_env.get("CLAUDE_CODE_SIMPLE", "").lower() not in ("", "0", "false", "no", "off")):
        _conflict("environment", "CLAUDE_CODE_SIMPLE")
    for key, value, _start, _end in options:
        if key in {"--model", "--fallback-model"}:
            if value != child_env["ANTHROPIC_MODEL"]:
                _conflict("command line", key)
        elif key == "--effort":
            if "CLAUDE_CODE_EFFORT_LEVEL" in child_env and not _effort_matches(value, child_env):
                _conflict("command line", key)
        elif key == "--autocompact":
            if value != child_env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"):
                _conflict("command line", key)
        elif key == "--setting-sources":
            sources = set(value.split(",")) if value else set()
            if sources - {"user", "project", "local"}:
                raise roster.RosterError("Invalid --setting-sources")
        elif key == "--settings":
            supplied.append(value)
        elif (key in {"--cloud", "--remote", "--teleport", "--environment"}
              or (key == "--bare" and child_env.get("ANTHROPIC_AUTH_TOKEN"))):
            _conflict("command line", key)
    if any(key == "--restricted" for key, _value, _start, _end in options):
        sources = set()
    paths = [_MANAGED_DIR / "managed-settings.json"]
    try:
        paths.extend(p for p in sorted((_MANAGED_DIR / "managed-settings.d").glob("*.json"))
                     if not p.name.startswith("."))
    except OSError:
        raise roster.RosterError(f"Cannot read settings directory {_MANAGED_DIR}") from None
    if "user" in sources:
        home = Path(child_env.get("HOME", str(Path.home())))
        config = Path(child_env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        paths.append(config / "settings.json")
    if "project" in sources:
        paths.append(cwd / ".claude/settings.json")
    if "local" in sources:
        paths.extend(root / ".claude/settings.local.json" for root in _local_roots(cwd))
    for path in dict.fromkeys(paths):
        document = _read(path)
        if document is not None:
            _check(document, str(path), child_env)
    for value in supplied:
        if value.lstrip().startswith(("{", "[", '"')):
            try:
                document = json.loads(value)
            except ValueError:
                raise roster.RosterError("Invalid inline --settings JSON") from None
            source = "inline --settings"
        else:
            path = Path(value)
            path = path if path.is_absolute() else cwd / path
            document, source = _read(path, required=True), str(path)
        _check(document, source, child_env)
