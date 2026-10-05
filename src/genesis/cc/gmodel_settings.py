"""Launch checks for foreground catalog routes: what ``--settings`` cannot pin.

The route itself is pinned by the ``--settings`` file the launcher passes
(``gmodel_routes.route_settings``), which outranks local, project and user
settings for the whole session, including reloads and ``/cd``.

THE LAUNCHER GRAMMAR is fixed: ``gmodel <name> [--route R] [claude args...]``.
``--route`` is recognised only immediately after the name; everything after it
goes to Claude Code untouched. The pass-through arguments are checked by WHOLE
TOKEN, with no table of which Claude option takes a value: such a table is a
copy of Claude Code's parser and drifts from it. The price is that a token that
is really an option's value (``--append-system-prompt -p``) is read as the flag
it spells. That error is always loud: a refusal, or an announced move off an
interactive-only route. Pass such a value as ``--option=value``. Arguments after
``--`` are not inspected, unless that ``--`` directly follows a bare option:
Claude Code then takes it as the option's VALUE and keeps parsing (MEASURED,
CC 2.1.280: ``--append-system-prompt -- --model`` fails on ``--model``).

RESIDUAL, not checked: MANAGED settings outrank ``--settings`` ("Nothing you
set overrides them"; Claude Code settings docs, "Settings precedence", read
2026-10-04). A managed policy can therefore change what a routed session runs.
``/status`` -> Setting sources shows whether one is in force.

The one key the pins cannot override from user, project or local files is
``maxEffortLevel`` (the lowest cap across files applies); ``effort_cap_warnings``
reports one at launch as advice, never as a refusal, because a later edit could
add one anyway.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from genesis.cc import gmodel_routes, roster

_HEADLESS = frozenset({"-p", "--print", "--bg", "--background"})
_RESUME = frozenset({"--resume", "-r", "--continue", "-c", "--from-pr"})
#: Flags that replace the pins (``--model``, ``--fallback-model``, ``--settings``),
#: move the session where this launcher's environment does not reach (``--cloud``,
#: ``--remote``, ``--teleport``, ``--environment``), change how Claude Code
#: authenticates (``--bare``) or turn customizations off (``--safe-mode``; whether
#: that includes the ``--settings`` layer is unverified, so it is refused).
#: ``--route`` is here because it is a launcher option only in first position.
_REFUSED = frozenset({
    "--model", "--fallback-model", "--settings", "--safe-mode", "--bare",
    "--cloud", "--remote", "--teleport", "--environment", "--route",
})
_EFFORT_RANK = {level: rank for rank, level in enumerate(gmodel_routes.EFFORTS)}
_READ_LIMIT = 2 * 1024 * 1024  # CC's own --settings bound; the same cap here.


def _options(args: list[str]) -> list[str]:
    """Option names before ``--``, with any ``=value`` dropped."""
    names, previous = [], ""
    for arg in args:
        # After a bare option, `--` may be that option's value (see module docstring).
        if arg == "--" and not (previous.startswith("-") and "=" not in previous):
            break
        names.append(arg.partition("=")[0] if arg.startswith("--") else arg)
        previous = arg
    return names


def _any(args: list[str], flags: frozenset[str], letters: str) -> bool:
    """A whole-token flag, or a short-option cluster containing one of ``letters``."""
    return any(name in flags or (re.fullmatch(r"-[A-Za-z]{2,}", name) and set(name) & set(letters))
               for name in _options(args))


def extract_route(args: list[str]) -> tuple[str | None, list[str]]:
    """Take ``--route R`` / ``--route=R`` from the first position only."""
    if not args or args[0].partition("=")[0] != "--route":
        return None, list(args)
    if args[0] == "--route":
        value, rest = (args[1] if len(args) > 1 else ""), args[2:]
    else:
        value, rest = args[0].partition("=")[2], args[1:]
    if not value or value.startswith("-"):
        raise roster.RosterError("Missing value for --route")
    return value, rest


def is_headless(args: list[str]) -> bool:
    """A print/background flag, or a short-option cluster containing ``p``."""
    return _any(args, _HEADLESS, "p")


def resumes(args: list[str]) -> bool:
    return _any(args, _RESUME, "cr")


def validate_cli(args: list[str]) -> None:
    """Refuse pass-through flags that would override or escape the route."""
    for name in _options(args):
        if name in _REFUSED:
            hint = (" (--route goes immediately after the model name)" if name == "--route"
                    else "; pass an option value as --option=value, or put a prompt after --")
            # Never interpolate values: credentials and inline JSON can contain secrets.
            raise roster.RosterError(f"Routing conflict in command line: {name}{hint}")


def _read(path: Path) -> object | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_READ_LIMIT + 1)
        if len(raw) > _READ_LIMIT:
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
